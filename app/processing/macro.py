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

import requests

logger = logging.getLogger(__name__)

_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 6  # fail fast on throttled series rather than blocking the panel
_TREND_LOOKBACK = 30  # rows back for the trend reference

_SERIES = [
    ("T10Y2Y", "10Y-2Y Spread"),
    ("CPIAUCSL", "CPI"),
    ("UNRATE", "Unemployment"),
    ("DFF", "Fed Funds"),
    ("VIXCLS", "VIX"),
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


def _interpret(series: str, value: float) -> str:
    """One-line read on a single indicator."""
    if series == "T10Y2Y":
        return "inverted — recession warning" if value < 0 else "normal (positive)"
    if series == "VIXCLS":
        if value > 25:
            return "elevated — fear"
        return "low — calm" if value < 15 else "moderate"
    return "see trend"


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

    indicators = []
    spread = None
    vix = None
    for series, label in _SERIES:
        data = fetch_series(series)
        if not data:
            continue
        value, prev = data["value"], data["prev"]
        change = round(value - prev, 2)
        indicators.append(
            {
                "id": series,
                "label": label,
                "value": round(value, 2),
                "prev": round(prev, 2),
                "change": change,
                "interpretation": _interpret(series, value),
            }
        )
        if series == "T10Y2Y":
            spread = value
        elif series == "VIXCLS":
            vix = value

    # Regime: inverted curve or a fearful VIX tilts risk-off; a healthy
    # positive spread with calm VIX is risk-on; otherwise neutral.
    if (spread is not None and spread < 0) or (vix is not None and vix > 25):
        regime = "risk-off"
    elif spread is not None and spread > 0 and (vix is None or vix < 20):
        regime = "risk-on"
    else:
        regime = "neutral"

    result = {"indicators": indicators, "regime": regime, "updated": None}
    _cache, _cache_at = result, time.monotonic()
    return result
