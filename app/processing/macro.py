"""Macro-economic regime signal via FRED's fredgraph CSV endpoint (free, no auth).

The fredgraph CSV endpoint (`/graph/fredgraph.csv?id=SERIES`) returns plain
date,value rows with the latest observation last — no API key required, unlike
the FRED JSON API. We pull a handful of regime-defining series:

  - T10Y2Y (10Y-2Y Treasury spread): the classic recession indicator. A
    *negative* (inverted) spread is a risk-off warning.
  - CPIAUCSL (CPI), UNRATE (unemployment), DFF (fed funds), VIXCLS (VIX).

For each series we report the latest non-empty value plus the value ~30 rows
back as a trend reference, and derive a coarse risk-on / risk-off regime from
the yield-spread sign and the VIX level. Results are cached for one hour.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)

_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 6  # fail fast on throttled series rather than blocking the panel
_TREND_LOOKBACK = 30  # rows back for the trend reference

# (series id, label, group). Grouped for a richer macro dashboard.
_SERIES = [
    # Rates & curve
    ("T10Y2Y", "10Y-2Y Spread", "Rates & Curve"),
    ("T10Y3M", "10Y-3M Spread", "Rates & Curve"),
    ("DGS10", "10Y Treasury", "Rates & Curve"),
    ("DFF", "Fed Funds", "Rates & Curve"),
    # Inflation
    ("CPIAUCSL", "CPI", "Inflation"),
    ("T5YIE", "5Y Inflation Expect.", "Inflation"),
    ("DCOILWTICO", "WTI Crude", "Inflation"),
    # Growth & jobs
    ("UNRATE", "Unemployment", "Growth & Jobs"),
    ("ICSA", "Initial Jobless Claims", "Growth & Jobs"),
    ("INDPRO", "Industrial Production", "Growth & Jobs"),
    # Risk & credit
    ("VIXCLS", "VIX", "Risk & Credit"),
    ("BAMLH0A0HYM2", "High-Yield Spread", "Risk & Credit"),
    ("DTWEXBGS", "Dollar Index", "Risk & Credit"),
]

_CACHE_TTL = 3600  # one hour
_cache: dict | None = None
_cache_at: float = 0.0


def fetch_series(series: str) -> dict | None:
    """Return {value, prev} for a FRED series, or None on failure.

    `value` is the latest non-empty observation; `prev` is the value ~30 rows
    back (FRED uses "." for missing observations, which we skip).
    """
    try:
        resp = requests.get(
            _CSV.format(series=series),
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.text.strip().splitlines()
    except Exception:
        logger.debug("fredgraph failed for %s", series)
        return None

    # Drop the header, parse date,value pairs, skip missing (".") values.
    values = []
    for row in rows[1:]:
        parts = row.split(",")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        if not raw or raw == ".":
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue

    if not values:
        return None

    value = values[-1]
    prev = values[-(_TREND_LOOKBACK + 1)] if len(values) > _TREND_LOOKBACK else values[0]
    return {"value": value, "prev": prev}


def _interpret(series: str, value: float, change: float) -> str:
    """One-line read on a single indicator."""
    if series in ("T10Y2Y", "T10Y3M"):
        return "inverted — recession warning" if value < 0 else "normal (positive)"
    if series == "VIXCLS":
        if value > 25:
            return "elevated — fear"
        return "low — calm" if value < 15 else "moderate"
    if series == "BAMLH0A0HYM2":
        return "wide — credit stress" if value > 5 else "tight — risk appetite"
    if series == "ICSA":
        return "rising — labor softening" if change > 0 else "falling — labor firm"
    if series == "UNRATE":
        return "rising" if change > 0 else "falling/stable"
    if series == "T5YIE":
        return "elevated" if value > 2.5 else "anchored"
    return ("rising" if change > 0 else "falling") if change else "flat"


def _outlook_score(by_id: dict[str, dict]) -> dict:
    """Weighted macro-outlook composite in [-100, +100] (risk-off .. risk-on).

    Each present component contributes its weight × its risk sign; we normalize
    by the weights actually available so a throttled series doesn't skew it.
    """
    comps = []  # (label, weight, signed value in [-1, 1])

    def curve(v): return max(-1.0, min(1.0, v / 1.5))          # +1.5% spread = max risk-on
    def vix(v):   return max(-1.0, min(1.0, (18 - v) / 12))     # <18 good, >30 bad
    def hy(v):    return max(-1.0, min(1.0, (4.5 - v) / 3))     # tight spread = risk-on
    def claims(c):return max(-1.0, min(1.0, -c / 50000))         # rising claims = risk-off
    def unemp(c): return max(-1.0, min(1.0, -c * 2))            # rising unemp = risk-off
    def fed(c):   return max(-1.0, min(1.0, -c))               # hikes = risk-off

    spec = [
        ("T10Y2Y", 0.22, curve), ("T10Y3M", 0.10, curve),
        ("VIXCLS", 0.20, vix), ("BAMLH0A0HYM2", 0.18, hy),
        ("ICSA", 0.12, claims), ("UNRATE", 0.10, unemp), ("DFF", 0.08, fed),
    ]
    num = den = 0.0
    for sid, w, fn in spec:
        d = by_id.get(sid)
        if not d:
            continue
        # curve/vix/hy use level; claims/unemp/fed use change.
        x = fn(d["value"]) if sid in ("T10Y2Y", "T10Y3M", "VIXCLS", "BAMLH0A0HYM2") else fn(d["change"])
        comps.append({"id": sid, "label": d["label"], "contribution": round(x * w, 3)})
        num += x * w
        den += w
    score = round((num / den) * 100, 1) if den else 0.0
    if score >= 30:    label = "risk-on"
    elif score <= -30: label = "risk-off"
    else:              label = "neutral"
    return {"score": score, "label": label, "components": comps}


def get_macro() -> dict:
    """Return macro indicators + a coarse risk regime, cached for one hour.

    Shape:
        {"indicators": [{"id", "label", "value", "prev", "change",
                         "interpretation"}],
         "regime": "<risk-on|risk-off|neutral>",
         "updated": None}

    Each series is fetched independently and wrapped in try/except, so a flaky
    or missing series degrades gracefully rather than failing the whole call.
    """
    global _cache, _cache_at
    if _cache is not None and (time.monotonic() - _cache_at) < _CACHE_TTL:
        return _cache
    # Cold start: warm from disk before re-fetching 13 throttled FRED/GDELT series.
    if _cache is None:
        from app import cache as _disk_cache
        disk = _disk_cache.get("macro")
        if disk is not None:
            _cache, _cache_at = disk, time.monotonic()
            return disk

    # Fetch all series concurrently — sequential would be 13×timeout on a
    # throttled host; parallel keeps the whole call near a single timeout.
    with ThreadPoolExecutor(max_workers=13) as pool:
        fetched = list(pool.map(lambda s: (s, fetch_series(s[0])), _SERIES))

    indicators = []
    by_id: dict[str, dict] = {}
    for (series, label, group), data in fetched:
        if not data:
            continue
        value, prev = data["value"], data["prev"]
        change = round(value - prev, 2)
        ind = {
            "id": series, "label": label, "group": group,
            "value": round(value, 2), "prev": round(prev, 2), "change": change,
            "interpretation": _interpret(series, value, change),
        }
        indicators.append(ind)
        by_id[series] = ind

    outlook = _outlook_score(by_id)
    # Backwards-compatible coarse regime label derived from the composite.
    regime = outlook["label"]

    result = {
        "indicators": indicators,
        "outlook": outlook,           # weighted composite score + components
        "regime": regime,
        "updated": None,
    }
    _cache, _cache_at = result, time.monotonic()
    if result.get("indicators"):
        from app import cache as _disk_cache
        _disk_cache.set("macro", result, ttl=_CACHE_TTL)
    return result
