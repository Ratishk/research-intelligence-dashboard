"""Ticker price/earnings refresh (yfinance) and watchlist signal scoring."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import select

from app.models import Signal, Ticker, WatchlistItem

logger = logging.getLogger(__name__)

# Cross-thread rate limiter for the per-symbol external fetches (yfinance /
# StockTwits / Yahoo chart). Caps the *start* rate to ~6/s so parallel refreshes
# don't hammer those throttled hosts; network waits still overlap.
_REFRESH_MIN_INTERVAL = 0.15
_refresh_lock = threading.Lock()
_refresh_last = [0.0]


def _refresh_throttle() -> None:
    with _refresh_lock:
        wait = _REFRESH_MIN_INTERVAL - (time.monotonic() - _refresh_last[0])
        if wait > 0:
            time.sleep(wait)
        _refresh_last[0] = time.monotonic()

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
    _refresh_throttle()  # cap aggregate start rate across parallel refreshes
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


def signal_flow_scores(session, symbols: set[str], days: int = 7) -> dict[str, float]:
    """Net bullish/bearish flow score for MANY symbols in ONE query.

    Replaces the per-symbol N+1 (one full 7-day signal scan per ticker) with a
    single scan that buckets directions per ticker. Returns {symbol: score} where
    score = (bullish - bearish) / total over signals whose entities list the ticker.
    """
    if not symbols:
        return {}
    wanted = {s.upper() for s in symbols}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    signals = (
        session.execute(select(Signal).where(Signal.created_at >= cutoff))
        .scalars()
        .all()
    )
    buckets: dict[str, list[int]] = {s: [0, 0, 0] for s in wanted}  # [bull, bear, total]
    for sig in signals:
        try:
            tickers = json.loads(sig.entities_json or "{}").get("tickers", [])
        except json.JSONDecodeError:
            continue
        for t in {str(t).upper() for t in tickers}:
            if t not in buckets:
                continue
            b = buckets[t]
            b[2] += 1
            if sig.direction == "bullish":
                b[0] += 1
            elif sig.direction == "bearish":
                b[1] += 1
    return {
        sym: round((b[0] - b[1]) / b[2], 3) if b[2] else 0.0
        for sym, b in buckets.items()
    }


def _refresh_one_symbol(symbol: str, short_vol_pct, flow) -> None:
    """Refresh one watchlist symbol in its OWN DB session (thread-safe).

    Each thread gets its own session; check_same_thread is False on the engine so
    the connection pool is shared across threads. Failures are isolated so one bad
    symbol can't abort the batch.
    """
    from app.database import session_scope
    from app.processing.consensus import refresh_consensus

    try:
        with session_scope() as ts:
            refresh_ticker(ts, symbol)
            ticker = ts.get(Ticker, symbol)
            if ticker is not None:
                refresh_consensus(ticker, short_vol_pct=short_vol_pct, signal_flow=flow)
    except Exception:
        logger.exception("watchlist refresh failed for %s", symbol)


def refresh_watchlist_tickers(session, max_workers: int = 6) -> int:
    from app.processing.short_volume import get_short_volume

    symbols = {
        row[0]
        for row in session.execute(select(WatchlistItem.ticker_symbol)).all()
    }
    if not symbols:
        return 0
    # One FINRA pull for all tickers (an always-reachable positioning input).
    short_vol = {
        t["symbol"]: t["short_pct"]
        for t in get_short_volume(list(symbols)).get("tickers", [])
    }
    # One batched query for all per-symbol signal-flow scores (was N+1).
    flows = signal_flow_scores(session, symbols)

    # Parallelize the per-symbol external fetches; each thread uses its own session.
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ticker") as pool:
        futs = [
            pool.submit(_refresh_one_symbol, sym, short_vol.get(sym), flows.get(sym))
            for sym in symbols
        ]
        for fut in as_completed(futs):
            fut.result()  # surface nothing; _refresh_one_symbol already logs failures
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
