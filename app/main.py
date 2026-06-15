"""FastAPI application: REST API + static frontend.

Serves the 9-tab dashboard from /frontend and exposes /api/* used by app.js.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import config
from app.database import get_db, init_db
from app.discovery import engine as discovery_engine
from app.ingestion.runner import run_all_ingestion
from app.models import (
    DailyFinding,
    Holding,
    Industry,
    Item,
    Portfolio,
    ShiftAlert,
    Signal,
    Source,
    SourceStatus,
    SourceType,
    Thesis,
    ThesisEvidence,
    Ticker,
    WatchlistItem,
)
from app.ingestion import portfolio as portfolio_ingest
from app.processing import digest as digest_mod
from app.processing import research
from app.processing.alpha import score_signals
from app.processing.tickers import refresh_ticker
from app.llm import claude

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Research Intelligence Dashboard")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Tell the browser not to cache the frontend, so a fresh `python3 run.py`
    never serves a stale app.js/charts.js against updated HTML (the cause of
    'failed to load' after edits). Local dev tool — caching buys us nothing."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# -------------------------------------------------------------- ingest state
_ingest_state: dict = {
    "running": False,
    "started_at": None,
    "last_run": None,
    "last_counts": {},
}


def _run_ingest_background() -> None:
    global _ingest_state
    _ingest_state["running"] = True
    _ingest_state["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        counts = run_all_ingestion()
        _ingest_state["last_counts"] = counts
        _ingest_state["last_run"] = datetime.now(timezone.utc).isoformat()
    finally:
        _ingest_state["running"] = False


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # Pre-warm the hedge-fund consensus in the background so the first page load
    # is instant (a cold scrape over hundreds of funds takes a few minutes).
    try:
        from app.processing.funds import warm_consensus
        warm_consensus(force=False)
    except Exception:
        logger.exception("consensus pre-warm failed to start")


# ---------------------------------------------------------------- serialization
def _as_aware(dt: datetime) -> datetime:
    """SQLite stores naive datetimes; treat them as UTC for safe comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _item_dict(item: Item) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "author": item.author,
        "media_type": item.media_type,
        "source": item.source.name if item.source else "",
        "source_type": item.source.type if item.source else "",
        "tier": item.source.tier if item.source else 2,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "ingested_at": item.ingested_at.isoformat() if item.ingested_at else None,
        "relevance": item.relevance,
    }


def _signal_dict(sig: Signal, alpha: dict | None = None) -> dict:
    try:
        entities = json.loads(sig.entities_json or "{}")
    except json.JSONDecodeError:
        entities = {}
    industry = None
    if sig.item and sig.item.source and sig.item.source.industries:
        industry = sig.item.source.industries[0].name
    d = {
        "id": sig.id,
        "signal_type": sig.signal_type,
        "confidence": sig.confidence,
        "direction": sig.direction,
        "summary": sig.summary,
        "entities": entities,
        "speculative": sig.speculative,
        "research_brief": sig.research_brief,
        "industry": industry,
        "url": sig.item.url if sig.item else "",
        "created_at": sig.created_at.isoformat() if sig.created_at else None,
    }
    if alpha:
        d["alpha_score"] = alpha["total"]
        d["alpha_label"] = alpha["label"]
        d["alpha_components"] = {
            "timeliness": alpha["timeliness"],
            "originality": alpha["originality"],
            "exclusivity": alpha["exclusivity"],
            "contrarian": alpha["contrarian"],
        }
    return d


