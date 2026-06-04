"""FastAPI application: REST API + static frontend.

Serves the 9-tab dashboard from /frontend and exposes /api/* used by app.js.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import config
from app.database import get_db, init_db
from app.discovery import engine as discovery_engine
from app.ingestion.runner import run_all_ingestion
from app.models import (
    Industry,
    Item,
    Signal,
    Source,
    SourceStatus,
    Ticker,
    WatchlistItem,
)
from app.processing import digest as digest_mod
from app.processing import research
from app.processing.tickers import refresh_ticker, signal_score

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Research Intelligence Dashboard")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------- serialization
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


def _signal_dict(sig: Signal) -> dict:
    try:
        entities = json.loads(sig.entities_json or "{}")
    except json.JSONDecodeError:
        entities = {}
    return {
        "id": sig.id,
        "signal_type": sig.signal_type,
        "confidence": sig.confidence,
        "direction": sig.direction,
        "summary": sig.summary,
        "entities": entities,
        "speculative": sig.speculative,
        "research_brief": sig.research_brief,
        "industry": sig.item.source.industries[0].name
        if sig.item and sig.item.source and sig.item.source.industries
        else None,
        "url": sig.item.url if sig.item else "",
        "created_at": sig.created_at.isoformat() if sig.created_at else None,
    }


# ------------------------------------------------------------------------ feed
@app.get("/api/feed")
def get_feed(
    tab: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Items for the Feed/YouTube/Twitter/Blogs tabs. ``tab`` filters by media."""
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
    limit: int = 100,
    db: Session = Depends(get_db),
) -> JSONResponse:
    query = select(Signal).order_by(Signal.created_at.desc())
    if type:
        query = query.where(Signal.signal_type == type)
    if direction:
        query = query.where(Signal.direction == direction)
    signals = db.execute(query.limit(limit)).scalars().all()
    out = [_signal_dict(s) for s in signals]
    if ticker:
        t = ticker.upper()
        out = [s for s in out if t in {x.upper() for x in s["entities"].get("tickers", [])}]
    return JSONResponse(out)


@app.post("/api/signals/{signal_id}/research")
def signal_research(signal_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(404, "signal not found")
    brief = research.generate_brief(db, signal)
    db.commit()
    return JSONResponse({"research_brief": brief})


# ------------------------------------------------------------------ watchlists
@app.get("/api/watchlists")
def get_watchlists(db: Session = Depends(get_db)) -> JSONResponse:
    rows = db.execute(select(WatchlistItem)).scalars().all()
    lists: dict[str, list] = {}
    for row in rows:
        ticker = db.get(Ticker, row.ticker_symbol)
        score = signal_score(db, row.ticker_symbol)
        lists.setdefault(row.list_name, []).append(
            {
                "symbol": row.ticker_symbol,
                "name": ticker.name if ticker else "",
                "price": ticker.price if ticker else None,
                "week52_high": ticker.week52_high if ticker else None,
                "week52_low": ticker.week52_low if ticker else None,
                "next_earnings": ticker.next_earnings.isoformat()
                if ticker and ticker.next_earnings
                else None,
                "signal_score": score["score"],
                "bullish": score["bullish"],
                "bearish": score["bearish"],
                "sparkline": score["sparkline"],
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
        refresh_ticker(db, symbol)
        db.commit()
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
def get_trends(db: Session = Depends(get_db)) -> JSONResponse:
    """Weekly topic-mention frequency and per-direction signal counts."""
    signals = db.execute(select(Signal)).scalars().all()
    topic_weeks: dict[str, dict[str, int]] = {}
    direction_weeks: dict[str, dict[str, int]] = {}
    for sig in signals:
        if not sig.created_at:
            continue
        week = sig.created_at.strftime("%Y-W%U")
        direction_weeks.setdefault(week, {"bullish": 0, "bearish": 0, "neutral": 0})
        direction_weeks[week][sig.direction] = (
            direction_weeks[week].get(sig.direction, 0) + 1
        )
        try:
            techs = json.loads(sig.entities_json or "{}").get("technologies", [])
        except json.JSONDecodeError:
            techs = []
        for tech in techs:
            topic_weeks.setdefault(tech, {})
            topic_weeks[tech][week] = topic_weeks[tech].get(week, 0) + 1
    return JSONResponse({"topics": topic_weeks, "directions": direction_weeks})


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
def trigger_ingest() -> JSONResponse:
    counts = run_all_ingestion()
    return JSONResponse(counts)


@app.get("/api/config")
def get_config() -> JSONResponse:
    return JSONResponse({"cost_tier": config.COST_TIER})


# --------------------------------------------------------------- static + root
@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
