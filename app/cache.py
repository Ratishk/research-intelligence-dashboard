"""Tiny disk-backed cache so hot in-process caches survive a restart.

Expensive aggregates (the 400-fund 13F consensus, macro series, smart-money flow)
are cached in module globals with a TTL. On a cold start those globals are empty,
so the first request after a restart pays the full rebuild cost (minutes, for the
13F consensus). This module persists those payloads to a `cache` table in the same
SQLite DB, keyed by name with a stored TTL, so cold start can load-from-disk and
warm the in-memory cache instead of re-scraping.

Usage pattern (alongside the existing in-memory TTL):
    from app import cache
    # on write (after computing `result`):
    cache.set("fund_consensus", result, ttl=80*24*3600)
    # on cold start (in-memory global is empty):
    hit = cache.get("fund_consensus")   # None if missing or expired
    if hit is not None:
        _in_memory_cache = hit

Values must be JSON-serializable. Failures degrade to a miss / no-op — the disk
cache is an optimization, never a correctness dependency.
"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)

_DDL = (
    "CREATE TABLE IF NOT EXISTS cache ("
    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
    "stored_at REAL NOT NULL, ttl REAL NOT NULL)"
)
_ensured = False


def _ensure_table() -> None:
    global _ensured
    if _ensured:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(_DDL))
        _ensured = True
    except Exception:
        logger.warning("cache table create failed; disk cache disabled", exc_info=True)


def set(key: str, value, ttl: float) -> None:
    """Persist a JSON-serializable value under `key` with a TTL (seconds)."""
    _ensure_table()
    try:
        payload = json.dumps(value)
    except (TypeError, ValueError):
        logger.warning("cache.set: value for %s not JSON-serializable; skipping", key)
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cache (key, value, stored_at, ttl) "
                    "VALUES (:k, :v, :s, :t) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, stored_at=excluded.stored_at, ttl=excluded.ttl"
                ),
                {"k": key, "v": payload, "s": time.time(), "t": float(ttl)},
            )
    except Exception:
        logger.warning("cache.set failed for %s", key, exc_info=True)


def get(key: str):
    """Return the cached value for `key`, or None if missing/expired/corrupt."""
    _ensure_table()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value, stored_at, ttl FROM cache WHERE key = :k"),
                {"k": key},
            ).first()
    except Exception:
        logger.warning("cache.get failed for %s", key, exc_info=True)
        return None
    if row is None:
        return None
    value, stored_at, ttl = row
    if (time.time() - stored_at) >= ttl:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
