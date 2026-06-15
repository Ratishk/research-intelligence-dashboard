"""Investor Lens — analyze a ticker through legendary-investor frameworks.

Inspired by the multi-persona analyst pattern (popularized by ai-hedge-fund and
used in terminals like FinceptTerminal): take everything the dashboard knows
about a ticker — signals, insider/congressional/institutional flow, consensus,
volume, dilution, catalysts — and judge it through several famous investing
philosophies at once. Implemented natively with Claude (one structured call).
"""
from __future__ import annotations

import json
import logging

from app.config import config
from app.llm import claude

logger = logging.getLogger(__name__)

_PERSONAS = [
    ("Warren Buffett", "quality businesses with durable moats, predictable cash flow, bought with a margin of safety; avoids hype and complexity"),
    ("Benjamin Graham", "deep value and margin of safety; demands cheapness vs intrinsic value and a fortress balance sheet; wary of dilution"),
    ("Peter Lynch", "growth at a reasonable price; likes understandable stories with accelerating fundamentals and reasonable valuation"),
    ("Michael Burry", "contrarian and skeptical; hunts overvaluation, dilution, and crowded longs to short; trusts hard data over narrative"),
    ("Cathie Wood", "disruptive innovation and exponential growth; tolerates volatility and high multiples for technological winners"),
    ("Charlie Munger", "high-quality compounders, rationality, and patience; avoids obvious stupidity and overpaying; few big bets"),
]

_SYSTEM = (
    "You are a panel of legendary investors. Given a data dossier on ONE ticker, each "
    "investor judges it THROUGH THEIR OWN philosophy, citing the specific data. Be "
    "decisive and concise. The dossier may include a `fundamentals` block pulled from "
    "SEC filings (revenue, YoY growth, net/gross/operating margins, balance-sheet items, "
    "debt-to-equity, shares outstanding) — use it when judging, especially Buffett, "
    "Graham, and Munger who lean on cash flow, margins, and balance-sheet strength. "
    "Personas and their lenses:\n"
    + "\n".join(f"- {n}: {style}" for n, style in _PERSONAS)
    + "\n\nReturn ONLY JSON: {\"ticker\":..., \"personas\":[{\"name\","
    "\"verdict\":\"bullish|neutral|bearish\",\"rationale\":<1-2 sentences citing the data>}],"
    "\"consensus\":<1 sentence: where the panel agrees/splits and the net lean>}"
)


def analyze(ticker: str, dossier: dict) -> dict:
    if not claude.is_configured():
        return {"ticker": ticker, "personas": [], "consensus": "LLM not configured."}
    user = f"TICKER: {ticker}\n\nDATA DOSSIER:\n{json.dumps(dossier, indent=2, default=str)}"
    raw = claude.complete(
        system=_SYSTEM, user=user, model=config.SONNET_MODEL,
        max_tokens=1500, cache_system=False,
    )
    data = claude.extract_json(raw)
    if not data.get("personas"):
        return {"ticker": ticker, "personas": [], "consensus": "Insufficient data to judge."}
    return data
