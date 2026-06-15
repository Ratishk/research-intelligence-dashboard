"""Connector to the LogiqGPT research corpus.

LogiqGPT is the firm's research platform; we read its knowledge directly (no
Streamlit). Two sources:
  - `sharepoint_catalog.db` — 11k+ firm SharePoint docs indexed by ticker
    (investment memos, models, dilution analyses). Plain SQLite, always reliable.
  - `data/chroma` ChromaDB (`logiq_memory`) — semantically-searchable chunks of
    ingested SEC filings, memos, transcripts. Best-effort (needs the embedding
    model the corpus was built with); we degrade to the catalog if it fails.

Everything degrades gracefully — LogiqGPT is optional.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from app.config import config

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 1800  # seconds
_collection = None
_collection_tried = False


def _data_dir() -> Path:
    return Path(config.LOGIQGPT_PATH) / "data"


def firm_docs(ticker: str, limit: int = 8) -> list[dict]:
    """Firm SharePoint documents for a ticker (memos, models), newest first."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return []
    db = _data_dir() / "sharepoint_catalog.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT filename, path, ingest_status, web_url, modified "
                "FROM catalog_files WHERE ticker = ? "
                "ORDER BY modified DESC LIMIT ?",
                (sym, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.info("firm_research: sharepoint catalog query failed for %s", sym)
        return []
    return [
        {"filename": r[0], "path": r[1], "status": r[2],
         "web_url": r[3], "modified": r[4]}
        for r in rows
    ]


def _get_collection():
    """Lazily open the LogiqGPT ChromaDB collection (cached). None if unavailable."""
    global _collection, _collection_tried
    if _collection_tried:
        return _collection
    _collection_tried = True
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(_data_dir() / "chroma"))
        _collection = client.get_collection("logiq_memory")
    except Exception:
        logger.info("firm_research: ChromaDB unavailable", exc_info=True)
        _collection = None
    return _collection


def research_chunks(query: str, k: int = 5) -> list[dict]:
    """Semantically-relevant research chunks for a query (best-effort)."""
    col = _get_collection()
    if col is None or not query:
        return []
    try:
        res = col.query(query_texts=[query], n_results=k)
    except Exception:
        logger.info("firm_research: chroma query failed", exc_info=True)
        return []
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    out = []
    for i, text in enumerate(docs):
        m = metas[i] if i < len(metas) else {}
        out.append({
            "text": (text or "")[:600],
            "source": m.get("source_title"),
            "type": m.get("source_type"),
        })
    return out


def get_research(query: str, ticker: str | None = None, k: int = 5) -> dict | None:
    """Firm research for the ask: SharePoint memos for the ticker + semantic chunks.

    Returns None when there's nothing (no docs and no chunks)."""
    key = f"{(ticker or '').upper()}::{(query or '')[:80]}"
    cached = _cache.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    docs = firm_docs(ticker) if ticker else []
    q = query if not ticker else f"{ticker} {query}"
    chunks = research_chunks(q, k=k)
    result = None
    if docs or chunks:
        result = {"firm_docs": docs, "chunks": chunks,
                  "source": "LogiqGPT (SharePoint catalog + ChromaDB)"}
    _cache[key] = (time.time(), result)
    return result
