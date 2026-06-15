"""SEC EDGAR full-text search ingestion (free, no key).

Catches 8-K and other filings for watched companies — often the primary source
of a commercial announcement before any press release. We query EDGAR full-text
search for each SEC-type source's keyword (stored in ``handle`` or ``tags``).

API: https://efts.sec.gov/LATEST/search-index?q=...
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.ingestion import sec_throttle
from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://efts.sec.gov/LATEST/search-index"
# EDGAR requires a descriptive UA with contact per their fair-access policy.
_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_TIMEOUT = 20
MAX_HITS = 10


def ingest_source(session, source: Source) -> int:
    query = source.handle or source.tags or source.name
    forms = "8-K"
    try:
        sec_throttle.acquire()
        resp = requests.get(
            _ENDPOINT,
            headers=_HEADERS,
            params={"q": f'"{query}"', "forms": forms},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except (requests.RequestException, ValueError):
        logger.exception("EDGAR search failed for %s", source.name)
        return 0

    added = 0
    for hit in hits[:MAX_HITS]:
        src = hit.get("_source", {})
        adsh = hit.get("_id", "").split(":")[0].replace("-", "")
        cik = (src.get("ciks") or ["0"])[0].lstrip("0")
        doc = hit.get("_id", "").split(":")[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{doc}"
        published = None
        if src.get("file_date"):
            try:
                published = datetime.fromisoformat(src["file_date"]).replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        title = f"{src.get('display_names', [query])[0]} — {src.get('file_type', forms)}"
        item = upsert_item(
            session,
            source=source,
            title=title,
            url=url,
            content=" ".join(src.get("display_names", [])),
            author="SEC EDGAR",
            media_type="filing",
            published_at=published,
        )
        if item is not None:
            added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.sec.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("SEC ingest error for %s", source.name)
    return total
