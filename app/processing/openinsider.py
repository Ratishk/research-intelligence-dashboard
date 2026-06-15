"""Insider *cluster buys* from OpenInsider (http://openinsider.com).

A "cluster buy" is when MULTIPLE insiders at the same company buy the stock in
the same window — clustered open-market Form 4 purchases. Whereas one executive
buying can be noise, several insiders buying together is a high-conviction,
non-consensus bullish signal, which is why it gets its own surface alongside the
13F fund-consensus view.

We scrape the pre-built "latest cluster buys" screen, which is already
recent-first. The data table carries class ``tinytable``; its columns (verified
2026-06) are:

    X | Filing Date | Trade Date | Ticker | Company Name | Industry |
    Ins (# insiders) | Trade Type | Price | Qty | Owned | ΔOwn | Value | 1d 1w 1m 6m

The basic Scrapling ``Fetcher`` (TLS-impersonating plain HTTP, no browser) fetches
this fine; a plain ``requests`` call is the graceful fallback. Everything is wrapped
in try/except — on any failure we return an empty result and never raise.
"""
from __future__ import annotations

import logging
import time

from bs4 import BeautifulSoup

from app.processing.scrapling_fetch import fetch_html

logger = logging.getLogger(__name__)

_URL = "http://openinsider.com/latest-cluster-buys"
_CACHE_TTL = 6 * 3600  # 6 hours
# (monotonic_timestamp, value)
_cache: tuple[float, dict] | None = None


def _to_int(text: str) -> int:
    """Parse '$1,234,567' / '+136,818' / '402,630' into an int (0 on failure)."""
    if not text:
        return 0
    cleaned = (
        text.replace("$", "")
        .replace(",", "")
        .replace("+", "")
        .strip()
    )
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def _parse_rows(html: str, limit: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="tinytable")
    if not table:
        return []
    body = table.find("tbody")
    rows = body.find_all("tr") if body else table.find_all("tr")[1:]

    out: list[dict] = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 13:
            continue
        cells = [td.get_text(strip=True) for td in tds]
        # Column indices per the verified header layout.
        out.append({
            "filing_date": cells[1],
            "trade_date": cells[2],
            "ticker": cells[3],
            "company": cells[4],
            "industry": cells[5],
            "num_insiders": _to_int(cells[6]),
            "price": cells[8],
            "qty": _to_int(cells[9]),
            "value_usd": _to_int(cells[12]),
        })
        if len(out) >= limit:
            break
    return out


def get_cluster_buys(limit: int = 40) -> dict:
    """Latest insider cluster buys from OpenInsider.

    Returns: {
        "as_of": str|None,   # filing date of the most recent cluster buy
        "clusters": [{
            "filing_date": str, "trade_date": str, "ticker": str, "company": str,
            "industry": str, "num_insiders": int, "price": str, "qty": int,
            "value_usd": int,
        }, ...]              # already recent-first, capped at `limit`
    }

    Cached in-memory for ~6h. Returns an empty result on any failure (never raises).
    """
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    result: dict = {"as_of": None, "clusters": []}
    try:
        html = fetch_html(_URL)
        if html:
            clusters = _parse_rows(html, limit)
            result["clusters"] = clusters
            if clusters:
                result["as_of"] = clusters[0]["filing_date"]
    except Exception:
        logger.exception("get_cluster_buys failed")

    _cache = (now, result)
    return result
