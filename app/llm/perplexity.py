"""Perplexity Sonar client for cited research briefs and source discovery.

Used in two places:
- On-demand "Get Research Brief" for a signal (Sonar Pro, with citations).
- The discovery engine's source-finding queries (cheaper Sonar).
"""
from __future__ import annotations

import json
import logging

import requests

from app.config import config

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.perplexity.ai/chat/completions"
_TIMEOUT = 60
SONAR = "sonar"
SONAR_PRO = "sonar-pro"


def is_configured() -> bool:
    return bool(config.PERPLEXITY_API_KEY)


def ask(prompt: str, *, model: str = SONAR_PRO, max_tokens: int = 1024) -> dict:
    """Return {"text": ..., "citations": [...]} or empty on failure."""
    if not is_configured():
        return {}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {config.PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        logger.exception("Perplexity request failed")
        return {}
    text = data["choices"][0]["message"]["content"]
    citations = data.get("citations", []) or data.get("search_results", [])
    return {"text": text, "citations": citations}
