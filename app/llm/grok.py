"""xAI Grok client.

Two uses in this project:
1. Live X (Twitter) search via the server-side search tool — replaces the
   Twitter API. We batch multiple accounts into one query to cut call volume.
2. Cheap general analysis with Grok 4 Fast.

The xAI API is OpenAI-compatible; we call it over plain HTTP to avoid an extra
SDK dependency.
"""
from __future__ import annotations

import json
import logging

import requests

from app.config import config

logger = logging.getLogger(__name__)

_BASE = "https://api.x.ai/v1/chat/completions"
_TIMEOUT = 60
FAST_MODEL = "grok-4-fast"


def is_configured() -> bool:
    return bool(config.XAI_API_KEY)


def _chat(messages: list[dict], *, model: str, search: bool, max_tokens: int) -> str:
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if search:
        # Server-side live search restricted to X. Costed per call by xAI.
        payload["search_parameters"] = {"mode": "on", "sources": [{"type": "x"}]}
    try:
        resp = requests.post(
            _BASE,
            headers={
                "Authorization": f"Bearer {config.XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, ValueError):
        logger.exception("Grok request failed (model=%s, search=%s)", model, search)
        return ""


def search_x_accounts(handles: list[str], topics: list[str]) -> list[dict]:
    """Find recent on-topic posts from a batch of X accounts.

    Returns a list of dicts: {handle, text, url}. We ask Grok to return strict
    JSON so we can map results onto Item rows.
    """
    if not is_configured() or not handles:
        return []
    handle_list = ", ".join(f"@{h.lstrip('@')}" for h in handles)
    topic_list = ", ".join(topics) if topics else "their area of expertise"
    prompt = (
        f"Search X for the most recent posts (last 48 hours) from these accounts: "
        f"{handle_list}. Focus on posts about {topic_list}. "
        "Return ONLY a JSON array; each element must be an object with keys "
        '"handle" (without @), "text" (the post text), and "url" (link to the post). '
        "If an account has no relevant recent post, omit it. Max 3 posts per account."
    )
    raw = _chat(
        [{"role": "user", "content": prompt}],
        model=FAST_MODEL,
        search=True,
        max_tokens=4000,
    )
    return _parse_json_array(raw)


def _parse_json_array(raw: str) -> list[dict]:
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        text = text.lstrip("json").strip().rstrip("`").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
        return [p for p in parsed if isinstance(p, dict)]
    except json.JSONDecodeError:
        logger.warning("Could not parse Grok JSON array")
        return []
