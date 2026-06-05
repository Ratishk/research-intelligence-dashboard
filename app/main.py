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
    Industry,
    Item,
    ShiftAlert,
    Signal,
    Source,
    SourceStatus,
    Ticker,
    WatchlistItem,
)
from app.processing import digest as digest_mod
from app.processing import research
from app.processing.alpha import score_signals
from app.processing.tickers import refresh_ticker
from app.llm import claude

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Research Intelligence Dashboard")

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
    """Weekly signal direction counts + top technology mentions, bounded to `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    signals = db.execute(
        select(Signal).where(Signal.created_at >= cutoff)
    ).scalars().all()
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
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
            mine.append({"summary": s.summary, "direction": s.direction,
                         "type": s.signal_type, "confidence": round(s.confidence, 2)})
    flow = next((f for f in _smart_money_data(db, 21)["flow"] if f["ticker"] == sym), None)
    tk = db.get(Ticker, sym)
    return {
        "ticker": sym,
        "name": tk.name if tk else None,
        "price": tk.price if tk else None,
        "consensus": tk.consensus_label if tk else None,
        "analyst_rating": tk.analyst_rating if tk else None,
        "short_interest_pct": tk.short_interest_pct if tk else None,
        "rvol": tk.rvol if tk else None,
        "change_pct": tk.change_pct if tk else None,
        "smart_money_flow": flow,
        "recent_signals": mine[:15],
        "signal_count": len(mine),
    }


@app.post("/api/investor-lens")
def investor_lens(payload: dict, db: Session = Depends(get_db)) -> JSONResponse:
    """Judge a ticker through legendary-investor frameworks."""
    from app.processing import investor_lens as lens
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(400, "ticker required")
    if not claude.is_configured():
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    dossier = _ticker_dossier(db, ticker)
    result = lens.analyze(ticker, dossier)
    result["dossier"] = dossier
    return JSONResponse(result)


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
