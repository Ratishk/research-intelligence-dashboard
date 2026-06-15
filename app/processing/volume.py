"""Volume & momentum via Yahoo's v8 chart API (free, no auth, not rate-limited).

The v8 chart endpoint is a different host/path from the quoteSummary endpoint
that hard-throttles, so it works where yfinance fails. We use it to compute:

  - RVOL (relative volume): latest day volume / 20-day average. >1.5 = unusual,
    >2.5 = highly unusual — a classic "something is happening" marker.
  - 1-day and 5-day price change %.
  - Latest price (a reliable fallback when yfinance is blocked).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 15


def fetch_volume(symbol: str) -> dict | None:
    """Return {price, latest_volume, avg_volume, rvol, change_pct, change_5d_pct}."""
    try:
        resp = requests.get(
            _CHART.format(symbol=symbol),
            params={"range": "1mo", "interval": "1d"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
    except Exception:
        logger.debug("v8 chart failed for %s", symbol)
        return None

    quote = result.get("indicators", {}).get("quote", [{}])[0]
    vols = [v for v in (quote.get("volume") or []) if v]
    closes = [c for c in (quote.get("close") or []) if c]
    if len(vols) < 6 or len(closes) < 2:
        return None

    latest_vol = vols[-1]
    window = vols[-21:-1] if len(vols) > 20 else vols[:-1]
    avg_vol = sum(window) / len(window) if window else latest_vol
    rvol = latest_vol / avg_vol if avg_vol else None

    price = closes[-1]
    change_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else None
    change_5d = ((closes[-1] - closes[-6]) / closes[-6] * 100) if len(closes) >= 6 else None

    return {
        "price": round(price, 2),
        "latest_volume": latest_vol,
        "avg_volume": avg_vol,
        "rvol": round(rvol, 2) if rvol else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "change_5d_pct": round(change_5d, 2) if change_5d is not None else None,
    }


def refresh_volume(ticker) -> bool:
    """Populate volume/momentum fields on a Ticker. Returns True on success."""
    data = fetch_volume(ticker.symbol)
    if not data:
        return False
    ticker.rvol = data["rvol"]
    ticker.latest_volume = data["latest_volume"]
    ticker.change_pct = data["change_pct"]
    ticker.change_5d_pct = data["change_5d_pct"]
    # Use v8 price as a fallback when yfinance hasn't set one.
    if ticker.price is None and data["price"]:
        ticker.price = data["price"]
    ticker.updated_at = datetime.now(timezone.utc)
    return True
