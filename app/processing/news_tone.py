"""Global news volume & sentiment tone per company via GDELT DOC 2.0.

GDELT DOC 2.0 is free and needs no API key, but it rate-limits aggressively
(roughly <1 query/sec; bursts trigger a "limit requests to one every 5 seconds"
cooldown). We therefore add a short retry-with-sleep on each fetch and an
inter-query sleep in the batch helper.

Two modes are used per query:
  - timelinetone: average article tone over time (negative=bad, positive=good).
  - timelinevol:  share of global news coverage matching the query over time.

Both return JSON with timeline[0].data = [{"date", "value"}, ...].
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 15
_TIMESPAN = "14d"

# Module-level cache keyed by query -> (monotonic_expiry, value).
_CACHE: dict[str, tuple[float, dict | None]] = {}
_TTL = 3600.0  # 1 hour

_RETRIES = 3
_RETRY_SLEEP = 5.5  # seconds; GDELT asks for one request every ~5s when throttled


def _fetch_timeline(query: str, mode: str) -> list[dict] | None:
    """Fetch one GDELT timeline mode, returning its data array or None."""
    params = {
        "query": query,
        "mode": mode,
        "format": "json",
        "timespan": _TIMESPAN,
    }
    for attempt in range(_RETRIES):
        try:
            resp = requests.get(
                _BASE, params=params, headers=_HEADERS, timeout=_TIMEOUT
            )
            # GDELT returns a plain-text throttle notice with a 200 status.
            text = resp.text.strip()
            if not text.startswith("{"):
                logger.debug("GDELT throttle/non-json for %s/%s: %s", query, mode, text[:80])
                time.sleep(_RETRY_SLEEP)
                continue
            resp.raise_for_status()
            timeline = resp.json().get("timeline") or []
            if not timeline:
                return None
            return timeline[0].get("data") or []
        except Exception:
            logger.debug("GDELT request failed for %s/%s (attempt %d)", query, mode, attempt)
            time.sleep(_RETRY_SLEEP)
    return None


def get_tone(query: str) -> dict | None:
    """Return news tone/volume signal for a query, or None on failure.

    Shape:
      {
        "query":      str,
        "avg_tone":   float,   # mean of recent tone values (neg=bad, pos=good)
        "tone_trend": float,   # latest tone - avg_tone (>0 = improving sentiment)
        "latest_vol": float,   # latest coverage-volume value
        "vol_spike":  float,   # latest_vol / mean_vol (>1.5 = news spike)
      }
    """
    now = time.monotonic()
    cached = _CACHE.get(query)
    if cached and cached[0] > now:
        return cached[1]

    tone_data = _fetch_timeline(query, "timelinetone")
    time.sleep(0.4)  # space the two calls to respect the rate limit
    vol_data = _fetch_timeline(query, "timelinevol")

    if not tone_data and not vol_data:
        _CACHE[query] = (now + _TTL, None)
        return None

    result: dict = {"query": query}

    tones = [d["value"] for d in (tone_data or []) if d.get("value") is not None]
    if tones:
        avg_tone = sum(tones) / len(tones)
        result["avg_tone"] = round(avg_tone, 2)
        result["tone_trend"] = round(tones[-1] - avg_tone, 2)
    else:
        result["avg_tone"] = None
        result["tone_trend"] = None

    vols = [d["value"] for d in (vol_data or []) if d.get("value") is not None]
    if vols:
        latest_vol = vols[-1]
        mean_vol = sum(vols) / len(vols)
        result["latest_vol"] = round(latest_vol, 4)
        result["vol_spike"] = round(latest_vol / mean_vol, 2) if mean_vol else None
    else:
        result["latest_vol"] = None
        result["vol_spike"] = None

    _CACHE[query] = (now + _TTL, result)
    return result


def get_tone_batch(queries: list[str]) -> dict[str, dict]:
    """Fetch tone for several queries, sleeping 0.4s between (rate limit).

    Failures are skipped; the returned dict only contains successful queries.
    """
    out: dict[str, dict] = {}
    for i, query in enumerate(queries):
        if i:
            time.sleep(0.4)
        data = get_tone(query)
        if data is not None:
            out[query] = data
    return out