# ------------------------------------------------------------------------ feed
@app.get("/api/feed")
def get_feed(
    tab: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> JSONResponse:
    query = select(Item).join(Source).order_by(Item.ingested_at.desc())
    tab_media = {
        "youtube": ["video"],
        "twitter": ["tweet"],
        "blogs": ["article", "filing", "preprint"],
    }
    if tab in tab_media:
        query = query.where(Item.media_type.in_(tab_media[tab]))
    items = db.execute(query.limit(limit).offset(offset)).scalars().all()
    return JSONResponse([_item_dict(i) for i in items])


# --------------------------------------------------------------------- sources
@app.get("/api/sources")
def list_sources(db: Session = Depends(get_db)) -> JSONResponse:
    sources = db.execute(select(Source).order_by(Source.name)).scalars().all()
    return JSONResponse(
        [
            {
                "id": s.id,
                "name": s.name,
                "type": s.type,
                "url": s.url,
                "tags": s.tags,
                "tier": s.tier,
                "vetting_score": s.vetting_score,
                "signal_yield": round(s.signal_yield, 3),
                "status": s.status,
                "discovered_by": s.discovered_by,
                "industries": [i.name for i in s.industries],
                "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
            }
            for s in sources
        ]
    )


@app.post("/api/sources")
def add_source(payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    name = (payload.get("name") or "").strip()
    type_ = (payload.get("type") or "").strip()
    if not name or not type_:
        raise HTTPException(400, "name and type are required")
    source = Source(
        name=name,
        type=type_,
        url=(payload.get("url") or "").strip(),
        handle=(payload.get("handle") or "").strip(),
        tags=(payload.get("tags") or "").strip(),
        tier=int(payload.get("tier", 2)),
        discovered_by="manual",
        status=SourceStatus.active.value,
        active=True,
    )
    industry_name = payload.get("industry")
    if industry_name:
        ind = db.scalar(select(Industry).where(Industry.name == industry_name))
        if ind:
            source.industries.append(ind)
    db.add(source)
    db.commit()
    return JSONResponse({"id": source.id})


@app.patch("/api/sources/{source_id}")
def patch_source(source_id: int, payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "source not found")
    if "active" in payload:
        source.active = bool(payload["active"])
        source.status = (
            SourceStatus.active.value if source.active else SourceStatus.retired.value
        )
    if "tags" in payload:
        source.tags = str(payload["tags"])
    if "tier" in payload:
        source.tier = int(payload["tier"])
    db.commit()
    return JSONResponse({"ok": True})


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "source not found")
    db.delete(source)
    db.commit()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------- signals
@app.get("/api/signals")
def list_signals(
    type: str | None = None,
    direction: str | None = None,
    ticker: str | None = None,
    sort: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """
    sort: newest (default) | confidence | alpha | contrarian
    Alpha and contrarian sorts fetch up to 300 candidates to score, then trim.
    """
    fetch_limit = 300 if sort in ("alpha", "contrarian") else limit
    query = select(Signal).order_by(Signal.created_at.desc())
    if type:
        query = query.where(Signal.signal_type == type)
    if direction:
        query = query.where(Signal.direction == direction)
    signals = db.execute(query.limit(fetch_limit)).scalars().all()

    # Ticker post-filter
    if ticker:
        t = ticker.upper()
        signals = [
            s for s in signals
            if t in {str(x).upper() for x in (json.loads(s.entities_json or "{}").get("tickers", []))}
        ]

    if sort == "confidence":
        signals = sorted(signals, key=lambda s: s.confidence, reverse=True)
        out = [_signal_dict(s) for s in signals[:limit]]
    elif sort in ("alpha", "contrarian"):
        scored = score_signals(signals, db, cache_key=f"{sort}-{direction}-{type}")
        if sort == "alpha":
            scored.sort(key=lambda x: x[1]["total"], reverse=True)
        else:
            scored.sort(key=lambda x: x[1]["contrarian"], reverse=True)
        out = [_signal_dict(s, a) for s, a in scored[:limit]]
    else:
        out = [_signal_dict(s) for s in signals[:limit]]

    return JSONResponse(out)


@app.post("/api/signals/{signal_id}/research")
def signal_research(signal_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(404, "signal not found")
    brief = research.generate_brief(db, signal)
    db.commit()
    return JSONResponse({"research_brief": brief})


# --------------------------------------------------------- form 144 / 8-K feeds
_FORM144_PREFIX = "FORM144_JSON "


@app.get("/api/form144")
def list_form144(days: int = 30, db: Session = Depends(get_db)) -> JSONResponse:
    """Planned insider sales (Form 144) within the window — contract C2.

    Reads the structured payload stashed in Item.content by the form144 ingester
    (prefixed FORM144_JSON). Falls back to the item's own fields if a row predates
    the structured-content format.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    rows = db.execute(
        select(Item)
        .join(Source, Item.source_id == Source.id)
        .where(Source.type == SourceType.form144.value)
        .order_by(Item.published_at.desc().nullslast())
        .limit(500)
    ).scalars().all()

    items = []
    for it in rows:
        filed_dt = _as_aware(it.published_at) if it.published_at else None
        if filed_dt is None or filed_dt < cutoff:
            continue
        content = it.content or ""
        # Prefer the structured rows the ingester writes (FORM144_JSON payload).
        # Legacy unstructured rows (person="SEC Form 144", no shares/ticker) carry
        # no usable C2 fields, so skip them rather than pad the feed with blanks.
        if not content.startswith(_FORM144_PREFIX):
            continue
        try:
            payload = json.loads(content[len(_FORM144_PREFIX):])
        except json.JSONDecodeError:
            payload = {}
        items.append({
            "filed": filed_dt.isoformat() if filed_dt else payload.get("filed"),
            "person": payload.get("person") or it.author or "",
            "issuer": payload.get("issuer") or "",
            "ticker": payload.get("ticker") or "",
            "shares": payload.get("shares"),
            "value_usd": payload.get("value_usd"),
            "approx_sale_date": payload.get("approx_sale_date"),
            "url": payload.get("url") or it.url,
        })
    return JSONResponse({"items": items, "count": len(items)})


@app.get("/api/unusual-whales")
def list_unusual_whales(limit: int = 50, db: Session = Depends(get_db)) -> JSONResponse:
    """Latest persisted Unusual Whales data from research.db (NOT a live passthrough).

    Reads the newest rows from each uw_* table (by pulled_at) and returns them
    grouped by feed. raw_json is parsed back to objects so the client gets the
    full point-in-time payload. Driven by the unusual_whales ingester.
    """
    from app.models import (
        UWCongress,
        UWDarkpool,
        UWFlowAlert,
        UWGreeks,
        UWInsider,
        UWMarketTide,
    )

    feeds = {
        "market_tide": UWMarketTide,
        "flow_alerts": UWFlowAlert,
        "darkpool": UWDarkpool,
        "congress": UWCongress,
        "insider": UWInsider,
        "greeks": UWGreeks,
    }
    cap = max(1, min(limit, 500))
    out: dict[str, list] = {}
    total = 0
    for name, model in feeds.items():
        rows = db.execute(
            select(model).order_by(model.pulled_at.desc()).limit(cap)
        ).scalars().all()
        items = []
        for r in rows:
            try:
                payload = json.loads(r.raw_json) if r.raw_json else {}
            except json.JSONDecodeError:
                payload = {}
            pulled = r.pulled_at
            items.append({
                "uw_hash": r.uw_hash,
                "pulled_at": (_as_aware(pulled).isoformat() if pulled else None),
                "data": payload,
            })
        out[name] = items
        total += len(items)
    return JSONResponse({"feeds": out, "count": total})


@app.get("/api/events8k")
def list_events8k(days: int = 14, db: Session = Depends(get_db)) -> JSONResponse:
    """Material 8-K events within the window — contract C2.

    Each 8-K signal/item is labelled+directioned by the events8k ingester; the
    item codes are parsed from the "Items: x.xx,y.yy" suffix stored in content.
    """
    from app.ingestion.events8k import _ITEMS  # label/direction source of truth

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    sigs = db.execute(
        select(Signal)
        .join(Item, Signal.item_id == Item.id)
        .join(Source, Item.source_id == Source.id)
        .where(Source.type == SourceType.form8k.value)
        .order_by(Signal.created_at.desc())
        .limit(500)
    ).scalars().all()

    events = []
    by_item: dict[str, int] = {}
    for s in sigs:
        item = s.item
        if item is None:
            continue
        filed_dt = _as_aware(item.published_at) if item.published_at else (
            _as_aware(s.created_at) if s.created_at else None
        )
        if filed_dt is None or filed_dt < cutoff:
            continue
        content = item.content or ""
        codes: list[str] = []
        if "Items:" in content:
            raw = content.rsplit("Items:", 1)[-1]
            codes = [c.strip() for c in raw.split(",") if c.strip() in _ITEMS]
        label = ""
        if codes:
            label = _ITEMS[codes[0]][0]
        try:
            entities = json.loads(s.entities_json or "{}")
        except json.JSONDecodeError:
            entities = {}
        tickers = entities.get("tickers") or []
        events.append({
            "ticker": tickers[0] if tickers else "",
            "item_codes": codes,
            "label": label,
            "direction": s.direction,
            "summary": s.summary,
            "url": item.url,
            "filed": filed_dt.isoformat(),
        })
        for c in codes:
            by_item[c] = by_item.get(c, 0) + 1
    return JSONResponse({"events": events, "by_item": by_item})


# ------------------------------------------------------------------ watchlists
@app.get("/api/watchlists")
def get_watchlists(db: Session = Depends(get_db)) -> JSONResponse:
    rows = db.execute(select(WatchlistItem)).scalars().all()
    if not rows:
        return JSONResponse({})

    symbols = list({r.ticker_symbol for r in rows})

    # Load all tickers in one query instead of N individual lookups.
    tickers_by_symbol = {
        t.symbol: t
        for t in db.execute(select(Ticker).where(Ticker.symbol.in_(symbols))).scalars().all()
    }

    # Load all recent signals once, then score every symbol in Python.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    all_signals = db.execute(
        select(Signal).where(Signal.created_at >= cutoff)
    ).scalars().all()

    scores: dict[str, dict] = {
        sym: {"bullish": 0, "bearish": 0, "total": 0, "daily": {}}
        for sym in symbols
    }
    for sig in all_signals:
        try:
            sig_tickers = json.loads(sig.entities_json or "{}").get("tickers", [])
        except json.JSONDecodeError:
            sig_tickers = []
        for t in sig_tickers:
            sym = str(t).upper()
            if sym not in scores:
                continue
            scores[sym]["total"] += 1
            if sig.direction == "bullish":
                scores[sym]["bullish"] += 1
            elif sig.direction == "bearish":
                scores[sym]["bearish"] += 1
            day = sig.created_at.strftime("%Y-%m-%d")
            scores[sym]["daily"][day] = scores[sym]["daily"].get(day, 0) + 1

    def _sparkline(daily: dict) -> list[int]:
        return [
            daily.get((cutoff + timedelta(days=i)).strftime("%Y-%m-%d"), 0)
            for i in range(8)
        ]

    lists: dict[str, list] = {}
    for row in rows:
        sym = row.ticker_symbol
        ticker = tickers_by_symbol.get(sym)
        sc = scores[sym]
        total = sc["total"]
        score_val = round((sc["bullish"] - sc["bearish"]) / total, 3) if total else 0.0
        lists.setdefault(row.list_name, []).append(
            {
                "symbol": sym,
                "name": ticker.name if ticker else "",
                "price": ticker.price if ticker else None,
                "week52_high": ticker.week52_high if ticker else None,
                "week52_low": ticker.week52_low if ticker else None,
                "next_earnings": ticker.next_earnings.isoformat()
                if ticker and ticker.next_earnings
                else None,
                "signal_score": score_val,
                "bullish": sc["bullish"],
                "bearish": sc["bearish"],
                "sparkline": _sparkline(sc["daily"]),
                # Webull-equivalent fundamentals
                "short_interest_pct": ticker.short_interest_pct if ticker else None,
                "short_ratio": ticker.short_ratio if ticker else None,
                "analyst_rating": ticker.analyst_rating if ticker else None,
                "analyst_target": ticker.analyst_target if ticker else None,
                "analyst_count": ticker.analyst_count if ticker else None,
                # StockTwits social sentiment
                "st_bull": ticker.st_bull if ticker else 0,
                "st_bear": ticker.st_bear if ticker else 0,
                # Blended consensus
                "consensus_score": ticker.consensus_score if ticker else None,
                "consensus_label": ticker.consensus_label if ticker else None,
            }
        )
    return JSONResponse(lists)


@app.post("/api/watchlists/{list_name}/tickers")
def add_watchlist_ticker(
    list_name: str, payload: dict, db: Session = Depends(get_db)
) -> JSONResponse:
    symbol = (payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol required")
    exists = db.scalar(
        select(WatchlistItem.id).where(
            WatchlistItem.list_name == list_name,
            WatchlistItem.ticker_symbol == symbol,
        )
    )
    if not exists:
        db.add(WatchlistItem(list_name=list_name, ticker_symbol=symbol))
        db.commit()
        # Ticker refresh is non-critical; run after commit so failure doesn't lose the add.
        try:
            refresh_ticker(db, symbol)
            db.commit()
        except Exception:
            logger.warning("Ticker refresh failed for %s", symbol)
    return JSONResponse({"ok": True})


@app.delete("/api/watchlists/{list_name}/tickers/{symbol}")
def remove_watchlist_ticker(
    list_name: str, symbol: str, db: Session = Depends(get_db)
) -> JSONResponse:
    row = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.list_name == list_name,
            WatchlistItem.ticker_symbol == symbol.upper(),
        )
    )
    if row:
        db.delete(row)
        db.commit()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ industries
@app.get("/api/industries")
def list_industries(db: Session = Depends(get_db)) -> JSONResponse:
    industries = db.execute(select(Industry).order_by(Industry.name)).scalars().all()
    out = []
    for ind in industries:
        active = [s for s in ind.sources if s.status == SourceStatus.active.value]
        avg_score = (
            round(sum(s.vetting_score for s in active) / len(active), 1) if active else 0
        )
        signal_count = db.scalar(
            select(func.count(Signal.id)).where(Signal.industry_id == ind.id)
        )
        out.append(
            {
                "id": ind.id,
                "name": ind.name,
                "group": ind.group,
                "source_count": len(active),
                "target": ind.target_source_count,
                "avg_score": avg_score,
                "signal_count": signal_count or 0,
            }
        )
    return JSONResponse(out)


@app.post("/api/industries/{industry_id}/discover")
def discover_industry(industry_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    ind = db.get(Industry, industry_id)
    if ind is None:
        raise HTTPException(404, "industry not found")
    result = discovery_engine.discover_for_industry(db, ind, limit=15)
    db.commit()
    return JSONResponse(result)


# ---------------------------------------------------------------------- trends
@app.get("/api/trends")
def get_trends(days: int = 90, db: Session = Depends(get_db)) -> JSONResponse:
    """Signal analytics: distributions (direction/type/ticker/sector) + time series.

    Distributions read well even with little history; the daily-volume series gives
    the time dimension without the sparse weekly-bucket bars the old chart used.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(Signal, Source.type)
        .join(Item, Signal.item_id == Item.id)
        .join(Source, Item.source_id == Source.id)
        .where(Signal.created_at >= cutoff)
    ).all()

    direction_total = {"bullish": 0, "bearish": 0, "neutral": 0}
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    ticker_stats: dict[str, dict] = {}
    sector_stats: dict[str, dict] = {}
    vol_by_day: dict[str, int] = {}
    topic_weeks: dict[str, dict[str, int]] = {}

    for sig, stype in rows:
        d = sig.direction or "neutral"
        direction_total[d] = direction_total.get(d, 0) + 1
        by_type[sig.signal_type] = by_type.get(sig.signal_type, 0) + 1
        by_source[stype] = by_source.get(stype, 0) + 1
        if sig.created_at:
            day = _as_aware(sig.created_at).strftime("%Y-%m-%d")
            vol_by_day[day] = vol_by_day.get(day, 0) + 1
            week = sig.created_at.strftime("%Y-W%U")
        else:
            week = "—"
        try:
            ents = json.loads(sig.entities_json or "{}")
        except json.JSONDecodeError:
            ents = {}
        ind = (sig.item.source.industries[0].name
               if sig.item and sig.item.source and sig.item.source.industries else "Other")
        sec = sector_stats.setdefault(ind, {"bullish": 0, "bearish": 0, "total": 0})
        sec[d] = sec.get(d, 0) + 1
        sec["total"] += 1
        for tk in ents.get("tickers", []):
            sym = str(tk).upper()
            ts = ticker_stats.setdefault(sym, {"bullish": 0, "bearish": 0, "total": 0})
            ts[d] = ts.get(d, 0) + 1
            ts["total"] += 1
        for tech in ents.get("technologies", []):
            topic_weeks.setdefault(tech, {})
            topic_weeks[tech][week] = topic_weeks[tech].get(week, 0) + 1

    top_tickers = sorted(
        [{"ticker": k, **v} for k, v in ticker_stats.items()],
        key=lambda x: x["total"], reverse=True)[:12]
    top_sectors = sorted(
        [{"sector": k, **v} for k, v in sector_stats.items()],
        key=lambda x: x["total"], reverse=True)[:10]
    volume_series = [{"date": d, "count": vol_by_day[d]} for d in sorted(vol_by_day)]

    return JSONResponse({
        "direction_total": direction_total,
        "by_type": by_type,
        "by_source": by_source,
        "top_tickers": top_tickers,
        "top_sectors": top_sectors,
        "volume_series": volume_series,
        "topics": topic_weeks,
        "total": sum(direction_total.values()),
    })


# ---------------------------------------------------------------- theme shifts
_PERIOD_DAYS = {"week": 7, "month": 30, "3month": 90, "6month": 180, "year": 365}


@app.get("/api/theme-shifts")
def get_theme_shifts(period: str = "month", db: Session = Depends(get_db)) -> JSONResponse:
    """Rising/falling themes over a period, split first-half vs second-half."""
    days = _PERIOD_DAYS.get(period, 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    mid = cutoff + timedelta(days=days // 2)

    signals = db.execute(
        select(Signal).where(Signal.created_at >= cutoff)
    ).scalars().all()

    theme_stats: dict[str, dict] = {}
    week_counts: dict[str, dict[str, int]] = {}

    for sig in signals:
        try:
            ents = json.loads(sig.entities_json or "{}")
        except json.JSONDecodeError:
            ents = {}
        themes = list(ents.get("technologies") or []) + list(ents.get("companies") or [])
        half = "recent" if sig.created_at and sig.created_at >= mid else "prior"
        week = sig.created_at.strftime("%Y-W%U") if sig.created_at else "unknown"
        direction = sig.direction or "neutral"

        for theme in themes:
            theme = str(theme).strip()
            if len(theme) < 2:
                continue
            if theme not in theme_stats:
                theme_stats[theme] = {
                    "recent": 0, "prior": 0,
                    "bullish": 0, "bearish": 0, "neutral": 0,
                }
            theme_stats[theme][half] += 1
            theme_stats[theme][direction] = theme_stats[theme].get(direction, 0) + 1
            week_counts.setdefault(theme, {})
            week_counts[theme][week] = week_counts[theme].get(week, 0) + 1

    results = []
    for theme, stats in theme_stats.items():
        total = stats["recent"] + stats["prior"]
        change = stats["recent"] - stats["prior"]
        results.append({
            "theme": theme,
            "total": total,
            "recent": stats["recent"],
            "prior": stats["prior"],
            "change": change,
            "bullish": stats["bullish"],
            "bearish": stats["bearish"],
            "neutral": stats["neutral"],
            "sentiment": round((stats["bullish"] - stats["bearish"]) / total, 2) if total else 0,
        })

    results.sort(key=lambda x: x["total"], reverse=True)
    top50 = results[:50]

    rising = sorted(
        [r for r in top50 if r["change"] > 0], key=lambda x: x["change"], reverse=True
    )[:15]
    falling = sorted(
        [r for r in top50 if r["change"] < 0], key=lambda x: x["change"]
    )[:15]

    # Weekly timeline for top 10 themes only (to keep payload small).
    top_theme_names = {r["theme"] for r in results[:10]}
    timeline = {k: v for k, v in week_counts.items() if k in top_theme_names}

    return JSONResponse({
        "period": period,
        "days": days,
        "total_signals": len(signals),
        "rising": rising,
        "falling": falling,
        "all": top50,
        "timeline": timeline,
    })


# ---------------------------------------------------------------- shift alerts
@app.get("/api/shift-alerts")
def get_shift_alerts(limit: int = 50, db: Session = Depends(get_db)) -> JSONResponse:
    alerts = db.execute(
        select(ShiftAlert).order_by(ShiftAlert.created_at.desc()).limit(limit)
    ).scalars().all()
    return JSONResponse([
        {
            "symbol": a.symbol,
            "from_score": round(a.from_score, 3),
            "to_score": round(a.to_score, 3),
            "recent_total": a.recent_total,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "direction": "bullish" if a.to_score > 0 else "bearish",
        }
        for a in alerts
    ])


# ----------------------------------------------------------------- brief / intel
_BRIEF_SYSTEM = (
    "You are a senior equity research analyst writing a pre-market intelligence brief. "
    "Given a JSON list of investment signals, write 3–5 concise bullet points (one per line, "
    "start each with •) covering: the most actionable developments, which sectors/tickers "
    "are showing momentum, and one risk or headwind to watch. "
    "Be specific — name tickers and technologies. No preamble, no sign-off. "
    "If signals are sparse, write what you can confidently say."
)


@app.get("/api/brief")
def get_brief(
    hours: int = 48,
    industry: str | None = None,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Structured investment brief from recent signals — no LLM, instant.

    When ``industry`` (name) is given, the brief is scoped to that industry's
    signals only.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    query = select(Signal).where(Signal.created_at >= cutoff)

    industry_name = None
    if industry:
        ind = db.scalar(select(Industry).where(Industry.name == industry))
        if ind:
            query = query.where(Signal.industry_id == ind.id)
            industry_name = ind.name

    signals = db.execute(
        query.order_by(Signal.confidence.desc()).limit(100)
    ).scalars().all()

    total = len(signals)
    by_dir: dict[str, int] = {"bullish": 0, "bearish": 0, "neutral": 0}
    by_type: dict[str, int] = {}
    by_sector: dict[str, dict] = {}
    by_ticker: dict[str, dict] = {}
    top_actionable: list[dict] = []

    for sig in signals:
        by_dir[sig.direction] = by_dir.get(sig.direction, 0) + 1
        by_type[sig.signal_type] = by_type.get(sig.signal_type, 0) + 1
        try:
            ents = json.loads(sig.entities_json or "{}")
        except json.JSONDecodeError:
            ents = {}
        industry = (
            sig.item.source.industries[0].name
            if sig.item and sig.item.source and sig.item.source.industries
            else "Other"
        )
        if industry not in by_sector:
            by_sector[industry] = {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0}
        by_sector[industry][sig.direction] = by_sector[industry].get(sig.direction, 0) + 1
        by_sector[industry]["total"] += 1
        for ticker in ents.get("tickers", []):
            sym = str(ticker).upper()
            if sym not in by_ticker:
                by_ticker[sym] = {"bullish": 0, "bearish": 0, "total": 0}
            by_ticker[sym][sig.direction] = by_ticker[sym].get(sig.direction, 0) + 1
            by_ticker[sym]["total"] += 1

    # Top actionable: high-confidence, directional
    top_actionable = [
        {
            "id": s.id,
            "summary": s.summary,
            "signal_type": s.signal_type,
            "direction": s.direction,
            "confidence": round(s.confidence, 2),
            "speculative": s.speculative,
            "entities": json.loads(s.entities_json or "{}") if s.entities_json else {},
            "url": s.item.url if s.item else "",
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in signals
        if s.direction != "neutral" and s.confidence >= 0.65
    ][:12]

    # Sector pulse sorted by total signal activity
    sector_pulse = sorted(
        [{"sector": k, **v} for k, v in by_sector.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:12]

    # Top tickers by signal count
    hot_tickers = sorted(
        [{"ticker": k, **v} for k, v in by_ticker.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    # Under the radar: top signals by alpha score (non-consensus, score >= 6)
    scored = score_signals(signals, db, cache_key="brief")
    scored.sort(key=lambda x: x[1]["total"], reverse=True)
    under_the_radar = [
        _signal_dict(s, a)
        for s, a in scored
        if a["total"] >= 6 and s.confidence >= 0.6
    ][:5]

    # Item count stats
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_items = db.scalar(select(func.count(Item.id)).where(Item.ingested_at >= today_start)) or 0
    total_items = db.scalar(select(func.count(Item.id))) or 0

    return JSONResponse({
        "hours": hours,
        "industry": industry_name,
        "generated": datetime.now(timezone.utc).isoformat(),
        "signal_count": total,
        "today_items": today_items,
        "total_items": total_items,
        "by_direction": by_dir,
        "by_type": by_type,
        "top_actionable": top_actionable,
        "sector_pulse": sector_pulse,
        "hot_tickers": hot_tickers,
        "under_the_radar": under_the_radar,
        "ai_synthesis": None,
    })


@app.post("/api/brief/synthesize")
def synthesize_brief(
    industry: str | None = None, db: Session = Depends(get_db)
) -> JSONResponse:
    """Ask Claude to narrate the top signals as an investment brief. Costs ~$0.002."""
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    sq = select(Signal).where(Signal.created_at >= cutoff, Signal.direction != "neutral")
    if industry:
        ind = db.scalar(select(Industry).where(Industry.name == industry))
        if ind:
            sq = sq.where(Signal.industry_id == ind.id)
    signals = db.execute(
        sq.order_by(Signal.confidence.desc()).limit(20)
    ).scalars().all()
    if not signals:
        return JSONResponse({"synthesis": "No directional signals in the last 48 hours to synthesize."})
    payload = [
        {
            "type": s.signal_type,
            "direction": s.direction,
            "confidence": round(s.confidence, 2),
            "summary": s.summary,
            "entities": json.loads(s.entities_json or "{}"),
        }
        for s in signals
    ]
    text = claude.complete(
        system=_BRIEF_SYSTEM,
        user=json.dumps(payload, indent=2),
        model=config.SONNET_MODEL,
        max_tokens=400,
        cache_system=False,
    )
    return JSONResponse({"synthesis": text or "Unable to synthesize at this time."})


# ----------------------------------------------------------------------- macro
@app.get("/api/macro")
def get_macro_endpoint() -> JSONResponse:
    """Macro regime indicators from FRED (yield curve, CPI, unemployment, VIX)."""
    from app.processing.macro import get_macro
    return JSONResponse(get_macro())


@app.get("/api/composites")
def get_composites(db: Session = Depends(get_db)) -> JSONResponse:
    """Three weighted composite indices in [-100, +100] for the analytics view:
    Macro Outlook, Market Consensus, and Signal Momentum."""
    from app.processing.macro import get_macro

    # 1. Macro Outlook — weighted FRED composite.
    macro = get_macro()
    macro_outlook = macro.get("outlook", {"score": 0, "label": "neutral", "components": []})

    # 2. Market Consensus — average blended consensus across watchlist tickers.
    syms = {r[0] for r in db.execute(select(WatchlistItem.ticker_symbol)).all()}
    tks = db.execute(
        select(Ticker).where(Ticker.symbol.in_(syms), Ticker.consensus_score.isnot(None))
    ).scalars().all() if syms else []
    if tks:
        avg = sum(t.consensus_score for t in tks) / len(tks)
        consensus = {
            "score": round(avg * 100, 1),
            "label": "bullish" if avg > 0.15 else "bearish" if avg < -0.15 else "mixed",
            "n": len(tks),
            "bull": sum(1 for t in tks if (t.consensus_score or 0) > 0.15),
            "bear": sum(1 for t in tks if (t.consensus_score or 0) < -0.15),
        }
    else:
        consensus = {"score": 0, "label": "no data", "n": 0, "bull": 0, "bear": 0}

    # 3. Signal Momentum — confidence-weighted net direction over the last 7 days.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    sigs = db.execute(select(Signal).where(Signal.created_at >= cutoff)).scalars().all()
    num = den = 0.0
    bull = bear = 0
    for s in sigs:
        w = s.confidence or 0.5
        if s.direction == "bullish":
            num += w; bull += 1
        elif s.direction == "bearish":
            num -= w; bear += 1
        den += w
    momentum = {
        "score": round((num / den) * 100, 1) if den else 0.0,
        "label": "bullish" if num > 0 else "bearish" if num < 0 else "flat",
        "n": len(sigs), "bull": bull, "bear": bear,
    }

    return JSONResponse({
        "macro_outlook": macro_outlook,
        "market_consensus": consensus,
        "signal_momentum": momentum,
    })


@app.get("/api/pulse")
def get_pulse(db: Session = Depends(get_db)) -> JSONResponse:
    """Per-watchlist-ticker alt-data: Wikipedia attention + GDELT news tone."""
    from app.processing.attention import get_attention_batch
    from app.processing.news_tone import get_tone_batch

    # Resolve watchlist tickers to company names for the attention/tone lookups.
    rows = db.execute(select(WatchlistItem.ticker_symbol)).all()
    symbols = sorted({r[0] for r in rows})[:12]  # cap for rate limits
    tickers = {
        t.symbol: t for t in
        db.execute(select(Ticker).where(Ticker.symbol.in_(symbols))).scalars().all()
    }
    # Use ticker name when known, else the symbol itself.
    name_of = {s: (tickers[s].name or s) if s in tickers else s for s in symbols}
    queries = list({name_of[s] for s in symbols})

    attention = get_attention_batch(queries)
    tone = get_tone_batch(queries)

    out = []
    for s in symbols:
        q = name_of[s]
        a = attention.get(q) or {}
        t = tone.get(q) or {}
        out.append({
            "symbol": s, "name": q,
            "attention_ratio": a.get("attention_ratio"),
            "attention_trend": a.get("trend"),
            "tone": t.get("avg_tone"),
            "tone_trend": t.get("tone_trend"),
            "news_vol_spike": t.get("vol_spike"),
        })
    # Rank: attention spikes + news spikes first.
    out.sort(key=lambda x: max(x.get("attention_ratio") or 0, x.get("news_vol_spike") or 0), reverse=True)
    return JSONResponse(out)


# --------------------------------------------------------- funds (13F) + short
@app.get("/api/funds")
def get_funds() -> JSONResponse:
    """What famous hedge funds hold and recently bought/sold (SEC 13F)."""
    from app.processing.funds import get_all_funds
    return JSONResponse(get_all_funds())


@app.get("/api/funds/{cik}")
def get_fund_detail(cik: int) -> JSONResponse:
    """One fund's full top holdings + quarter-over-quarter position changes."""
    from app.processing.funds import get_fund_holdings, get_fund_changes
    return JSONResponse({
        "holdings": get_fund_holdings(cik),
        "changes": get_fund_changes(cik),
    })


@app.get("/api/fund-consensus")
def get_fund_consensus() -> JSONResponse:
    """Cross-fund overlap from 13F filings — what the famous funds collectively hold.

    Auto-refreshes each quarter as new 13F-HRs land. Non-blocking: serves the
    cache and warms in the background (a cold scrape over hundreds of funds takes
    minutes), returning {"computing": true} until the first build finishes."""
    from app.processing.funds import get_consensus_cached
    return JSONResponse(get_consensus_cached())


@app.get("/api/insider-clusters")
def get_insider_clusters() -> JSONResponse:
    """Insider cluster buys — stocks where multiple insiders bought at once.

    Clustered Form 4 purchases are a high-conviction bullish signal. Scraped from
    OpenInsider (6h cache, graceful fallback)."""
    from app.processing.openinsider import get_cluster_buys
    return JSONResponse(get_cluster_buys())


@app.get("/api/short-volume")
def get_short_volume_endpoint(db: Session = Depends(get_db)) -> JSONResponse:
    """FINRA daily short-volume % for watchlist tickers."""
    from app.processing.short_volume import get_short_volume
    symbols = sorted({r[0] for r in db.execute(select(WatchlistItem.ticker_symbol)).all()})
    return JSONResponse(get_short_volume(symbols))


@app.get("/api/ftd")
def get_ftd_endpoint(db: Session = Depends(get_db)) -> JSONResponse:
    """SEC Fails-to-Deliver totals for watchlist tickers (squeeze-fuel proxy)."""
    from app.processing.ftd import get_ftd
    symbols = sorted({r[0] for r in db.execute(select(WatchlistItem.ticker_symbol)).all()})
    return JSONResponse(get_ftd(symbols))


# ------------------------------------------------------------- unusual volume
@app.get("/api/unusual-volume")
def get_unusual_volume(min_rvol: float = 1.5, db: Session = Depends(get_db)) -> JSONResponse:
    """Tickers trading on abnormal volume (RVOL = today / 20-day avg)."""
    tickers = db.execute(
        select(Ticker).where(Ticker.rvol.isnot(None), Ticker.rvol >= min_rvol)
        .order_by(Ticker.rvol.desc())
    ).scalars().all()
    return JSONResponse([
        {
            "symbol": t.symbol, "name": t.name, "rvol": t.rvol,
            "change_pct": t.change_pct, "change_5d_pct": t.change_5d_pct,
            "price": t.price, "consensus_label": t.consensus_label,
        }
        for t in tickers
    ])


# -------------------------------------------------------------- conviction page
_CONVICTION_SYSTEM = (
    "You are the head of research at a hedge fund writing the daily conviction memo. "
    "You are given an aggregated intelligence packet: classified news signals, insider "
    "(Form 4) trades, congressional trades, market-consensus scores, unusual-volume "
    "tickers, trending themes, and prediction-market forecasts.\n\n"
    "Identify the 1-5 HIGHEST-CONVICTION, most NON-CONSENSUS, ACTIONABLE ideas the data "
    "collectively points to. Prioritize ideas where MULTIPLE independent data sources "
    "corroborate (e.g. insider selling + unusual volume + bearish consensus), and where "
    "the read diverges from the crowd. Skip anything obvious or already priced in.\n\n"
    "Return ONLY a JSON object: {\"convictions\": [ {\n"
    "  \"rank\": <int 1=highest>,\n"
    "  \"title\": <short imperative, e.g. 'Short AVGO into the volume break'>,\n"
    "  \"direction\": <\"long\"|\"short\"|\"watch\">,\n"
    "  \"conviction\": <\"high\"|\"medium\"|\"speculative\">,\n"
    "  \"tickers\": [<symbols>],\n"
    "  \"thesis\": <2-3 sentences: what's happening and why it matters>,\n"
    "  \"evidence\": [<3-5 specific data points from the packet that support it>],\n"
    "  \"non_consensus\": <1 sentence: why this is NOT what the crowd believes>,\n"
    "  \"risk\": <1 sentence: the main thing that would make this wrong>\n"
    "} ] }\n"
    "Be specific and cite the actual numbers. If the data is thin, return fewer ideas. "
    "No preamble."
)


def _conviction_packet(db: Session) -> dict:
    """Assemble the structured intelligence packet fed to the synthesizer."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=72)

    recent = db.execute(
        select(Signal).where(Signal.created_at >= cutoff)
        .order_by(Signal.confidence.desc())
    ).scalars().all()

    # Non-consensus signals (alpha >= 7)
    scored = score_signals(recent, db, cache_key="conviction")
    scored.sort(key=lambda x: x[1]["total"], reverse=True)
    non_consensus = [
        {"summary": s.summary, "direction": s.direction,
         "alpha": a["total"], "label": a["label"],
         "tickers": json.loads(s.entities_json or "{}").get("tickers", [])}
        for s, a in scored if a["total"] >= 7
    ][:12]

    # Smart money
    smart = _smart_money_data(db, days=14)

    # Unusual volume
    uv = db.execute(
        select(Ticker).where(Ticker.rvol.isnot(None), Ticker.rvol >= 1.5)
        .order_by(Ticker.rvol.desc())
    ).scalars().all()
    unusual_volume = [
        {"ticker": t.symbol, "rvol": t.rvol, "change_pct": t.change_pct,
         "change_5d_pct": t.change_5d_pct, "consensus": t.consensus_label}
        for t in uv
    ][:12]

    # Rising themes (30d)
    theme_sigs = db.execute(
        select(Signal).where(Signal.created_at >= now - timedelta(days=30))
    ).scalars().all()
    mid = now - timedelta(days=7)
    tstats: dict[str, dict] = {}
    for s in theme_sigs:
        try:
            ents = json.loads(s.entities_json or "{}")
        except json.JSONDecodeError:
            ents = {}
        for th in (ents.get("technologies") or []) + (ents.get("companies") or []):
            th = str(th).strip()
            if len(th) < 2:
                continue
            d = tstats.setdefault(th, {"recent": 0, "prior": 0})
            d["recent" if (s.created_at and _as_aware(s.created_at) >= mid) else "prior"] += 1
    rising = sorted(
        [{"theme": k, "change": v["recent"] - v["prior"]} for k, v in tstats.items()],
        key=lambda x: x["change"], reverse=True,
    )[:8]

    # Prediction conflicts (markets disagreeing)
    pred_items = db.execute(
        select(Item).where(Item.media_type == "prediction")
        .order_by(Item.ingested_at.desc()).limit(200)
    ).scalars().all()
    preds = [_parse_pred(it) for it in pred_items]
    conflicts = []
    used = set()
    for i, a in enumerate(preds):
        if i in used or a["probability"] is None:
            continue
        for j in range(i + 1, len(preds)):
            if j in used or preds[j]["probability"] is None:
                continue
            b = preds[j]
            if a["source"] != b["source"] and len(a["keywords"] & b["keywords"]) >= 2:
                spread = abs(a["probability"] - b["probability"])
                if spread > 20:
                    conflicts.append({
                        "question": a["question"],
                        "spread": spread,
                        "prices": f"{a['source']} {a['probability']}% vs {b['source']} {b['probability']}%",
                    })
                used.add(j)
                break

    # Macro regime backdrop (best-effort)
    try:
        from app.processing.macro import get_macro
        macro = get_macro()
    except Exception:
        macro = {"regime": "unknown", "indicators": []}

    return {
        "generated": now.isoformat(),
        "macro_regime": macro.get("regime"),
        "macro_indicators": macro.get("indicators", [])[:5],
        "non_consensus_signals": non_consensus,
        "smart_money_divergent": smart["divergent"],
        "insider_trades": [t["summary"] for t in smart["insider_trades"][:10]],
        "politician_trades": [t["summary"] for t in smart["politician_trades"][:10]],
        "institutional_stakes": [t["summary"] for t in smart.get("institutional_trades", [])[:8]],
        "unusual_volume": unusual_volume,
        "rising_themes": rising,
        "prediction_conflicts": conflicts[:6],
    }


@app.get("/api/conviction")
def get_conviction(db: Session = Depends(get_db)) -> JSONResponse:
    """The data packet only (fast, no LLM) — for inspection / the evidence panel."""
    return JSONResponse(_conviction_packet(db))


@app.post("/api/conviction/synthesize")
def synthesize_conviction(db: Session = Depends(get_db)) -> JSONResponse:
    """Aggregate everything and ask Claude for the 1-5 highest-conviction plays."""
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    packet = _conviction_packet(db)
    has_data = any(packet[k] for k in (
        "non_consensus_signals", "smart_money_divergent", "unusual_volume",
        "insider_trades", "politician_trades",
    ))
    if not has_data:
        return JSONResponse({"convictions": [], "packet": packet,
                             "note": "Not enough data yet — run ingestion and ticker refresh."})
    raw = claude.complete(
        system=_CONVICTION_SYSTEM,
        user=json.dumps(packet, indent=2, default=str),
        model=config.SONNET_MODEL,
        max_tokens=4096,
        cache_system=False,
    )
    data = claude.extract_json(raw)
    return JSONResponse({
        "convictions": data.get("convictions", []),
        "packet": packet,
        "generated": packet["generated"],
    })


# -------------------------------------------------------------- investor lens
def _ticker_dossier(db: Session, ticker: str) -> dict:
    """Assemble everything we know about one ticker for the investor panel."""
    sym = ticker.upper()
    # Signal.created_at is stored naive (SQLite) — compare against a naive utc cutoff.
    cutoff = datetime.utcnow() - timedelta(days=21)
    sigs = db.execute(
        select(Signal).where(Signal.created_at >= cutoff).order_by(Signal.created_at.desc())
    ).scalars().all()
    mine = []
    for s in sigs:
        try:
            tks = {str(t).upper() for t in json.loads(s.entities_json or "{}").get("tickers", [])}
        except json.JSONDecodeError:
            tks = set()
        if sym in tks:
            mine.append({
                "summary": s.summary, "direction": s.direction,
                "type": s.signal_type, "signal_type": s.signal_type,
                "confidence": round(s.confidence, 2),
                "entities": json.loads(s.entities_json or "{}"),
                "url": s.item.url if s.item else "",
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })
    flow = next((f for f in _smart_money_data(db, 21)["flow"] if f["ticker"] == sym), None)
    tk = db.get(Ticker, sym)
    latest_signal_at = max(
        (m["created_at"] for m in mine if m.get("created_at")), default=None
    )
    return {
        "ticker": sym,
        "latest_signal_at": latest_signal_at,
        "name": tk.name if tk else None,
        "price": tk.price if tk else None,
        "consensus": tk.consensus_label if tk else None,
        "consensus_score": tk.consensus_score if tk else None,
        "analyst_rating": tk.analyst_rating if tk else None,
        "analyst_target": tk.analyst_target if tk else None,
        "short_interest_pct": tk.short_interest_pct if tk else None,
        "rvol": tk.rvol if tk else None,
        "change_pct": tk.change_pct if tk else None,
        "week52_high": tk.week52_high if tk else None,
        "week52_low": tk.week52_low if tk else None,
        "smart_money_flow": flow,
        "recent_signals": mine[:15],
        "signal_count": len(mine),
    }


def _ticker_full(db: Session, symbol: str, *, enrich: bool = True) -> dict:
    """The complete dossier for one ticker: on-demand enrichment + everything we
    aggregate (price/consensus/RVOL, SEC fundamentals, smart-money flow, recent
    signals, short volume, FTDs, theses, portfolio membership). Shared by the
    Ticker Dossier, the Ask aggregator, and the Investor Lens.
    """
    from app.processing import fundamentals
    sym = symbol.upper()
    # Keep market data current: re-pull unless refreshed in the last 15 min. Works
    # off the un-throttled Yahoo v8 chart even for off-watchlist names (e.g. AAOI).
    if enrich:
        tk = db.get(Ticker, sym)
        stale = tk is None or tk.updated_at is None or (
            datetime.utcnow() - tk.updated_at.replace(tzinfo=None)
        ) > timedelta(minutes=15)
        if stale:
            try:
                refresh_ticker(db, sym)
                db.commit()
            except Exception:
                logger.warning("refresh_ticker failed for %s", sym, exc_info=True)
                db.rollback()
    d = _ticker_dossier(db, sym)
    d["fundamentals"] = fundamentals.get_fundamentals(sym)
    tkr = db.get(Ticker, sym)
    d["price_updated_at"] = tkr.updated_at.isoformat() if tkr and tkr.updated_at else None
    # Short-volume + fails-to-deliver (best-effort — a transient FINRA/SEC failure
    # must not empty the dossier).
    d["short_volume_pct"] = None
    d["ftd_fails"] = None
    try:
        from app.processing.short_volume import get_short_volume
        sv = get_short_volume([sym]).get("tickers", [])
        d["short_volume_pct"] = sv[0]["short_pct"] if sv else None
    except Exception:
        logger.info("short volume fetch failed for %s", sym)
    try:
        from app.processing.ftd import get_ftd
        ftd = get_ftd([sym]).get("tickers", [])
        d["ftd_fails"] = ftd[0]["fails"] if ftd else None
    except Exception:
        logger.info("ftd fetch failed for %s", sym)
    # Theses touching this ticker
    theses = db.execute(select(Thesis)).scalars().all()
    d["theses"] = [
        {"id": t.id, "title": t.title, "direction": t.direction, "status": t.status}
        for t in theses
        if sym in {x.strip().upper() for x in (t.tickers or "").split(",")}
    ]
    # Portfolio membership
    held = portfolio_ingest.held_tickers(db)
    d["held"] = sym in held
    if d["held"]:
        d["weight_pct"] = next(
            (h["weight_pct"] for h in portfolio_ingest.holdings_list(db)
             if (h.get("yfinance_ticker") or h.get("ticker", "")).upper() == sym), None
        )
    return d


@app.get("/api/ticker/{symbol}")
def get_ticker(symbol: str, db: Session = Depends(get_db)) -> JSONResponse:
    """Everything we know about one ticker, in one payload (the Ticker Dossier)."""
    return JSONResponse(_ticker_full(db, symbol))


@app.post("/api/investor-lens")
def investor_lens(payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    """Judge a ticker through legendary-investor frameworks."""
    from app.processing import investor_lens as lens
    from app.processing import fundamentals
    raw = (payload.get("ticker") or "").strip()
    if not raw:
        raise HTTPException(400, "ticker required")
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    # Accept a company name too, and resolve it to a real symbol (e.g. "harmonic" -> HLIT).
    sym = fundamentals.resolve_symbol(raw) or raw.upper()
    # Refresh the Ticker row so the panel reads CURRENT price/volume/consensus —
    # the lens is a user-clicked, low-frequency action, so we re-pull unless it
    # was refreshed in the last 15 min (avoids redundant fetches on rapid re-runs).
    # Works via the un-throttled Yahoo v8 chart even when yfinance 429s.
    tk = db.get(Ticker, sym)
    stale = tk is None or tk.updated_at is None or (
        datetime.utcnow() - tk.updated_at.replace(tzinfo=None)
    ) > timedelta(minutes=15)
    if stale:
        try:
            refresh_ticker(db, sym)
            db.commit()
        except Exception:
            logger.warning("refresh_ticker failed for %s", sym, exc_info=True)
            db.rollback()
    dossier = _ticker_dossier(db, sym)
    dossier["fundamentals"] = fundamentals.get_fundamentals(sym)
    # Surface data freshness so the UI can show how current each input is.
    tkr = db.get(Ticker, sym)
    dossier["price_updated_at"] = (
        tkr.updated_at.isoformat() if tkr and tkr.updated_at else None
    )
    result = lens.analyze(sym, dossier)
    result["ticker"] = sym
    result["dossier"] = dossier
    return JSONResponse(result)


# ------------------------------------------------------------------- portfolio
def _enrich_portfolio_tickers() -> None:
    """Background: enrich the now-tracked holdings (price/consensus via the same
    path the 6h cron uses) and warm the SEC fundamentals cache, so a freshly
    synced portfolio is immediately queryable instead of waiting for the cron."""
    from app.database import session_scope
    from app.processing.tickers import refresh_watchlist_tickers
    from app.processing import fundamentals
    try:
        with session_scope() as s:
            refresh_watchlist_tickers(s)
            held = list(portfolio_ingest.held_tickers(s))
        for sym in held:
            try:
                fundamentals.get_fundamentals(sym)  # warms the 24h cache
            except Exception:
                logger.debug("fundamentals warm failed for %s", sym, exc_info=True)
    except Exception:
        logger.warning("portfolio enrichment failed", exc_info=True)


@app.post("/api/portfolio/import")
def import_portfolio(
    background: BackgroundTasks, db: Session = Depends(get_db)
) -> JSONResponse:
    """Re-read the LCO holdings snapshot file into the DB (replace-all), auto-track
    the holdings, and kick off background enrichment."""
    try:
        counts = portfolio_ingest.import_holdings(db)
    except FileNotFoundError:
        raise HTTPException(
            404, f"holdings snapshot not found at {config.LCO_HOLDINGS_PATH}"
        )
    background.add_task(_enrich_portfolio_tickers)
    counts["enriching"] = True
    return JSONResponse(counts)


@app.get("/api/portfolio")
def get_portfolio(db: Session = Depends(get_db)) -> JSONResponse:
    """The current portfolio: meta + equity holdings enriched with our own
    ticker data (price/consensus/rvol) and recent-signal flow."""
    holdings = portfolio_ingest.holdings_list(db)
    meta = portfolio_ingest.portfolio_meta(db) or {}

    # Recent signals once, then bucket per held ticker (naive UTC cutoff to match
    # how Signal.created_at is stored — see CLAUDE.md datetime trap).
    cutoff = datetime.utcnow() - timedelta(days=21)
    recent = db.execute(
        select(Signal).where(Signal.created_at >= cutoff)
    ).scalars().all()
    counts: dict[str, dict[str, int]] = {}
    for s in recent:
        try:
            tks = {str(t).upper() for t in json.loads(s.entities_json or "{}").get("tickers", [])}
        except json.JSONDecodeError:
            tks = set()
        for t in tks:
            c = counts.setdefault(t, {"count": 0, "bull": 0, "bear": 0})
            c["count"] += 1
            if s.direction == "bullish":
                c["bull"] += 1
            elif s.direction == "bearish":
                c["bear"] += 1

    enriched = []
    for h in holdings:
        sym = (h.get("yfinance_ticker") or h.get("ticker") or "").upper()
        tk = db.get(Ticker, sym) if sym else None
        c = counts.get(sym, {"count": 0, "bull": 0, "bear": 0})
        enriched.append({
            **h,
            "ticker_price": tk.price if tk else None,
            "consensus_label": tk.consensus_label if tk else None,
            "consensus_score": tk.consensus_score if tk else None,
            "rvol": tk.rvol if tk else None,
            "signal_count": c["count"],
            "net_signal": c["bull"] - c["bear"],
        })
    enriched.sort(key=lambda x: x.get("weight_pct") or 0.0, reverse=True)

    meta = {**meta, "holdings": len(holdings), "equity": len(holdings)}
    return JSONResponse({"meta": meta, "holdings": enriched})


# ------------------------------------------------------------------------- ask
def _web_read(ticker: str, question: str) -> dict | None:
    """A live web read on the ticker via Claude's built-in web_search tool (uses
    the existing Anthropic key — no separate search vendor). None if unavailable."""
    if not claude.is_configured():
        return None
    try:
        prompt = (
            f"Search the web for current information and give a concise analyst read "
            f"on {ticker} relevant to this question: {question}\nCover competitive "
            f"landscape, recent catalysts, and key risks. 4-6 bullets, include "
            f"figures/dates where possible."
        )
        r = claude.web_search(prompt, max_tokens=900)
        text = (r or {}).get("text")
        if not text:
            return None
        return {
            "summary": text[:2000],
            "citations": [c.get("url") for c in (r.get("citations") or [])[:6] if c.get("url")],
            "source": "Claude web_search",
        }
    except Exception:
        logger.warning("web read failed for %s", ticker, exc_info=True)
        return None


def _compact_dossier(d: dict) -> dict:
    """Token-bounded projection of a full ticker dossier for the LLM context —
    keeps fundamentals/flow/positioning/theses, trims recent_signals."""
    keys = (
        "ticker", "name", "price", "change_pct", "consensus", "consensus_score",
        "rvol", "week52_high", "week52_low", "analyst_rating", "analyst_target",
        "short_interest_pct", "short_volume_pct", "ftd_fails", "smart_money_flow",
        "fundamentals", "theses", "held", "weight_pct", "signal_count",
    )
    out = {k: d.get(k) for k in keys if d.get(k) is not None}
    out["recent_signals"] = [
        {"summary": s.get("summary"), "direction": s.get("direction"),
         "type": s.get("type"), "date": (s.get("created_at") or "")[:10],
         "confidence": s.get("confidence")}
        for s in (d.get("recent_signals") or [])[:6]
    ]
    return out


@app.post("/api/ask")
def ask(payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    """RAG Q&A over our own data. Retrieval is free (local FTS5 + filters); only
    the final answer costs tokens, bounded by a small retrieved context."""
    from app.processing import rag

    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    claude.start_usage()  # tally tokens across the lens + web + final calls

    # Free local retrieval over the signal corpus...
    signals = rag.retrieve(db, question, k=15)
    # ...plus a FULL aggregated dossier for every ticker named (watchlist or not).
    # _ticker_full enriches on demand, so even an off-watchlist name like AAOI comes
    # back with price + SEC fundamentals + flow + positioning, not "no data".
    tickers = rag.detect_tickers(db, question)
    dossiers = [_ticker_full(db, t) for t in tickers[:3]]

    held = portfolio_ingest.held_tickers(db)
    for s in signals:
        s["held"] = any(str(t).upper() in held for t in s.get("tickers", []))

    # For an evaluative question on a specific name, convene the Investor Lens panel
    # on the primary ticker and fold its 6-persona read into the answer.
    lens_panel = None
    if dossiers and rag.is_evaluative(question):
        try:
            from app.processing import investor_lens as _lens
            lp = _lens.analyze(dossiers[0]["ticker"], dossiers[0])
            if lp.get("personas"):
                lens_panel = lp
        except Exception:
            logger.warning("investor lens during ask failed", exc_info=True)

    # Cross-source intelligence on the primary named ticker: AI-bottleneck
    # competitive landscape, firm research (LogiqGPT memos + ChromaDB), and — for
    # evaluative questions — a live web read. Each degrades to None independently.
    bottleneck = firm = web = None
    if dossiers:
        primary = dossiers[0]["ticker"]
        try:
            from app.processing import bottlenecks
            bottleneck = bottlenecks.get_landscape(primary)
        except Exception:
            logger.warning("bottleneck landscape failed", exc_info=True)
        try:
            from app.processing import firm_research
            firm = firm_research.get_research(question, ticker=primary)
        except Exception:
            logger.warning("firm research failed", exc_info=True)
        if rag.is_evaluative(question):
            web = _web_read(primary, question)

    have_data = bool(signals) or bottleneck or firm or web or any(
        d.get("price") is not None or d.get("fundamentals") or d.get("recent_signals")
        for d in dossiers
    )
    if not have_data:
        return JSONResponse({
            "answer": "I don't have any aggregated data matching that yet — try a "
                      "ticker, theme, or topic the dashboard tracks.",
            "sources": [], "retrieved": 0,
        })

    context = {
        "signals": signals,
        "tickers": [_compact_dossier(d) for d in dossiers],
    }
    if lens_panel:
        context["investor_lens"] = {
            "ticker": lens_panel.get("ticker"),
            "personas": lens_panel.get("personas"),
            "consensus": lens_panel.get("consensus"),
        }
    if bottleneck:
        context["bottleneck_landscape"] = bottleneck
    if firm:
        context["firm_research"] = firm
    if web:
        context["web"] = web
    if rag.is_portfolio_query(question) and held:
        meta = portfolio_ingest.portfolio_meta(db) or {}
        holdings = portfolio_ingest.holdings_list(db)
        context["portfolio"] = {
            "meta": meta,
            "holdings": [
                {"ticker": h["ticker"], "name": h["name"],
                 "weight_pct": h["weight_pct"], "market_value": h["market_value"]}
                for h in holdings[:25]
            ],
            "you_hold": sorted(held),
        }
    answer_text = rag.generate(question, context)
    # Token accounting + cost estimate (Sonnet 4.6: $3/1M in, $15/1M out; cache
    # reads ~$0.30/1M). Excludes the web_search tool's per-search fee.
    u = claude.get_usage() or {}
    cost = round(
        (u.get("input_tokens", 0) * 3.0
         + u.get("output_tokens", 0) * 15.0
         + u.get("cache_read", 0) * 0.30) / 1_000_000,
        4,
    )
    usage = {**u, "model": config.SONNET_MODEL, "est_cost_usd": cost,
             "note": "excludes web_search per-search fee (~$0.01/search)"}
    return JSONResponse({
        "answer": answer_text,
        "usage": usage,
        "sources": [
            {"summary": s["summary"], "direction": s["direction"],
             "url": s["url"], "date": s["date"]}
            for s in signals[:8]
        ],
        "retrieved": len(signals),
        "tickers": tickers,
        "dossiers": context["tickers"],
        "investor_lens": context.get("investor_lens"),
        "firm_docs": [
            {"filename": fd.get("filename"), "web_url": fd.get("web_url"),
             "status": fd.get("status"), "modified": fd.get("modified")}
            for fd in (firm or {}).get("firm_docs", [])[:6]
        ],
        "bottleneck_landscape": bottleneck,
        "sources_used": [
            name for name, present in (
                ("dashboard signals", bool(signals)),
                ("ticker dossier + SEC fundamentals", bool(dossiers)),
                ("investor lens", bool(lens_panel)),
                ("AI-bottleneck landscape", bool(bottleneck)),
                ("LogiqGPT firm research", bool(firm)),
                ("web", bool(web)),
                ("portfolio", "portfolio" in context),
            ) if present
        ],
    })


# ---------------------------------------------------------------------- theses
def _thesis_dict(db: Session, t: Thesis) -> dict:
    ev = db.execute(
        select(ThesisEvidence).where(ThesisEvidence.thesis_id == t.id)
        .order_by(ThesisEvidence.created_at.desc())
    ).scalars().all()
    confirms = sum(1 for e in ev if e.stance == "confirms")
    contradicts = sum(1 for e in ev if e.stance == "contradicts")
    items = []
    for e in ev[:20]:
        sig = db.get(Signal, e.signal_id)
        items.append({
            "stance": e.stance, "note": e.note,
            "summary": sig.summary if sig else "",
            "direction": sig.direction if sig else "",
            "url": sig.item.url if sig and sig.item else "",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    total = confirms + contradicts
    return {
        "id": t.id, "title": t.title, "direction": t.direction,
        "tickers": t.tickers, "rationale": t.rationale, "status": t.status,
        "confirms": confirms, "contradicts": contradicts,
        "score": round((confirms - contradicts) / total, 2) if total else 0.0,
        "evidence": items,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@app.get("/api/theses")
def list_theses(db: Session = Depends(get_db)) -> JSONResponse:
    theses = db.execute(select(Thesis).order_by(Thesis.created_at.desc())).scalars().all()
    return JSONResponse([_thesis_dict(db, t) for t in theses])


@app.post("/api/theses")
def create_thesis(payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    t = Thesis(
        title=title,
        direction=(payload.get("direction") or "long").strip().lower(),
        tickers=",".join(s.strip().upper() for s in (payload.get("tickers") or "").split(",") if s.strip()),
        rationale=(payload.get("rationale") or "").strip(),
    )
    db.add(t)
    db.commit()
    return JSONResponse({"id": t.id})


@app.patch("/api/theses/{thesis_id}")
def update_thesis(thesis_id: int, payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    t = db.get(Thesis, thesis_id)
    if t is None:
        raise HTTPException(404, "thesis not found")
    for field in ("title", "direction", "rationale", "status"):
        if field in payload:
            setattr(t, field, str(payload[field]))
    if "tickers" in payload:
        t.tickers = ",".join(s.strip().upper() for s in str(payload["tickers"]).split(",") if s.strip())
    t.updated_at = datetime.now(timezone.utc)
    db.commit()
    return JSONResponse({"ok": True})


@app.delete("/api/theses/{thesis_id}")
def delete_thesis(thesis_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    t = db.get(Thesis, thesis_id)
    if t is not None:
        db.delete(t)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/theses/evaluate")
def evaluate_theses_endpoint(db: Session = Depends(get_db)) -> JSONResponse:
    """Classify recent signals against all active theses (confirm/contradict)."""
    from app.processing.theses import evaluate_all
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    result = evaluate_all(db)
    return JSONResponse({"evaluated": result})


# ------------------------------------------------------------- daily findings
def run_daily_findings(db: Session, force: bool = False) -> dict:
    """DeepResearch top-5 for today (cached per day unless force). Stores the row."""
    from app.processing.knowledge import deep_research_findings

    date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = db.scalar(select(DailyFinding).where(DailyFinding.date_key == date_key))
    if existing and not force:
        return {
            "date": date_key,
            "findings": json.loads(existing.findings_json or "[]"),
            "packet": json.loads(existing.packet_json or "{}"),
            "cached": True,
        }

    packet = _conviction_packet(db)
    findings = deep_research_findings(packet)

    if existing:
        existing.findings_json = json.dumps(findings)
        existing.packet_json = json.dumps(packet, default=str)
    else:
        db.add(DailyFinding(
            date_key=date_key,
            findings_json=json.dumps(findings),
            packet_json=json.dumps(packet, default=str),
        ))
    db.commit()
    return {"date": date_key, "findings": findings, "packet": packet, "cached": False}


@app.get("/api/findings/today")
def findings_today(db: Session = Depends(get_db)) -> JSONResponse:
    """Today's stored DeepResearch findings (does NOT trigger an LLM run)."""
    date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = db.scalar(select(DailyFinding).where(DailyFinding.date_key == date_key))
    if row is None:
        # Fall back to the most recent stored day so the page is never empty.
        row = db.scalar(select(DailyFinding).order_by(DailyFinding.created_at.desc()).limit(1))
    if row is None:
        return JSONResponse({"date": None, "findings": [], "packet": {}, "stale": False})
    return JSONResponse({
        "date": row.date_key,
        "findings": json.loads(row.findings_json or "[]"),
        "packet": json.loads(row.packet_json or "{}"),
        "stale": row.date_key != date_key,
    })


@app.post("/api/findings/generate")
def findings_generate(db: Session = Depends(get_db)) -> JSONResponse:
    """Run (or re-run) today's DeepResearch findings now."""
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    return JSONResponse(run_daily_findings(db, force=True))


@app.get("/api/findings/history")
def findings_history(limit: int = 14, db: Session = Depends(get_db)) -> JSONResponse:
    """Past daily findings (date + count + the top-1 title)."""
    rows = db.execute(
        select(DailyFinding).order_by(DailyFinding.created_at.desc()).limit(limit)
    ).scalars().all()
    out = []
    for r in rows:
        f = json.loads(r.findings_json or "[]")
        out.append({
            "date": r.date_key,
            "count": len(f),
            "top": f[0]["title"] if f else None,
        })
    return JSONResponse(out)


# ----------------------------------------------------------------- smart money
# Short-TTL cache so /api/smart-money, /api/home, and the conviction packet all
# reuse one computation instead of re-aggregating per request.
_smart_cache: dict[int, tuple[float, dict]] = {}
_SMART_TTL = 90  # seconds


def _smart_money_data(db: Session, days: int = 14) -> dict:
    """Aggregate insider (Form 4) + congressional + institutional trades into
    per-ticker flow, flagging divergence from consensus. Cached for 90s."""
    import time as _time
    hit = _smart_cache.get(days)
    if hit and (_time.monotonic() - hit[0]) < _SMART_TTL:
        return hit[1]
    result = _smart_money_compute(db, days)
    _smart_cache[days] = (_time.monotonic(), result)
    return result


def _smart_money_compute(db: Session, days: int = 14) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Pull congress + insider + institutional signals via their source type.
    rows = db.execute(
        select(Signal, Source.type)
        .join(Item, Signal.item_id == Item.id)
        .join(Source, Item.source_id == Source.id)
        .where(
            Signal.created_at >= cutoff,
            Source.type.in_(["congress", "form4", "institutional"]),
        )
        .order_by(Signal.created_at.desc())
    ).all()

    politician_trades, insider_trades, institutional_trades = [], [], []
    flow: dict[str, dict] = {}
    for sig, stype in rows:
        try:
            tickers = json.loads(sig.entities_json or "{}").get("tickers", [])
        except json.JSONDecodeError:
            tickers = []
        entry = {
            "summary": sig.summary,
            "direction": sig.direction,
            "url": sig.item.url if sig.item else "",
            "created_at": sig.created_at.isoformat() if sig.created_at else None,
            "tickers": tickers,
        }
        if stype == "congress":
            politician_trades.append(entry)
        elif stype == "institutional":
            institutional_trades.append(entry)
        else:
            insider_trades.append(entry)
        for t in tickers:
            sym = str(t).upper()
            f = flow.setdefault(sym, {
                "ticker": sym, "insider_buy": 0, "insider_sell": 0,
                "congress_buy": 0, "congress_sell": 0,
                "inst_buy": 0, "inst_sell": 0,
            })
            kind = {"congress": "congress", "institutional": "inst"}.get(stype, "insider")
            if sig.direction == "bullish":
                f[f"{kind}_buy"] += 1
            elif sig.direction == "bearish":
                f[f"{kind}_sell"] += 1

    # Join consensus + compute divergence.
    syms = list(flow.keys())
    consensus = {
        t.symbol: t for t in
        db.execute(select(Ticker).where(Ticker.symbol.in_(syms))).scalars().all()
    } if syms else {}

    flow_list = []
    for sym, f in flow.items():
        buys = f["insider_buy"] + f["congress_buy"] + f.get("inst_buy", 0)
        sells = f["insider_sell"] + f["congress_sell"] + f.get("inst_sell", 0)
        total = buys + sells
        smart_dir = (buys - sells) / total if total else 0
        tk = consensus.get(sym)
        cons = tk.consensus_score if tk else None
        cons_label = tk.consensus_label if tk else None
        # Divergence: smart money moving opposite to crowd consensus.
        divergence = None
        if cons is not None and abs(smart_dir) >= 0.3 and abs(cons) >= 0.15:
            if (smart_dir > 0 and cons < 0):
                divergence = "smart money buying vs bearish consensus"
            elif (smart_dir < 0 and cons > 0):
                divergence = "smart money selling vs bullish consensus"
        flow_list.append({
            **f,
            "total": total,
            "smart_direction": round(smart_dir, 2),
            "consensus_score": cons,
            "consensus_label": cons_label,
            "divergence": divergence,
        })

    # Rank: divergent first, then by activity.
    flow_list.sort(key=lambda x: (x["divergence"] is not None, x["total"]), reverse=True)

    return {
        "politician_trades": politician_trades[:25],
        "insider_trades": insider_trades[:25],
        "institutional_trades": institutional_trades[:25],
        "flow": flow_list[:30],
        "divergent": [f for f in flow_list if f["divergence"]][:10],
    }


@app.get("/api/smart-money")
def get_smart_money(days: int = 14, db: Session = Depends(get_db)) -> JSONResponse:
    """Insider + politician trades, aggregated per ticker with consensus divergence."""
    return JSONResponse(_smart_money_data(db, days))


@app.get("/api/agreement")
def get_agreement(days: int = 30, db: Session = Depends(get_db)) -> JSONResponse:
    """Cross-signal agreement — the 'everyone agrees' view.

    Counts, per ticker, how many INDEPENDENT smart-money sources are bullish at once
    (insider Form-4 buying, insider *cluster* buys, congressional buying, institutional
    13D/G stakes), keeps names where >=2 align, and flags 'contrarian' when the crowd
    consensus isn't bullish (that gap is the non-consensus edge). Pure cross-referencing
    of existing signals — no prediction."""
    sm = _smart_money_data(db, days)
    flow = {f["ticker"]: f for f in sm.get("flow", [])}

    from app.processing.openinsider import get_cluster_buys
    clusters: dict[str, dict] = {}
    for c in get_cluster_buys().get("clusters", []):
        t = (c.get("ticker") or "").upper()
        if t:
            clusters[t] = c

    out = []
    for sym in set(flow) | set(clusters):
        f = flow.get(sym, {})
        sources = []
        if f.get("insider_buy", 0) > 0 and f["insider_buy"] > f.get("insider_sell", 0):
            sources.append({"label": "Insider buying (Form 4)", "detail": f"{f['insider_buy']} buy signal(s)"})
        if f.get("congress_buy", 0) > 0 and f["congress_buy"] > f.get("congress_sell", 0):
            sources.append({"label": "Congress buying", "detail": f"{f['congress_buy']} disclosure(s)"})
        if f.get("inst_buy", 0) > 0 and f["inst_buy"] > f.get("inst_sell", 0):
            sources.append({"label": "Institutional stake (13D/G)", "detail": f"{f['inst_buy']} filing(s)"})
        cluster_size = 0
        if sym in clusters:
            c = clusters[sym]
            cluster_size = int(c.get("num_insiders") or 0)
            val = c.get("value_usd") or 0
            sources.append({"label": "Insider cluster buy",
                            "detail": f"{cluster_size or '?'} insiders · ${val / 1e6:.1f}M"})
        # Qualify on real cross-source agreement (>=2 distinct source types), OR a
        # strong standalone insider cluster (>=3 insiders all buying the same name).
        if len(sources) >= 2 or cluster_size >= 3:
            cons = f.get("consensus_score")
            out.append({
                "ticker": sym,
                "company": (clusters.get(sym) or {}).get("company"),
                "agreement": len(sources),
                "cluster_size": cluster_size,
                "sources": sources,
                "consensus_score": cons,
                "consensus_label": f.get("consensus_label"),
                "contrarian": cons is None or cons <= 0.15,  # crowd not (yet) bullish = the edge
            })
    # Multi-source agreements first, then big clusters, edge (contrarian) ranked up.
    out.sort(key=lambda x: (x["agreement"], x["cluster_size"], x["contrarian"]), reverse=True)
    return JSONResponse({"days": days, "count": len(out), "tickers": out[:40]})


_PRED_STOP = {
    "will", "the", "a", "an", "of", "in", "on", "at", "to", "be", "by", "or",
    "and", "before", "after", "above", "below", "than", "this", "that", "for",
    "yes", "no", "end", "reach", "hit", "finish", "settle", "over", "under",
    "between", "during", "week", "month", "year", "june", "july", "2025", "2026",
}


def _pred_keywords(question: str) -> set[str]:
    """Signature tokens for clustering the same real-world event across markets."""
    import re
    toks = re.findall(r"[A-Za-z]{3,}|\$?\d[\d,\.]*[kKmMbB%]?", question)
    out = set()
    for t in toks:
        tl = t.lower().strip("$,.")
        if tl and tl not in _PRED_STOP and not tl.isdigit() or any(c in t for c in "$%kKmMbB"):
            out.add(tl)
    return out


def _parse_pred(it: Item) -> dict:
    prob = None
    head = it.title.split("%", 1)
    if len(head) == 2 and head[0].strip().isdigit():
        prob = int(head[0].strip())
    question = it.title.split("—", 1)[-1].strip() if "—" in it.title else it.title
    return {
        "title": it.title,
        "question": question,
        "probability": prob,
        "source": it.author or "Prediction market",
        "url": it.url,
        "end_date": it.published_at.isoformat() if it.published_at else None,
        "keywords": _pred_keywords(question),
    }


@app.get("/api/predictions")
def get_predictions(limit: int = 60, db: Session = Depends(get_db)) -> JSONResponse:
    """Forecasts from Polymarket + Kalshi, with cross-market validation.

    When both venues price the same real-world event, we surface them together:
    tight agreement = a validated forecast; a wide spread = markets disagree
    (an arbitrage or genuine-uncertainty signal).
    """
    items = db.execute(
        select(Item).where(Item.media_type == "prediction")
        .order_by(Item.ingested_at.desc()).limit(200)
    ).scalars().all()
    preds = [_parse_pred(it) for it in items]

    # Cluster across sources by shared signature keywords (need >=2 overlap).
    cross = []
    used = set()
    for i, a in enumerate(preds):
        if i in used or a["probability"] is None:
            continue
        group = [a]
        for j in range(i + 1, len(preds)):
            if j in used or preds[j]["probability"] is None:
                continue
            b = preds[j]
            if a["source"] != b["source"] and len(a["keywords"] & b["keywords"]) >= 2:
                group.append(b)
                used.add(j)
        if len(group) > 1:
            used.add(i)
            probs = [g["probability"] for g in group]
            spread = max(probs) - min(probs)
            cross.append({
                "question": a["question"],
                "members": [
                    {"source": g["source"], "probability": g["probability"], "url": g["url"]}
                    for g in group
                ],
                "spread": spread,
                "agreement": "validated" if spread <= 10 else "diverging" if spread <= 30 else "conflicting",
            })

    cross.sort(key=lambda x: x["spread"])  # validated (tight) first

    # Flat list (strip keyword sets for JSON)
    flat = [{k: v for k, v in p.items() if k != "keywords"} for p in preds][:limit]

    return JSONResponse({"predictions": flat, "cross_validated": cross[:20]})


@app.get("/api/consensus")
def get_consensus(db: Session = Depends(get_db)) -> JSONResponse:
    """All tickers with a computed consensus score, plus their raw inputs."""
    tickers = db.execute(
        select(Ticker).where(Ticker.consensus_score.isnot(None))
        .order_by(Ticker.consensus_score)
    ).scalars().all()
    return JSONResponse([
        {
            "symbol": t.symbol,
            "name": t.name,
            "consensus_score": t.consensus_score,
            "consensus_label": t.consensus_label,
            "analyst_rating": t.analyst_rating,
            "short_interest_pct": t.short_interest_pct,
            "st_bull": t.st_bull, "st_bear": t.st_bear,
        }
        for t in tickers
    ])


# ------------------------------------------------------------------------ home
@app.get("/api/home")
def get_home(db: Session = Depends(get_db)) -> JSONResponse:
    """One-glance overview: condensed highlights from every important tab."""
    now = datetime.now(timezone.utc)
    cutoff48 = now - timedelta(hours=48)
    cutoff7d = now - timedelta(days=7)

    recent = db.execute(
        select(Signal).where(Signal.created_at >= cutoff48)
        .order_by(Signal.confidence.desc())
    ).scalars().all()

    by_dir = {"bullish": 0, "bearish": 0, "neutral": 0}
    for s in recent:
        by_dir[s.direction] = by_dir.get(s.direction, 0) + 1

    # Top signals (high-confidence directional)
    top_signals = [
        _signal_dict(s) for s in recent
        if s.direction != "neutral" and s.confidence >= 0.7
    ][:5]

    # Non-consensus (alpha >= 6)
    scored = score_signals(recent, db, cache_key="home")
    scored.sort(key=lambda x: x[1]["total"], reverse=True)
    non_consensus = [
        _signal_dict(s, a) for s, a in scored
        if a["total"] >= 6 and s.confidence >= 0.55
    ][:5]

    # Industry pulse: signals per industry (last 7d) + sentiment
    industries = db.execute(select(Industry).order_by(Industry.name)).scalars().all()
    ind_pulse = []
    for ind in industries:
        sigs = db.execute(
            select(Signal).where(
                Signal.industry_id == ind.id, Signal.created_at >= cutoff7d
            )
        ).scalars().all()
        if not sigs:
            continue
        bull = sum(1 for s in sigs if s.direction == "bullish")
        bear = sum(1 for s in sigs if s.direction == "bearish")
        ind_pulse.append({
            "name": ind.name,
            "group": ind.group,
            "total": len(sigs),
            "bullish": bull,
            "bearish": bear,
            "sentiment": round((bull - bear) / len(sigs), 2) if sigs else 0,
        })
    ind_pulse.sort(key=lambda x: x["total"], reverse=True)

    # Theme shifts (30-day, top 3 rising / falling)
    mid = cutoff7d  # reuse: compare last 7d (recent) vs prior 23d
    cutoff30 = now - timedelta(days=30)
    theme_sigs = db.execute(
        select(Signal).where(Signal.created_at >= cutoff30)
    ).scalars().all()
    theme_stats: dict[str, dict] = {}
    for s in theme_sigs:
        try:
            ents = json.loads(s.entities_json or "{}")
        except json.JSONDecodeError:
            ents = {}
        themes = list(ents.get("technologies") or []) + list(ents.get("companies") or [])
        half = "recent" if s.created_at and _as_aware(s.created_at) >= mid else "prior"
        for theme in themes:
            theme = str(theme).strip()
            if len(theme) < 2:
                continue
            st = theme_stats.setdefault(theme, {"recent": 0, "prior": 0, "total": 0})
            st[half] += 1
            st["total"] += 1
    theme_changes = [
        {"theme": k, "change": v["recent"] - v["prior"], "total": v["total"]}
        for k, v in theme_stats.items() if v["total"] >= 2
    ]
    rising = sorted([t for t in theme_changes if t["change"] > 0],
                    key=lambda x: x["change"], reverse=True)[:5]
    falling = sorted([t for t in theme_changes if t["change"] < 0],
                     key=lambda x: x["change"])[:5]

    # Watchlist movers: tickers with strongest |signal score| in last 7d
    ticker_dirs: dict[str, dict] = {}
    for s in theme_sigs:
        try:
            tks = json.loads(s.entities_json or "{}").get("tickers", [])
        except json.JSONDecodeError:
            tks = []
        for t in tks:
            sym = str(t).upper()
            d = ticker_dirs.setdefault(sym, {"bullish": 0, "bearish": 0, "total": 0})
            d[s.direction] = d.get(s.direction, 0) + 1
            d["total"] += 1
    movers = sorted(
        [
            {"ticker": k, "score": round((v["bullish"] - v["bearish"]) / v["total"], 2),
             "total": v["total"], "bullish": v["bullish"], "bearish": v["bearish"]}
            for k, v in ticker_dirs.items() if v["total"] >= 2
        ],
        key=lambda x: abs(x["score"]) * x["total"], reverse=True,
    )[:6]

    # Recent shift alerts
    alerts = db.execute(
        select(ShiftAlert).order_by(ShiftAlert.created_at.desc()).limit(5)
    ).scalars().all()
    shift_alerts = [
        {"symbol": a.symbol, "from_score": round(a.from_score, 2),
         "to_score": round(a.to_score, 2),
         "direction": "bullish" if a.to_score > 0 else "bearish",
         "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in alerts
    ]

    # Smart money (insider + politician) + prediction markets
    smart = _smart_money_data(db, days=14)
    predictions = [
        {"title": it.title,
         "probability": (int(it.title.split("%", 1)[0].strip())
                         if it.title.split("%", 1)[0].strip().isdigit() else None),
         "url": it.url}
        for it in db.execute(
            select(Item).where(Item.media_type == "prediction")
            .order_by(Item.ingested_at.desc()).limit(6)
        ).scalars().all()
    ]

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_items = db.scalar(select(func.count(Item.id)).where(Item.ingested_at >= today_start)) or 0
    total_items = db.scalar(select(func.count(Item.id))) or 0

    return JSONResponse({
        "generated": now.isoformat(),
        "stats": {
            "total_items": total_items,
            "today_items": today_items,
            "signals_48h": len(recent),
            "bullish": by_dir["bullish"],
            "bearish": by_dir["bearish"],
        },
        "top_signals": top_signals,
        "non_consensus": non_consensus,
        "industry_pulse": ind_pulse[:8],
        "rising_themes": rising,
        "falling_themes": falling,
        "watchlist_movers": movers,
        "shift_alerts": shift_alerts,
        "smart_money_divergent": smart["divergent"][:5],
        "politician_trades": smart["politician_trades"][:6],
        "insider_trades": smart["insider_trades"][:6],
        "predictions": predictions,
    })


# ---------------------------------------------------------------------- digest
@app.get("/api/digest/preview")
def digest_preview(db: Session = Depends(get_db)) -> JSONResponse:
    return JSONResponse(digest_mod.build_digest(db))


@app.post("/api/digest/send")
def digest_send(db: Session = Depends(get_db)) -> JSONResponse:
    payload = digest_mod.send_daily_digest(db)
    db.commit()
    return JSONResponse({"sent": True, "signals": len(payload["signals"])})


# ---------------------------------------------------------------------- ingest
@app.post("/api/ingest")
def trigger_ingest(background_tasks: BackgroundTasks) -> JSONResponse:
    if _ingest_state["running"]:
        return JSONResponse({"status": "already_running"})
    background_tasks.add_task(_run_ingest_background)
    return JSONResponse({"status": "started"})


@app.get("/api/ingest/status")
def ingest_status(db: Session = Depends(get_db)) -> JSONResponse:
    now = datetime.now(timezone.utc)
    yesterday_start = (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_start = yesterday_start + timedelta(days=1)
    yesterday_count = db.scalar(
        select(func.count(Item.id)).where(
            Item.ingested_at >= yesterday_start,
            Item.ingested_at < today_start,
        )
    ) or 0
    today_count = db.scalar(
        select(func.count(Item.id)).where(Item.ingested_at >= today_start)
    ) or 0
    total_count = db.scalar(select(func.count(Item.id))) or 0
    return JSONResponse({
        **_ingest_state,
        "yesterday_items": yesterday_count,
        "today_items": today_count,
        "total_items": total_count,
    })


@app.get("/api/config")
def get_config() -> JSONResponse:
    return JSONResponse({"cost_tier": config.COST_TIER})


# --------------------------------------------------------------- static + root
@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
