"""Discover candidate sources for an industry via Perplexity (with Grok fallback).

Returns lightweight candidate dicts; the scorer vets them before they enter the
registry as ``candidate`` status.
"""
from __future__ import annotations

import json
import logging

from app.llm import grok, perplexity

logger = logging.getLogger(__name__)


def _prompt(industry_name: str, existing: list[str]) -> str:
    avoid = ", ".join(existing[:40]) if existing else "none yet"
    return (
        f"List the most credible, frontier information sources for the "
        f"'{industry_name}' sector for an investment research pipeline. Each must "
        "be either highly technical in the industry, highly technical in finance, "
        "or both, with a track record of original analysis. Exclude these already "
        f"tracked: {avoid}. Return ONLY a JSON array; each element an object with "
        'keys "name", "type" (one of youtube, twitter, rss, forum, sec, reddit, '
        'hackernews, arxiv), "url", "handle" (X handle or channel id if relevant), '
        'and "reason" (1 sentence of credibility evidence). Return up to 15.'
    )


def _parse_array(raw: str) -> list[dict]:
    if not raw:
        return []
    text = raw.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for p in parsed:
        if isinstance(p, dict) and p.get("name") and p.get("type"):
            out.append(p)
    return out


def find_candidates(industry_name: str, existing_names: list[str]) -> list[dict]:
    prompt = _prompt(industry_name, existing_names)
    if perplexity.is_configured():
        result = perplexity.ask(prompt, model=perplexity.SONAR, max_tokens=2000)
        candidates = _parse_array(result.get("text", "")) if result else []
        if candidates:
            return candidates
    # Fallback to Grok (non-search general knowledge) if Perplexity is absent.
    if grok.is_configured():
        raw = grok._chat(
            [{"role": "user", "content": prompt}],
            model=grok.FAST_MODEL,
            search=False,
            max_tokens=2000,
        )
        return _parse_array(raw)
    return []
