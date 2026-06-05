"""Thesis tracking — classify incoming signals against persistent theses.

Turns the dashboard from a stateless daily snapshot into a thesis-monitoring
system: each active thesis accumulates a feed of signals that CONFIRM or
CONTRADICT it, so you can see whether a call is playing out over time.

Pattern adapted from Anthropic's financial-services `thesis-tracker` skill,
implemented natively with Claude over the dashboard's own signals.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import config
from app.llm import claude
from app.models import Signal, Thesis, ThesisEvidence

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 21
_MAX_SIGNALS_PER_THESIS = 25

_SYSTEM = (
    "You evaluate whether investment signals support or undermine a stated thesis. "
    "For each signal, decide if it CONFIRMS the thesis (makes it more likely right), "
    "CONTRADICTS it (makes it more likely wrong), or is NEUTRAL/irrelevant. Consider "
    "the thesis direction: a bearish thesis is confirmed by bad news / selling and "
    "contradicted by good news / buying. Return ONLY JSON: "
    '{"results":[{"signal_id":<int>,"stance":"confirms|contradicts|neutral",'
    '"note":<= 12 words why>}]}. Be strict — only confirms/contradicts when the link '
    "is real; default to neutral."
)


def _ticker_set(thesis: Thesis) -> set[str]:
    return {t.strip().upper() for t in (thesis.tickers or "").split(",") if t.strip()}


def _candidate_signals(db, thesis: Thesis) -> list[Signal]:
    """Recent signals mentioning any of the thesis tickers (free pre-filter)."""
    syms = _ticker_set(thesis)
    if not syms:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
    recent = db.execute(
        select(Signal).where(Signal.created_at >= cutoff).order_by(Signal.created_at.desc())
    ).scalars().all()
    out = []
    for s in recent:
        try:
            tks = {str(t).upper() for t in json.loads(s.entities_json or "{}").get("tickers", [])}
        except json.JSONDecodeError:
            tks = set()
        if syms & tks:
            out.append(s)
        if len(out) >= _MAX_SIGNALS_PER_THESIS:
            break
    return out


def evaluate_thesis(db, thesis: Thesis) -> int:
    """Classify candidate signals against one thesis; store new evidence rows."""
    if not claude.is_configured():
        return 0
    candidates = _candidate_signals(db, thesis)
    if not candidates:
        return 0
    # Skip signals we've already linked to this thesis.
    seen = {
        e.signal_id for e in db.execute(
            select(ThesisEvidence).where(ThesisEvidence.thesis_id == thesis.id)
        ).scalars().all()
    }
    fresh = [s for s in candidates if s.id not in seen]
    if not fresh:
        return 0

    payload = {
        "thesis": {"title": thesis.title, "direction": thesis.direction,
                   "tickers": thesis.tickers, "rationale": thesis.rationale},
        "signals": [
            {"signal_id": s.id, "direction": s.direction, "type": s.signal_type,
             "summary": s.summary}
            for s in fresh
        ],
    }
    raw = claude.complete(
        system=_SYSTEM, user=json.dumps(payload, indent=2),
        model=config.SONNET_MODEL, max_tokens=1200, cache_system=False,
    )
    results = claude.extract_json(raw).get("results", [])
    added = 0
    for r in results:
        stance = r.get("stance")
        if stance not in ("confirms", "contradicts"):
            continue
        sid = r.get("signal_id")
        if sid is None or sid in seen:
            continue
        db.add(ThesisEvidence(
            thesis_id=thesis.id, signal_id=sid, stance=stance,
            note=str(r.get("note", ""))[:300],
        ))
        seen.add(sid)
        added += 1
    thesis.updated_at = datetime.now(timezone.utc)
    return added


def evaluate_all(db) -> dict:
    """Evaluate every active thesis. Returns per-thesis new-evidence counts."""
    theses = db.execute(
        select(Thesis).where(Thesis.status == "active")
    ).scalars().all()
    out = {}
    for t in theses:
        try:
            out[t.id] = evaluate_thesis(db, t)
        except Exception:
            logger.exception("Thesis eval failed for %s", t.id)
            out[t.id] = 0
    db.commit()
    return out
