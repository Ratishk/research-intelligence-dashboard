"""Investment narrative synthesis + narrative-shift detection.

- ``narrative_for_signal``: a short Sonnet take on what a signal means.
- ``detect_shifts``: flags tickers whose net signal direction flipped polarity
  within a recent window — the basis for the email shift alerts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.config import config
from app.llm import claude
from app.models import Signal
from app.processing.tickers import signal_score

logger = logging.getLogger(__name__)

_NARRATIVE_SYSTEM = (
    "You are an investment strategist. In 2-3 sentences, explain what the given "
    "signal means for investors: who benefits, who is at risk, and whether it "
    "reflects real adoption or early R&D. No preamble."
)


def narrative_for_signal(signal: Signal) -> str:
    return claude.complete(
        system=_NARRATIVE_SYSTEM,
        user=f"Signal: {signal.summary}\nType: {signal.signal_type}\nDirection: {signal.direction}",
        model=config.SONNET_MODEL,
        max_tokens=200,
    )


def detect_shifts(session, symbols: list[str]) -> list[dict]:
    """Compare each ticker's score over the last 2 days vs the prior window.

    A polarity flip (e.g. positive -> negative) is surfaced as an alert. This is
    intentionally simple and cheap; it runs on already-stored signals.
    """
    alerts = []
    for symbol in symbols:
        recent = signal_score(session, symbol, days=2)
        prior = signal_score(session, symbol, days=7)
        if recent["total"] == 0 or prior["total"] == 0:
            continue
        if recent["score"] * prior["score"] < 0:  # opposite signs => flip
            alerts.append(
                {
                    "symbol": symbol,
                    "from": round(prior["score"], 2),
                    "to": round(recent["score"], 2),
                    "recent_total": recent["total"],
                }
            )
    return alerts
