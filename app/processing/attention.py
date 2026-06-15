"""Retail-attention via Wikipedia pageviews (free, no key).

Spikes in pageviews for a company's Wikipedia article proxy public/retail
attention and often precede or accompany price moves. We compute an
attention_ratio analogous to RVOL (see volume.py):

  - attention_ratio: latest day views / 20-day average. >1.5 = attention
    spike — a "people are suddenly looking" marker.
  - trend: 7-day average vs the prior 7 days ("rising"/"falling"/"flat").

The Wikimedia pageviews REST API requires a descriptive User-Agent header.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

_API = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "en.wikipedia/all-access/all-agents/{article}/daily/{start}/{end}"
)
_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_TIMEOUT = 15
_CACHE_TTL = 6 * 60 * 60  # 6 hours

# Module-level cache: {article: (monotonic_expiry, result)}
_cache: dict[str, tuple[float, dict | None]] = {}


def get_attention(article: str) -> dict | None:
    """Return {article, latest_views, avg_views, attention_ratio, trend} or None.

    Fetches the last ~30 daily pageviews for the given Wikipedia article title.
    attention_ratio = latest / 20-day-avg (>1.5 = attention spike).
    Cached per article with a 6-hour TTL.
    """
    now = time.monotonic()
    cached = _cache.get(article)
    if cached and cached[0] > now:
        return cached[1]

    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=30)

    try:
        resp = requests.get(
            _API.format(
                article=article,
                start=start.strftime("%Y%m%d"),
                end=end.strftime("%Y%m%d"),
            ),
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception:
        logger.debug("wikipedia pageviews failed for %s", article)
        return None

    views = [it["views"] for it in items if it.get("views") is not None]
    if len(views) < 8:
        return None

    latest = views[-1]
    window = views[-21:-1] if len(views) > 20 else views[:-1]
    avg = sum(window) / len(window) if window else latest
    ratio = latest / avg if avg else None

    recent_7 = views[-7:]
    prior_7 = views[-14:-7]
    if prior_7:
        recent_avg = sum(recent_7) / len(recent_7)
        prior_avg = sum(prior_7) / len(prior_7)
        if prior_avg and recent_avg > prior_avg * 1.1:
            trend = "rising"
        elif prior_avg and recent_avg < prior_avg * 0.9:
            trend = "falling"
        else:
            trend = "flat"
    else:
        trend = "flat"

    result = {
        "article": article,
        "latest_views": latest,
        "avg_views": round(avg),
        "attention_ratio": round(ratio, 2) if ratio else None,
        "trend": trend,
    }
    _cache[article] = (now + _CACHE_TTL, result)
    return result


def get_attention_batch(articles: list[str]) -> dict[str, dict]:
    """Fetch attention for multiple articles, skipping any that fail."""
    out: dict[str, dict] = {}
    for article in articles:
        data = get_attention(article)
        if data:
            out[article] = data
        time.sleep(0.2)
    return out
