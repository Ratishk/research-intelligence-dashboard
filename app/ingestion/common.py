"""Shared ingestion helpers: URL normalization, dedupe hashing, item upsert."""
from __future__ import annotations

import datetime as _dt
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Item, Source

# Tracking params stripped so the same article from two referrers dedupes.
# _TRACKING_EXACT: stripped only when the key matches exactly.
# _TRACKING_PREFIXES: stripped when the key starts with the prefix.
_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
_TRACKING_PREFIXES = ("utm_",)


def normalize_url(url: str) -> str:
    """Lowercase host, drop fragments and tracking query params, strip trailing /."""
    if not url:
        return ""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url.strip()

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    query = parts.query
    if query:
        kept = []
        for kv in query.split("&"):
            if not kv:
                continue
            key = kv.split("=", 1)[0].lower()
            if key in _TRACKING_EXACT or any(key.startswith(p) for p in _TRACKING_PREFIXES):
                continue
            kept.append(kv)
        query = "&".join(sorted(kept))

    path = parts.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", query, ""))


def content_hash(url: str, fallback_text: str = "") -> str:
    """Stable dedupe key. Uses normalized URL when present, else text digest."""
    basis = normalize_url(url) or fallback_text.strip()
    return hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()


def new_since_cursor(source: Source, filing_dates: list[str]) -> set[int]:
    """Indices of filings filed on/after the source's last_crawled cursor (work dedup).

    SEC submission-history walks re-list ALL recent filings every pass. Once a source
    has been crawled, only filings filed on or after the cursor date can be new (one
    day of slack, since filingDate is date-granular and last_crawled is a timestamp).
    First crawl (no cursor) processes everything. Always pair with stamp_crawled() so
    the cursor advances even when a pass finds nothing new.
    """
    n = len(filing_dates)
    cursor = source.last_crawled
    if cursor is None:
        return set(range(n))
    cutoff = (cursor.date() - _dt.timedelta(days=1))
    keep: set[int] = set()
    for i, ds in enumerate(filing_dates):
        try:
            fd = _dt.date.fromisoformat((ds or "")[:10])
        except (ValueError, TypeError):
            keep.add(i)  # unparseable date -> don't silently skip it
            continue
        if fd >= cutoff:
            keep.add(i)
    return keep


def stamp_crawled(source: Source) -> None:
    """Advance the source's last_crawled cursor to now (call once per crawl)."""
    source.last_crawled = datetime.now(timezone.utc)


def upsert_item(
    session: Session,
    *,
    source: Source,
    title: str,
    url: str,
    content: str = "",
    author: str = "",
    media_type: str = "article",
    published_at: datetime | None = None,
) -> Item | None:
    """Insert an item if its content_hash is new. Returns the Item or None if dup.

    The unique constraint on content_hash also guards against races; we check
    first to avoid a flush exception on the common duplicate path.
    """
    digest = content_hash(url, f"{source.id}:{title}")
    existing = session.scalar(select(Item.id).where(Item.content_hash == digest))
    if existing:
        return None

    item = Item(
        source_id=source.id,
        title=(title or "").strip()[:600],
        url=url.strip()[:800],
        content_hash=digest,
        content=(content or "").strip(),
        author=(author or "").strip()[:200],
        media_type=media_type,
        published_at=published_at,
    )
    session.add(item)
    try:
        session.flush([item])
    except IntegrityError:
        session.rollback()
        return None
    source.last_crawled = datetime.now(timezone.utc)
    return item
