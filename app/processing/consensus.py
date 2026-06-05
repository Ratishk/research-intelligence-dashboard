"""Consensus Engine — quantify what the crowd believes about a ticker.

You can't find *non-consensus* without first measuring consensus. This blends
every crowd-positioning signal we can pull for free into one score in [-1, +1]:

  +1  = crowd is maximally bullish (analysts say buy, no shorts, retail bullish)
  -1  = crowd is maximally bearish

A new signal's contrarian value is then its *divergence* from this score, not
its agreement with our own prior signals (which would be circular).

Inputs (all already fetched in tickers.py):
  - analyst_rating       : sell-side recommendation
  - short_interest_pct   : positioning (high short = bearish bet)
  - st_bull / st_bear    : StockTwits retail sentiment
"""
from __future__ import annotations

from datetime import datetime, timezone

# Analyst recommendation → [-1, +1]
_RATING_SCORE = {
    "strong_buy": 1.0,
    "buy": 0.5,
    "outperform": 0.5,
    "hold": 0.0,
    "neutral": 0.0,
    "underperform": -0.5,
    "sell": -0.5,
    "strong_sell": -1.0,
}

# Component weights (sum need not be 1; we normalize by present components).
_W_ANALYST = 0.45
_W_SHORT = 0.25
_W_SOCIAL = 0.30


def _short_score(short_pct: float | None) -> float | None:
    """High short interest = bearish positioning. ~0% -> +0.3, >=20% -> -1."""
    if short_pct is None:
        return None
    # short_pct is a fraction (0.05 = 5%). Map 0..0.20 -> +0.3..-1.0
    s = 0.3 - (min(short_pct, 0.20) / 0.20) * 1.3
    return max(-1.0, min(1.0, s))


def _social_score(bull: int, bear: int) -> float | None:
    total = (bull or 0) + (bear or 0)
    if total == 0:
        return None
    return (bull - bear) / total


def compute_consensus(ticker) -> dict:
    """Return {score, label, components} for a Ticker ORM object."""
    components: dict[str, float] = {}

    rating = (ticker.analyst_rating or "").lower()
    if rating in _RATING_SCORE:
        components["analyst"] = _RATING_SCORE[rating]

    ss = _short_score(ticker.short_interest_pct)
    if ss is not None:
        components["short"] = ss

    soc = _social_score(ticker.st_bull, ticker.st_bear)
    if soc is not None:
        components["social"] = soc

    if not components:
        return {"score": None, "label": "no data", "components": {}}

    weights = {"analyst": _W_ANALYST, "short": _W_SHORT, "social": _W_SOCIAL}
    num = sum(components[k] * weights[k] for k in components)
    den = sum(weights[k] for k in components)
    score = num / den if den else 0.0

    if score >= 0.5:
        label = "strongly bullish"
    elif score >= 0.15:
        label = "bullish"
    elif score > -0.15:
        label = "neutral"
    elif score > -0.5:
        label = "bearish"
    else:
        label = "strongly bearish"

    return {"score": round(score, 3), "label": label, "components": components}


def refresh_consensus(ticker) -> None:
    """Compute and persist the consensus score onto the Ticker object."""
    result = compute_consensus(ticker)
    ticker.consensus_score = result["score"]
    ticker.consensus_label = result["label"]
    ticker.consensus_updated_at = datetime.now(timezone.utc)
