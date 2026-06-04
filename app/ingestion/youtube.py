"""YouTube ingestion via the Data API v3 + transcript extraction.

Transcript-first: each new video is turned into searchable text so the
classifier can score it like any article. Channel handles/URLs are resolved to
their uploads playlist, then ``playlistItems.list`` (1 quota unit) pulls recent
videos cheaply.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.config import config
from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

MAX_VIDEOS = 10
_HANDLE_RE = re.compile(r"(?:youtube\.com/(?:@|channel/|c/|user/)?|^@)([\w\-.]+)")


def _client():
    if not config.YOUTUBE_API_KEY:
        return None
    try:
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("google-api-python-client not installed; skipping YouTube")
        return None
    return build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY, cache_discovery=False)


def _resolve_uploads_playlist(yt, source: Source) -> str | None:
    """Resolve a channel id/handle/url to its uploads playlist id."""
    channel_id = None
    raw = source.handle or source.url
    if "channel/" in raw:
        channel_id = raw.split("channel/", 1)[1].split("/")[0]

    if channel_id:
        resp = yt.channels().list(part="contentDetails", id=channel_id).execute()
    else:
        match = _HANDLE_RE.search(raw)
        handle = match.group(1) if match else raw.lstrip("@")
        resp = yt.channels().list(part="contentDetails", forHandle=handle).execute()

    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def _transcript(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        chunks = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(c["text"] for c in chunks)
    except Exception:
        return ""  # transcripts often disabled; title+description still useful


def ingest_source(session, source: Source) -> int:
    yt = _client()
    if yt is None:
        return 0
    try:
        playlist_id = _resolve_uploads_playlist(yt, source)
    except Exception:
        logger.exception("YouTube channel resolve failed for %s", source.name)
        return 0
    if not playlist_id:
        return 0

    resp = (
        yt.playlistItems()
        .list(part="snippet", playlistId=playlist_id, maxResults=MAX_VIDEOS)
        .execute()
    )
    added = 0
    for entry in resp.get("items", []):
        snippet = entry["snippet"]
        video_id = snippet["resourceId"]["videoId"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        published = None
        if snippet.get("publishedAt"):
            try:
                published = datetime.fromisoformat(
                    snippet["publishedAt"].replace("Z", "+00:00")
                )
            except ValueError:
                published = datetime.now(timezone.utc)
        body = snippet.get("description", "")
        transcript = _transcript(video_id)
        if transcript:
            body = f"{body}\n\n[TRANSCRIPT]\n{transcript}"
        item = upsert_item(
            session,
            source=source,
            title=snippet.get("title", ""),
            url=url,
            content=body,
            author=snippet.get("channelTitle", source.name),
            media_type="video",
            published_at=published,
        )
        if item is not None:
            added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.youtube.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("YouTube ingest error for %s", source.name)
    return total
