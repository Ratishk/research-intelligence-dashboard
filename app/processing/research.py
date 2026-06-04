"""On-demand research briefs for signals via Perplexity Sonar Pro."""
from __future__ import annotations

import json
import logging

from app.llm import perplexity
from app.models import Signal

logger = logging.getLogger(__name__)


def generate_brief(session, signal: Signal) -> str:
    """Produce a cited investment-significance brief and persist it on the signal."""
    entities = {}
    try:
        entities = json.loads(signal.entities_json or "{}")
    except json.JSONDecodeError:
        pass
    companies = ", ".join(entities.get("companies", [])) or "the relevant companies"
    prompt = (
        "You are an equity research analyst. Explain the investment significance "
        f"of this development: \"{signal.summary}\". Cover: what it means for "
        f"{companies}, second-order effects on the supply chain or competitors, "
        "and whether this signals real adoption versus R&D. Be concise and cite sources."
    )
    result = perplexity.ask(prompt, model=perplexity.SONAR_PRO, max_tokens=900)
    if not result:
        return ""
    text = result.get("text", "")
    citations = result.get("citations", [])
    if citations:
        links = "\n".join(
            f"- {c if isinstance(c, str) else c.get('url', '')}" for c in citations
        )
        text = f"{text}\n\nSources:\n{links}"
    signal.research_brief = text
    session.add(signal)
    return text
