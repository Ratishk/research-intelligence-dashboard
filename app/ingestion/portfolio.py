"""Portfolio ingestion — read the LogiqGPT LCO fund holdings snapshot.

The holdings JSON is maintained by another app (logiqgpt, synced daily from
logiqetf.com); we read the file directly with no runtime dependency on it. Each
import is a SNAPSHOT: existing Holding rows for the source are replaced wholesale
so the table always mirrors the latest file.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.models import Holding, Portfolio, WatchlistItem

logger = logging.getLogger(__name__)

_NAME = {"LCO": "LogiqGPT LCO Fund"}
# Holdings are auto-tracked under this watchlist so the 6h ticker-refresh cron
# enriches them and the ingestion pre-filter lets their mentions become signals.
_WATCHLIST = {"LCO": "LCO Portfolio"}


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp to a NAIVE UTC datetime (SQLite stores naive)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def import_holdings(db: Session, *, source: str = "LCO", path: str | None = None) -> dict:
    """Replace all Holding rows for ``source`` with the latest snapshot file.

    Raises FileNotFoundError if the snapshot is missing (the route maps it to a
    404). Returns a small counts dict.
    """
    snapshot_path = path or config.LCO_HOLDINGS_PATH
    try:
        with open(snapshot_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.info("holdings snapshot not found at %s", snapshot_path)
        raise

    rows = data.get("holdings", []) or []
    meta = data.get("meta", {}) or {}

    # Snapshot semantics: drop the old rows, insert the current file wholesale.
    db.query(Holding).filter(Holding.source == source).delete()
    equity = 0
    held: set[str] = set()
    for h in rows:
        bucket = (h.get("bucket") or "").strip()
        ticker = (h.get("ticker") or "").strip().upper()
        yf = (h.get("yfinance_ticker") or "").strip().upper()
        if bucket == "equity":
            equity += 1
            sym = yf or ticker
            if sym:
                held.add(sym)
        db.add(Holding(
            source=source,
            ticker=ticker,
            name=(h.get("name") or "").strip(),
            cusip=(h.get("cusip") or "").strip(),
            shares=_f(h.get("shares")),
            price=_f(h.get("price")),
            market_value=_f(h.get("market_value")),
            weight_pct=_f(h.get("weight_pct")),
            bucket=bucket,
            yfinance_ticker=yf,
        ))

    # Auto-track: sync the holdings watchlist to exactly the current equity names
    # (add new, drop names no longer held) so they enter the tracked universe.
    tracked = _sync_watchlist(db, source, held)

    synced_at = _parse_iso(meta.get("synced_at"))
    aum = _f(meta.get("aum"))
    cash_weight_pct = _f(meta.get("cash_weight_pct"))
    pf = db.get(Portfolio, source)
    if pf is None:
        pf = Portfolio(source=source)
        db.add(pf)
    pf.name = _NAME.get(source, source)
    pf.aum = aum
    pf.cash_weight_pct = cash_weight_pct
    pf.synced_at = synced_at
    pf.updated_at = datetime.utcnow()

    db.commit()
    logger.info(
        "imported %d holdings (%d equity, %d tracked) for %s",
        len(rows), equity, tracked, source,
    )
    return {
        "source": source,
        "holdings": len(rows),
        "equity": equity,
        "tracked": tracked,
        "aum": aum,
        "synced_at": synced_at.isoformat() if synced_at else None,
    }


def _sync_watchlist(db: Session, source: str, held: set[str]) -> int:
    """Make the source's holdings watchlist match ``held`` exactly. Returns the
    number of tracked tickers after sync."""
    list_name = _WATCHLIST.get(source, f"{source} Portfolio")
    existing = {
        row[0] for row in db.execute(
            select(WatchlistItem.ticker_symbol).where(WatchlistItem.list_name == list_name)
        ).all()
    }
    for sym in held - existing:
        db.add(WatchlistItem(list_name=list_name, ticker_symbol=sym))
    stale = existing - held
    if stale:
        db.query(WatchlistItem).filter(
            WatchlistItem.list_name == list_name,
            WatchlistItem.ticker_symbol.in_(stale),
        ).delete(synchronize_session=False)
    return len(held)


def held_tickers(db: Session, source: str = "LCO") -> set[str]:
    """Uppercase set of non-empty equity tickers (prefer yfinance_ticker)."""
    rows = db.execute(
        select(Holding.ticker, Holding.yfinance_ticker)
        .where(Holding.source == source, Holding.bucket == "equity")
    ).all()
    out: set[str] = set()
    for ticker, yf in rows:
        sym = (yf or ticker or "").strip().upper()
        if sym:
            out.add(sym)
    return out


def holdings_list(db: Session, source: str = "LCO") -> list[dict]:
    """Equity holdings sorted by weight_pct desc."""
    rows = db.execute(
        select(Holding).where(Holding.source == source, Holding.bucket == "equity")
    ).scalars().all()
    rows.sort(key=lambda h: h.weight_pct or 0.0, reverse=True)
    return [{
        "ticker": h.ticker,
        "name": h.name,
        "weight_pct": h.weight_pct,
        "market_value": h.market_value,
        "shares": h.shares,
        "price": h.price,
        "bucket": h.bucket,
        "yfinance_ticker": h.yfinance_ticker,
    } for h in rows]


def portfolio_meta(db: Session, source: str = "LCO") -> dict | None:
    pf = db.get(Portfolio, source)
    if pf is None:
        return None
    return {
        "source": pf.source,
        "name": pf.name,
        "aum": pf.aum,
        "cash_weight_pct": pf.cash_weight_pct,
        "synced_at": pf.synced_at.isoformat() if pf.synced_at else None,
        "updated_at": pf.updated_at.isoformat() if pf.updated_at else None,
    }
