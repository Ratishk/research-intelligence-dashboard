# RAG Phase 2 — LogiqGPT-style hybrid semantic retrieval

**Status:** planned (not built). Phase 1 (lean FTS5) is live in `app/processing/rag.py`.

This plan mirrors the architecture in your LogiqGPT app (`services/rag_retrieval.py`,
`services/rag_query.py`, `services/memory.py`) and adapts it to this dashboard. Every
weight, model, and default below is taken from LogiqGPT's actual code so Phase 2 is a
faithful port, not a re-invention.

---

## 1. Why Phase 1 is enough *today* (and when it stops being enough)

Phase 1 works because our signals are **atomic, pre-tagged, short chunks** — one signal =
one summary + direction + type + tickers. Keyword FTS5 + structural ranking (ticker boost,
recency, confidence) retrieves them well, and the enriched index (baked-in category vocab +
stemming) covers most paraphrase. Retrieval is **free and local**; only the answer spends tokens.

LogiqGPT needs more because it retrieves over **long unstructured documents** — PDFs, DOCX,
XLSX, SharePoint files, earnings/media transcripts — that must be chunked and matched
*semantically*. Keyword search fails there: a query for "margin compression" won't match a
filing that says "gross margin declined 300bps."

**Build Phase 2 when we start ingesting long-form content where keyword search breaks:**

- Full-text SEC 10-K / 10-Q / 8-K bodies (not just the filing-type signal we emit now)
- Earnings-call transcripts
- Full news articles & research PDFs
- Long YouTube transcripts

Concrete trigger: when an item's text routinely exceeds ~1–2k chars, **or** when `/api/ask`
visibly misses answers that require semantic (not lexical) match. Until then, Phase 2's
~2GB of ML dependencies aren't justified.

---

## 2. Target architecture (faithful port of LogiqGPT)

```
question ─► query parser ──► ┌─ FTS5 (keyword)      ─┐
                             ├─ Chroma (semantic)   ─┤─► weighted merge ─► cross-encoder ─► top-k ─► generate()
                             └─ structural (ticker/  │   0.55/0.35/0.10     rerank (gated)   chunks    (tokens)
                                recency/path bonus)  ─┘
   everything left of generate() is LOCAL + FREE
```

### 2a. Chunking layer — new `app/processing/chunk.py`
- Long docs → ~800-token chunks, ~120-token overlap, split on paragraph→sentence boundaries
  (RecursiveCharacter-style). Atomic signals stay a single chunk (already true today).
- Each chunk carries metadata, mirroring LogiqGPT `save_to_memory`'s metadata dict
  (`source_id`, `source_title`, `source_type`, `section`, timestamps): for us →
  `item_id`, `signal_id`, `source_type`, `tickers`, `direction`, `published_at`, `section`.
- Chunk IDs follow LogiqGPT's `f"{source_id}_{i}"` convention → `f"{item_id}_{i}"`.

### 2b. Embeddings + vector store — new `app/processing/vectors.py`
- **Chroma `PersistentClient(path="data/chroma")`**, collection `signal_chunks`
  (LogiqGPT uses the same client at `data/chroma`, collection `logiq_memory`).
- **Embedding model = Chroma's built-in `all-MiniLM-L6-v2`** (ONNX, CPU, ~80MB, downloads
  once). LogiqGPT's `save_to_memory` never passes a custom `embedding_function` — it relies
  on this exact default. **No embedding API → retrieval stays $0.**
- Incremental indexing: embed only new chunks, reusing the `MAX(id)` watermark pattern from
  Phase 1's `ensure_index`. Add `ensure_vectors(db)` alongside `ensure_index(db)`.
- Local CPU embeddings have **no network call**, so the datacenter-IP blocks that hit
  yfinance/StockTwits/FRED do **not** apply here.

### 2c. Hybrid retrieval — rewrite `retrieve()` to merge (LogiqGPT weights, verbatim)
From `rag_retrieval.py`:
```
_W_FTS = 0.35   _W_SEMANTIC = 0.55   _W_PATH = 0.10
_EXPLICIT_FILE_BONUS = 2.0   _CATALOG_INDEXED_BONUS = 1.5
```
- Run FTS5 and Chroma concurrently; normalize each score to [0,1].
- `score = 0.55*semantic + 0.35*fts + 0.10*structural`, where **structural** = our existing
  named-ticker hard-include (+5.0) + recency + confidence blend (reused from Phase 1 — it
  maps onto LogiqGPT's path/explicit/catalog bonuses).
- Port LogiqGPT's **on-miss fallback bonuses** (`_ON_MISS_EXPLICIT_BONUS=100`,
  `_ON_MISS_TICKER_BONUS=50`, `_ON_MISS_PATH_BONUS=30`, `_ON_MISS_FTS_BASE=10`): when the
  semantic stage misses but the user named a ticker/explicit item, still surface it.

