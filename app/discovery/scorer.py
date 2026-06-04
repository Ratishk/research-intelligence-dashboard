"""Apply the 0-12 vetting rubric to a candidate source using Claude.

Dimensions (3 pts each): Activity, Originality, Track Record, Signal Yield.
For brand-new candidates we have no measured yield yet, so Claude estimates a
provisional Track-Record-based proxy; the recycler later overwrites the yield
dimension with measured data.
"""
from __future__ import annotations

import logging

from app.config import config
from app.llm import claude
from app.models import Source

logger = logging.getLogger(__name__)

_SCORER_SYSTEM = (
    "You vet information sources for an investment research pipeline. Score the "
    "candidate 0-3 on each dimension:\n"
    "- activity: how recently/regularly it publishes\n"
    "- originality: primary research/analysis (3) vs pure aggregation (0)\n"
    "- track_record: documented correct calls or recognized domain/finance expertise\n"
    "- signal_quality: signal-to-noise for actionable market intelligence\n"
    "Return ONLY JSON: {activity, originality, track_record, signal_quality, "
    "rationale}. Each numeric field is an integer 0-3."
)

# Tier thresholds on the 0-12 total.
TIER1_MIN = 9
KEEP_MIN = 6


def score_candidate(candidate: dict) -> dict:
    desc = (
        f"Name: {candidate.get('name')}\n"
        f"Type: {candidate.get('type')}\n"
        f"URL/handle: {candidate.get('url') or candidate.get('handle')}\n"
        f"Claimed credibility: {candidate.get('reason', '')}"
    )
    raw = claude.complete(
        system=_SCORER_SYSTEM, user=desc, model=config.SONNET_MODEL, max_tokens=300
    )
    data = claude.extract_json(raw)

    def _clip(key: str) -> int:
        try:
            return max(0, min(3, int(data.get(key, 0))))
        except (TypeError, ValueError):
            return 0

    parts = {
        "activity": _clip("activity"),
        "originality": _clip("originality"),
        "track_record": _clip("track_record"),
        "signal_quality": _clip("signal_quality"),
    }
    total = sum(parts.values())
    return {
        "score": total,
        "tier": 1 if total >= TIER1_MIN else 2,
        "keep": total >= KEEP_MIN,
        "parts": parts,
        "rationale": data.get("rationale", ""),
    }


def rescore_existing(source: Source) -> dict:
    """Re-score an existing source, folding in its measured signal yield.

    Measured yield (signals/items) maps onto the 0-3 signal dimension, replacing
    Claude's provisional estimate so proven sources rise and dead ones fall.
    """
    candidate = {
        "name": source.name,
        "type": source.type,
        "url": source.url,
        "handle": source.handle,
        "reason": f"Currently tier {source.tier}, measured signal yield {source.signal_yield:.2f}",
    }
    result = score_candidate(candidate)
    # Override signal dimension with measured yield (0..~1 -> 0..3 buckets).
    y = source.signal_yield
    measured = 3 if y >= 0.25 else 2 if y >= 0.1 else 1 if y > 0 else 0
    result["parts"]["signal_quality"] = measured
    result["score"] = sum(result["parts"].values())
    result["tier"] = 1 if result["score"] >= TIER1_MIN else 2
    result["keep"] = result["score"] >= KEEP_MIN
    return result
