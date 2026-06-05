"""Patent ingestion via the Google Patents XHR JSON endpoint.

FreePatentsOnline/Justia RSS feeds are unreliable (HTML responses, Cloudflare),
so we query Google Patents' public XHR endpoint directly. Each patent source
stores its search keyword in ``handle`` (or ``tags``); results are sorted newest
first so recent filings surface. Patent sources score 3/3 on alpha originality.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://patents.google.com/xhr/query"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
_TIMEOUT = 20
MAX_PATENTS = 15


def _strip_tags(text: str) -> str:
    """Google snippets contain <b> highlight tags — drop them."""
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


def ingest_source(session, source: Source) -> int:
    keyword = (source.handle or source.tags or source.name).strip()
    if not keyword:
        return 0
    # url=q=<keyword>&sort=new — the whole thing is URL-encoded as one param value.
    inner = f"q={quote(keyword)}&sort=new"
    try:
        resp = requests.get(
            _ENDPOINT,
            params={"url": inner, "exp": ""},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        clusters = resp.json().get("results", {}).get("cluster", [])
    except Exception:
        logger.exception("Google Patents query failed for %s", source.name)
        return 0

    results = []
    for cluster in clusters:
        results.extend(cluster.get("result", []))

    added = 0
    for r in results[:MAX_PATENTS]:
        p = r.get("patent", {})
        pub_num = p.get("publication_number", "")
        if not pub_num:
            continue
        title = _strip_tags(p.get("title", ""))
        snippet = _strip_tags(p.get("snippet", ""))
        assignee = p.get("assignee", "")
        url = f"https://patents.google.com/patent/{pub_num}/en"
        published = None
        if p.get("publication_date"):
            try:
                published = datetime.fromisoformat(p["publication_date"]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        content = f"{title}\n\n{snippet}".strip()
        item = upsert_item(
            session,
            source=source,
            title=f"[{pub_num}] {title}"[:300],
            url=url,
            content=content,
            author=assignee,
            media_type="patent",
            published_at=published,
        )
        if item is not None:
            added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.patent.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("Patent ingest error for %s", source.name)
    return total
