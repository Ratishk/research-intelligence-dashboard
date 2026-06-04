"""FreshRSS GReader-compatible API client (optional scalable RSS backbone).

When FRESHRSS_* env vars are set, the scheduler can read from a self-hosted
FreshRSS instance (which handles feed scheduling/dedup/storage for thousands of
feeds) instead of polling each feed directly. Items are mapped onto a synthetic
"FreshRSS" source if a matching registry source can't be resolved by stream.

API reference: https://github.com/FreshRSS/FreshRSS/blob/edge/docs/en/developers/06_GoogleReader_API.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.config import config
from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_TIMEOUT = 20


def is_configured() -> bool:
    return bool(
        config.FRESHRSS_API_URL
        and config.FRESHRSS_API_USER
        and config.FRESHRSS_API_PASSWORD
    )


def _auth_token() -> str | None:
    """ClientLogin: exchange user/password for an Auth token."""
    url = config.FRESHRSS_API_URL.rstrip("/") + "/api/greader.php/accounts/ClientLogin"
    try:
        resp = requests.post(
            url,
            data={
                "Email": config.FRESHRSS_API_USER,
                "Passwd": config.FRESHRSS_API_PASSWORD,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("FreshRSS ClientLogin failed")
        return None
    for line in resp.text.splitlines():
        if line.startswith("Auth="):
            return line[len("Auth=") :]
    return None


def _get_or_create_backbone_source(session) -> Source:
    """A catch-all source representing items pulled via FreshRSS."""
    src = (
        session.query(Source)
        .filter(Source.type == SourceType.rss.value, Source.name == "FreshRSS Backbone")
        .first()
    )
    if src is None:
        src = Source(
            name="FreshRSS Backbone",
            type=SourceType.rss.value,
            url=config.FRESHRSS_API_URL,
            discovered_by="seed",
            tier=1,
        )
        session.add(src)
        session.flush()
    return src


def ingest_all(session, max_items: int = 500) -> int:
    """Pull recent unread items from FreshRSS. Returns count added."""
    if not is_configured():
        return 0
    token = _auth_token()
    if not token:
        return 0

    url = (
        config.FRESHRSS_API_URL.rstrip("/")
        + "/api/greader.php/reader/api/0/stream/contents/reading-list"
    )
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"GoogleLogin auth={token}"},
            params={"n": max_items, "xt": "user/-/state/com.google/read"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        logger.exception("FreshRSS stream fetch failed")
        return 0

    backbone = _get_or_create_backbone_source(session)
    added = 0
    for entry in payload.get("items", []):
        canonical = entry.get("canonical") or entry.get("alternate") or []
        link = canonical[0].get("href", "") if canonical else ""
        if not link:
            continue
        published = None
        if entry.get("published"):
            try:
                published = datetime.fromtimestamp(entry["published"], tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                published = None
        body = (entry.get("summary") or {}).get("content", "")
        item = upsert_item(
            session,
            source=backbone,
            title=entry.get("title", ""),
            url=link,
            content=body,
            author=entry.get("author", ""),
            media_type="article",
            published_at=published,
        )
        if item is not None:
            added += 1
    return added
