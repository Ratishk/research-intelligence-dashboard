"""Anthropic Claude client with prompt caching.

Two model roles:
- Haiku: cheap relevance pre-filter over every ingested item.
- Sonnet: structured signal classification + investment narrative on the
  subset that passes the filter.

The large static system prompt (tracked companies/technologies, rubric) is
marked with cache_control so repeated calls hit the prompt cache.
"""
from __future__ import annotations

import json
import logging

from app.config import config

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def _client():
    if not is_configured():
        return None
    try:
        import anthropic

        return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    except ImportError:
        logger.warning("anthropic SDK not installed")
        return None


def complete(
    *,
    system: str | list,
    user: str,
    model: str,
    max_tokens: int = 1024,
    cache_system: bool = True,
) -> str:
    """Single-turn completion. ``system`` may be a string or content blocks.

    When cache_system is True and system is a string, it's wrapped in a single
    cache_control block so the static prefix is cached across calls.
    """
    client = _client()
    if client is None:
        return ""
    if isinstance(system, str) and cache_system:
        system_param = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    else:
        system_param = system
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_param,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")
    except Exception:
        logger.exception("Claude completion failed (model=%s)", model)
        return ""


def extract_json(raw: str) -> dict:
    """Best-effort parse of a JSON object from a model response."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.lstrip("json").strip().rstrip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("Could not parse Claude JSON object")
        return {}
