"""Anthropic Claude client with prompt caching.

Two model roles:
- Haiku: cheap relevance pre-filter over every ingested item.
- Sonnet: structured signal classification + investment narrative on the
  subset that passes the filter.

The large static system prompt (tracked companies/technologies, rubric) is
marked with cache_control so repeated calls hit the prompt cache.
"""
from __future__ import annotations

import contextvars
import json
import logging

from app.config import config

logger = logging.getLogger(__name__)

# Per-request token accounting. Call start_usage() at the top of a request, then
# get_usage() after — every complete()/web_search() in between adds to the tally.
_usage_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "claude_usage", default=None
)


def start_usage() -> None:
    _usage_var.set({"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "calls": 0})


def get_usage() -> dict | None:
    return _usage_var.get()


def _track(resp) -> None:
    tally = _usage_var.get()
    if tally is None:
        return
    u = getattr(resp, "usage", None)
    if u is None:
        return
    tally["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    tally["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    tally["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
    tally["calls"] += 1


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
        _track(resp)
        return "".join(block.text for block in resp.content if block.type == "text")
    except Exception:
        logger.exception("Claude completion failed (model=%s)", model)
        return ""


def web_search(prompt: str, *, model: str | None = None, max_tokens: int = 1024) -> dict:
    """Live web read via Claude's built-in server-side web_search tool.

    Returns {"text": ..., "citations": [{"url","title"}, ...]} or {} on failure.
    Uses the existing Anthropic key — no separate search vendor needed.
    """
    client = _client()
    if client is None:
        return {}
    model = model or config.SONNET_MODEL
    tools = [{"type": "web_search_20260209", "name": "web_search"}]
    messages = [{"role": "user", "content": prompt}]
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages, tools=tools
        )
        _track(resp)
        # The server runs its own search loop; if it pauses at the iteration cap,
        # re-send to let it finish (bounded so we never loop forever).
        guard = 0
        while getattr(resp, "stop_reason", None) == "pause_turn" and guard < 3:
            messages.append({"role": "assistant", "content": resp.content})
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages, tools=tools
            )
            _track(resp)
            guard += 1
        text_parts: list[str] = []
        citations: list[dict] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
                for c in (getattr(block, "citations", None) or []):
                    url = getattr(c, "url", None)
                    if url:
                        citations.append({"url": url, "title": getattr(c, "title", "") or ""})
        seen: set[str] = set()
        uniq = []
        for c in citations:
            if c["url"] not in seen:
                seen.add(c["url"])
                uniq.append(c)
        return {"text": "".join(text_parts).strip(), "citations": uniq[:8]}
    except Exception:
        logger.exception("Claude web_search failed (model=%s)", model)
        return {}


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
