"""Local RAG over the signal corpus — Phase 1 (keyword, no embeddings).

The dashboard's signals are short, already-tagged "chunks", so retrieval is free
and local: a SQLite FTS5 full-text index + ticker/recency ranking. No Chroma, no
torch, no model downloads. Only the final answer call spends tokens.

(LogiqGPT uses Chroma + a cross-encoder because it retrieves over long unstructured
documents; for atomic tagged signals, FTS5 + structure is enough — see the Phase 2
plan for when embeddings become worth it.)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import config
from app.llm import claude
from app.models import Item, Signal, Source, WatchlistItem

# Source type -> searchable category words, so natural-language questions like
# "insider selling" or "what are politicians buying" match the right signals
# (FTS has no stemming/synonyms — we bake the vocabulary into the index).
_CATEGORY = {
    "form4": "insider insiders insider-trade form-4 executive",
    "form144": "insider insiders planned-sale intent-to-sell form-144",
    "congress": "congress congressional politician politicians senator representative",
    "institutional": "institutional hedge-fund activist 13d 13g stake",
    "dilution": "dilution offering shelf share-issuance s-1 s-3",
    "fdarecall": "fda recall drug safety biotech pharma",
    "form8k": "8-k material-event corporate-event filing",
    "clinicaltrial": "clinical-trial biotech drug catalyst phase",
    "govcontract": "government-contract federal-award defense revenue",
    "twitter": "tweet social x", "bluesky": "social bluesky",
    "youtube": "video youtube", "patent": "patent",
    "sec": "sec filing", "rss": "news article",
}
_TABLE = "sig_fts_v2"  # bump to force a clean rebuild on schema change

logger = logging.getLogger(__name__)

_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "is", "are", "was", "for", "and",
    "or", "what", "whats", "how", "why", "when", "which", "who", "do", "does",
    "did", "will", "should", "can", "could", "with", "about", "any", "this",
    "that", "right", "now", "today", "show", "me", "tell", "give", "latest",
    "going", "look", "looks", "like", "still", "intact", "happening",
}


def _content_for(s: Signal, src_type: str | None) -> str:
    """Enriched FTS text: summary + direction + signal type + source-category vocab.

    Baking 'insider', 'congress', 'dilution', 'bullish', etc. into the index lets
    natural-language questions match the right signals despite FTS having no
    stemming or synonyms (the core Phase-1 limitation embeddings fix in Phase 2)."""
    parts = [s.summary or "", s.direction or "", (s.signal_type or "").replace("_", " ")]
    if src_type and src_type in _CATEGORY:
        parts.append(_CATEGORY[src_type])
    return " ".join(p for p in parts if p)


def ensure_index(db) -> None:
    """Create the FTS5 table if needed and incrementally index new signals."""
    conn = db.connection()
    conn.exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} "
        "USING fts5(content, tickers, sid UNINDEXED)"
    )
    row = conn.exec_driver_sql(f"SELECT MAX(CAST(sid AS INTEGER)) FROM {_TABLE}").fetchone()
    max_id = (row[0] or 0) if row else 0
    new_sigs = db.execute(
        select(Signal, Source.type)
        .join(Item, Signal.item_id == Item.id)
        .join(Source, Item.source_id == Source.id)
        .where(Signal.id > max_id)
    ).all()
    for s, src_type in new_sigs:
        try:
            tickers = " ".join(json.loads(s.entities_json or "{}").get("tickers", []))
        except json.JSONDecodeError:
            tickers = ""
        conn.exec_driver_sql(
            f"INSERT INTO {_TABLE} (content, tickers, sid) VALUES (?, ?, ?)",
            (_content_for(s, src_type), tickers, str(s.id)),
        )
    if new_sigs:
        db.commit()


def _stem(t: str) -> str:
    """Crude suffix-stripping so 'selling'/'sells' both reduce toward 'sell'."""
    for suf in ("ing", "ied", "ies", "ed", "es", "s"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _build_fts(question: str) -> str | None:
    raw = [t for t in re.findall(r"[A-Za-z]{3,}", question.lower()) if t not in _STOP]
    terms: list[str] = []
    for t in raw:
        terms.append(t)
        st = _stem(t)
        if st != t and len(st) >= 3:
            terms.append(st + "*")  # prefix-match the stem to recover inflections
    terms = list(dict.fromkeys(terms))[:16]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' if not t.endswith("*") else t for t in terms)


def detect_tickers(db, question: str) -> list[str]:
    syms = {r[0] for r in db.execute(select(WatchlistItem.ticker_symbol)).all()}
    tokens = set(re.findall(r"\b[A-Z]{1,5}\b", question))
    return [t for t in tokens if t in syms]


def retrieve(db, question: str, k: int = 15) -> list[dict]:
    """Free local retrieval: FTS5 keyword match + ticker boost + recency/alpha rank."""
    ensure_index(db)
    conn = db.connection()
    scores: dict[int, float] = {}

    fts = _build_fts(question)
    if fts:
        try:
            rows = conn.exec_driver_sql(
                f"SELECT sid, bm25({_TABLE}) FROM {_TABLE} "
                f"WHERE {_TABLE} MATCH ? ORDER BY bm25({_TABLE}) LIMIT ?",
                (fts, k * 3),
            ).fetchall()
            for sid, bm in rows:
                scores[int(sid)] = -float(bm)  # bm25 lower=better → negate
        except Exception:
            logger.info("FTS query failed for %r", question)

    tickers = detect_tickers(db, question)

    # Always include a recency/relevance baseline so general questions still ground.
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    recent = db.execute(
        select(Signal).where(Signal.created_at >= cutoff)
    ).scalars().all()
    by_id = {s.id: s for s in recent}

    # If specific tickers were named, hard-include their recent signals.
    for s in recent:
        try:
            stk = {str(t).upper() for t in json.loads(s.entities_json or "{}").get("tickers", [])}
        except json.JSONDecodeError:
            stk = set()
        if tickers and (set(tickers) & stk):
            scores[s.id] = scores.get(s.id, 0) + 5.0  # strong boost for named tickers

    # Blend in recency + confidence for anything in scope.
    now = datetime.now(timezone.utc)
    ranked = []
    for sid, base in scores.items():
        s = by_id.get(sid) or db.get(Signal, sid)
        if s is None:
            continue
        age_days = ((now - (s.created_at.replace(tzinfo=timezone.utc) if s.created_at and not s.created_at.tzinfo else s.created_at)).days
                    if s.created_at else 30)
        recency = max(0.0, 1.0 - age_days / 21)
        ranked.append((base + recency + (s.confidence or 0), s))

    # Fallback: no keyword/ticker hits → top recent high-confidence signals.
    if not ranked:
        ranked = [(s.confidence or 0, s) for s in sorted(recent, key=lambda x: x.created_at or now, reverse=True)[:k]]

    ranked.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, s in ranked[:k]:
        try:
            ents = json.loads(s.entities_json or "{}")
        except json.JSONDecodeError:
            ents = {}
        out.append({
            "id": s.id, "summary": s.summary, "direction": s.direction,
            "type": s.signal_type, "confidence": round(s.confidence or 0, 2),
            "tickers": ents.get("tickers", []),
            "url": s.item.url if s.item else "",
            "date": s.created_at.strftime("%Y-%m-%d") if s.created_at else "",
        })
    return out


_SYSTEM = (
    "You are the analyst for an investment-research dashboard. Answer the user's "
    "question USING ONLY the provided context (retrieved signals + ticker dossiers "
    "from our own aggregated data). Be specific and cite the evidence (quote signal "
    "summaries / numbers). If the context doesn't cover the question, say so plainly "
    "rather than guessing. Keep it tight — a few sentences or short bullets. End with "
    "a one-line bottom-line read when the data supports one."
)


def generate(question: str, context: dict, *, model: str | None = None,
             max_tokens: int = 700) -> str:
    if not claude.is_configured():
        return "Claude is not configured (set ANTHROPIC_API_KEY)."
    user = (
        f"QUESTION: {question}\n\n"
        f"CONTEXT (our aggregated data):\n{json.dumps(context, indent=2, default=str)}"
    )
    return claude.complete(
        system=_SYSTEM, user=user,
        model=model or config.SONNET_MODEL, max_tokens=max_tokens, cache_system=False,
    ) or "No answer generated."
