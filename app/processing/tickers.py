"""Ticker price/earnings refresh (yfinance) and watchlist signal scoring."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Signal, Ticker, WatchlistItem

logger = logging.getLogger(__name__)


def refresh_ticker(session, symbol: str) -> None:
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info
    except Exception:
        logger.warning("yfinance fetch failed for %s", symbol)
        return
    ticker = session.get(Ticker, symbol)
    if ticker is None:
        ticker = Ticker(symbol=symbol)
        session.add(ticker)
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
    ticker.updated_at = datetime.now(timezone.utc)


def refresh_watchlist_tickers(session) -> int:
    symbols = {
        row[0]
        for row in session.execute(select(WatchlistItem.ticker_symbol)).all()
    }
    for symbol in symbols:
        refresh_ticker(session, symbol)
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
