"""Ticker price/earnings refresh (yfinance) and watchlist signal scoring."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import select

from app.models import Signal, Ticker, WatchlistItem

logger = logging.getLogger(__name__)

_ST_TIMEOUT = 8
# Browser-like UA — StockTwits throttles generic clients more aggressively.
_ST_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _refresh_stocktwits(ticker: Ticker) -> None:
    """Fetch bull/bear message counts from StockTwits public stream (no auth)."""
    try:
        resp = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{ticker.symbol}.json",
            params={"limit": 30},
            headers={"User-Agent": _ST_UA},
            timeout=_ST_TIMEOUT,
        )
        if resp.status_code != 200:
            return
        messages = resp.json().get("messages", [])
        bull = sum(
            1 for m in messages
            if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bullish"
        )
        bear = sum(
            1 for m in messages
            if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bearish"
        )
        ticker.st_bull = bull
        ticker.st_bear = bear
        ticker.st_updated_at = datetime.now(timezone.utc)
    except Exception:
        logger.debug("StockTwits fetch skipped for %s", ticker.symbol)


def refresh_ticker(session, symbol: str) -> None:
    ticker = session.get(Ticker, symbol)
    if ticker is None:
        ticker = Ticker(symbol=symbol)
        session.add(ticker)

    # yfinance (price + fundamentals) — independent; failure must NOT skip the
    # StockTwits/consensus steps below, since social alone yields a consensus.
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info
        ticker.name = info.get("shortName") or info.get("longName") or ticker.name
        ticker.sector = info.get("sector") or ticker.sector
        ticker.price = info.get("currentPrice") or info.get("regularMarketPrice")
        ticker.market_cap = info.get("marketCap")
        ticker.week52_high = info.get("fiftyTwoWeekHigh")
        ticker.week52_low = info.get("fiftyTwoWeekLow")
        ts = info.get("earningsTimestamp")
        if ts:
            try:
                ticker.next_earnings = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        ticker.short_interest_pct = info.get("shortPercentOfFloat")
        ticker.short_ratio = info.get("shortRatio")
        ticker.analyst_rating = info.get("recommendationKey")
        ticker.analyst_target = info.get("targetMeanPrice")
        ticker.analyst_count = info.get("numberOfAnalystOpinions")
    except Exception:
        logger.warning("yfinance fetch failed for %s (using social-only data)", symbol)

    ticker.updated_at = datetime.now(timezone.utc)
    # Volume/momentum + price fallback (Yahoo v8 chart — works when yfinance 429s)
    from app.processing.volume import refresh_volume
    refresh_volume(ticker)
    # StockTwits sentiment (non-critical — failure doesn't abort)
    _refresh_stocktwits(ticker)
    # Recompute blended consensus from whatever positioning data we have.
    from app.processing.consensus import refresh_consensus
    refresh_consensus(ticker)


def refresh_watchlist_tickers(session) -> int:
    from app.processing.consensus import refresh_consensus
    from app.processing.short_volume import get_short_volume

    symbols = {
        row[0]
        for row in session.execute(select(WatchlistItem.ticker_symbol)).all()
    }
    # One FINRA pull for all tickers (an always-reachable positioning input).
    short_vol = {
        t["symbol"]: t["short_pct"]
        for t in get_short_volume(list(symbols)).get("tickers", [])
    }
    for symbol in symbols:
        refresh_ticker(session, symbol)
        # Re-blend consensus with FINRA short volume + our own signal flow, so it
        # populates even when yfinance/StockTwits are IP-blocked.
        ticker = session.get(Ticker, symbol)
        if ticker is not None:
            flow = signal_score(session, symbol).get("score")
            refresh_consensus(ticker, short_vol_pct=short_vol.get(symbol), signal_flow=flow)
    return len(symbols)


def signal_score(session, symbol: str, days: int = 7) -> dict:
    """Net bullish/bearish score for a ticker from recent signals.

    score = (bullish - bearish) / total, computed over signals whose entities
    list the ticker. Returns counts plus a sparkline of daily signal volume.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    signals = (
        session.execute(select(Signal).where(Signal.created_at >= cutoff))
        .scalars()
        .all()
    )
    bullish = bearish = total = 0
    daily: dict[str, int] = {}
    for sig in signals:
        try:
            tickers = json.loads(sig.entities_json or "{}").get("tickers", [])
        except json.JSONDecodeError:
            tickers = []
        if symbol.upper() not in {str(t).upper() for t in tickers}:
            continue
        total += 1
        if sig.direction == "bullish":
            bullish += 1
        elif sig.direction == "bearish":
            bearish += 1
        day = sig.created_at.strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + 1
    score = (bullish - bearish) / total if total else 0.0
    return {
        "symbol": symbol,
        "score": round(score, 3),
        "bullish": bullish,
        "bearish": bearish,
        "total": total,
        "sparkline": [daily.get((cutoff + timedelta(days=i)).strftime("%Y-%m-%d"), 0)
                      for i in range(days + 1)],
    }