### 2d. Cross-encoder reranking — the precision stage (env-gated, exactly like LogiqGPT)
From `rag_retrieval.py`:
- Model `cross-encoder/ms-marco-MiniLM-L-6-v2`, **lazy-loaded**, gated by
  `RAG_CROSS_ENCODER_ENABLED` (default off).
- Hybrid stage returns ~40 candidates → rerank `(question, chunk_text)` pairs → keep top 15.
- Port `_rerank_with_cross_encoder` verbatim, including its `try/except → return
  candidates[:top_n]` fallback so a model-load failure degrades gracefully (a core dashboard
  convention).
- Still **$0** (local CPU). Adds ~200–500ms; keep off by default, flip on for "deep" mode.

### 2e. Two-stage doc→chunk top-k (LogiqGPT defaults)
From `rag_query.py`: `doc_top_k=6` (max 20), `chunk_top_k=18` (max 60),
`max_chunks_per_doc=4`, `min_chunks_threshold=3`.
- Long docs: pick top docs first, then top chunks within, capping chunks/doc so one 10-K
  can't dominate the context window. For atomic signals this collapses to one-chunk-per-doc
  (i.e. today's behavior), so the same code path serves both.

### 2f. Query modes (optional, LogiqGPT parity)
`rag_query.py` parses `@brief` / `@deep` and inline `docs=`, `chunks=`, `topk=` overrides,
plus a ticker denylist (`AI`, `CEO`, `SEC`, `PDF`…). Adopt at least:
- `@deep` → enable cross-encoder + larger chunk_top_k; `@brief` → smaller.
- A ticker denylist so words like "AI"/"CEO" aren't treated as tickers (our current
  `detect_tickers` already filters by watchlist, which mostly covers this).

---

## 3. Cost model

| Stage | Phase 1 (now) | Phase 2 |
|---|---|---|
| Indexing | $0 (SQLite FTS5) | $0 (local MiniLM embeddings, CPU) |
| Retrieval | $0 | $0 (FTS5 + Chroma + local cross-encoder) |
| Generation | tokens (unchanged) | tokens (unchanged) |
| One-time | — | ~80MB embed model + ~80MB cross-encoder download |
| Footprint | negligible | +`chromadb`, +`sentence-transformers` (+torch CPU ≈ 2GB deps), index RAM/disk |

**Retrieval remains free.** The only real cost is dependency weight (torch) and disk — which
is precisely why this is Phase 2, gated behind long-document ingestion.

---

## 4. Backward-compatibility & migration

- Phase 1 FTS stays the **fallback**: if Chroma deps are absent or `RAG_VECTOR_ENABLED=false`,
  `retrieve()` uses today's FTS-only path (graceful degradation — the dashboard's standard pattern).
- `/api/ask`'s external contract is unchanged; only `retrieve()` internals grow.
- One-time backfill script embeds existing signals/long-docs into Chroma (`data/chroma`,
  gitignored like `research.db`).
- New env: `RAG_VECTOR_ENABLED`, `RAG_CROSS_ENCODER_ENABLED`, `CHROMA_PATH`,
  `RAG_DEFAULT_DOC_TOP_K`, `RAG_DEFAULT_CHUNK_TOP_K`, `RAG_MAX_CHUNKS_PER_DOC` — all mirroring
  LogiqGPT's `_env_int` knobs so tuning transfers across both apps.

## 5. Risks / decisions
- **Dependency weight** (torch ~2GB) is the main cost — acceptable once long docs exist, not before.
- **Index growth**: thousands of atomic signals is trivial; long-doc chunks reach ~10s of MB — fine for SQLite+Chroma on one box.
- **Pin the embedding + cross-encoder model versions** for reproducible scores.
- **No new network egress** at retrieval time → no datacenter-IP rate-limit exposure.

## 6. Verification plan
A/B the same question set through Phase-1 FTS vs Phase-2 hybrid. Focus on **paraphrase
queries Phase 1 misses today** ("margin compression" ↔ "gross margin declined 300bps"). Phase 2
ships only if it measurably lifts recall on that class without regressing the atomic-signal cases.

---

### Build order (when triggered)
1. `chunk.py` + long-doc ingestion for one source (e.g. 10-K bodies).
2. `vectors.py` (Chroma + incremental embed) + backfill script.
3. Hybrid merge in `retrieve()` behind `RAG_VECTOR_ENABLED`.
4. Cross-encoder rerank behind `RAG_CROSS_ENCODER_ENABLED`.
5. Two-stage doc→chunk top-k + `@deep`/`@brief` modes.
6. A/B verification, then default-on.
