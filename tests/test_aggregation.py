"""Tests for WS-AGG (Goal 4): scheduler hardening, parallel ingestion, ticker
N+1 fix, disk-cache cold-start survival, and the signal_fts drop migration.

All tests are offline (no network); the parallel-ingestion test monkeypatches the
ingester functions so it exercises the runner's threading + per-thread-session
contract without hitting any external host.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import text

from app.database import engine, init_db


# ── Criterion (2): scheduler sets max_instances=1 + coalesce=True on ALL jobs ──
def test_scheduler_hardening_all_jobs():
    from app.scheduler import build_scheduler

    # build_scheduler() configures jobs but does not start the scheduler.
    scheduler = build_scheduler()
    jobs = scheduler.get_jobs()
    assert len(jobs) == 9, f"expected 9 jobs, got {len(jobs)}"
    for job in jobs:
        assert job.max_instances == 1, f"{job.id}: max_instances != 1"
        assert job.coalesce is True, f"{job.id}: coalesce != True"


# ── Criterion (3): ingestion runs in parallel, each in its own session, with
#    per-ingester failure isolation preserved. ─────────────────────────────────
def test_parallel_ingestion_isolation_and_keys(monkeypatch):
    import app.ingestion.runner as runner

    init_db()
    seen_threads: set[str] = set()
    lock = threading.Lock()

    def make_ok(n):
        def _fn(session):
            # Prove each ingester gets a usable session on its own thread.
            session.execute(text("SELECT 1"))
            with lock:
                seen_threads.add(threading.current_thread().name)
            time.sleep(0.02)  # let threads overlap so we observe >1 thread
            return n
        return _fn

    def boom(session):
        raise RuntimeError("simulated ingester crash")

    # Patch every ingester module the runner imports to a fast offline stub.
    stub_counts = {
        "rss": 3, "youtube": 1, "twitter": 2, "sec": 4, "form4": 5,
        "institutional": 6, "dilution": 7, "biotech": 8, "events8k": 9,
        "form144": 10, "govcontracts": 11, "fda": 12, "openfda": 13,
        "bluesky": 14, "patent": 15, "congress": 16, "prediction": 17,
        "kalshi": 18, "unusual_whales": 19, "community": 20,
    }
    for mod_name, n in stub_counts.items():
        monkeypatch.setattr(getattr(runner, mod_name), "ingest_all", make_ok(n))
    # freshrss not configured -> runner uses rss
    monkeypatch.setattr(runner.freshrss, "is_configured", lambda: False)
    # One ingester crashes — must be isolated to a 0 count, not abort the run.
    monkeypatch.setattr(runner.patent, "ingest_all", boom)

    counts = runner.run_all_ingestion()

    # All 20 source-type keys present (rss path, not freshrss).
    expected_keys = {
        "rss", "youtube", "twitter", "sec", "form4", "institutional", "dilution",
        "biotech", "form8k", "form144", "govcontract", "fdarecall", "openfda",
        "bluesky", "patent", "congress", "prediction", "kalshi", "unusual_whales",
        "community",
    }
    assert set(counts) == expected_keys
    # Failure isolation: the crashing ingester yields 0, others their stub count.
    assert counts["patent"] == 0
    assert counts["unusual_whales"] == 19
    assert counts["form4"] == 5
    # Actually ran in parallel (more than one worker thread observed).
    assert len(seen_threads) > 1, f"expected parallel threads, saw {seen_threads}"


# ── Criterion (4): watchlist ticker refresh is parallel + batches signal flow ──
def test_signal_flow_scores_batched():
    """One batched query returns the same score signal_score computes per-symbol."""
    import json as _json
    import uuid
    from datetime import datetime, timezone

    from app.database import session_scope
    from app.models import Item, Signal, Source, SourceType
    from app.processing.tickers import signal_flow_scores

    init_db()
    # Uppercase + unique so reruns don't collide AND match the score fn's .upper().
    run_id = uuid.uuid4().hex[:8].upper()
    with session_scope() as s:
        src = Source(name=f"agg-test-src-{run_id}", type=SourceType.rss.value,
                     url=f"internal://agg-test/{run_id}")
        s.add(src)
        s.flush()
        now = datetime.now(timezone.utc)
        # Unique tickers per run so other rows in the persistent db can't skew counts.
        t_a, t_b, t_c = f"AAA{run_id}", f"BBB{run_id}", f"CCC{run_id}"
        # t_a: 2 bullish, 1 bearish -> (2-1)/3 = 0.333 ; t_b: 1 bullish -> 1.0
        specs = [(t_a, "bullish"), (t_a, "bullish"), (t_a, "bearish"), (t_b, "bullish")]
        for i, (tk, direction) in enumerate(specs):
            it = Item(source_id=src.id, title=f"t{i}", url=f"internal://agg/{run_id}/{i}",
                      content_hash=f"agghash{run_id}{i}")
            s.add(it)
            s.flush()
            s.add(Signal(item_id=it.id, signal_type="x", confidence=0.9, direction=direction,
                         summary="s", entities_json=_json.dumps({"tickers": [tk]}),
                         created_at=now))

    with session_scope() as s:
        scores = signal_flow_scores(s, {t_a, t_b, t_c})
    assert scores[t_a] == 0.333
    assert scores[t_b] == 1.0
    assert scores[t_c] == 0.0  # no signals -> neutral, still present


def test_refresh_watchlist_parallel(monkeypatch):
    """refresh_watchlist refreshes every symbol via a thread pool (no network)."""
    import app.processing.tickers as tickers
    from app.database import session_scope
    from app.models import WatchlistItem

    from sqlalchemy import select

    init_db()
    syms = {"PAR1", "PAR2", "PAR3", "PAR4"}
    with session_scope() as s:
        existing = {
            row[0] for row in s.execute(select(WatchlistItem.ticker_symbol)).all()
        }
        for sym in syms - existing:
            s.add(WatchlistItem(list_name="agg-test", ticker_symbol=sym))

    refreshed: set[str] = set()
    lock = threading.Lock()

    def fake_refresh_one(symbol, short_vol_pct, flow):
        with lock:
            refreshed.add(symbol)

    monkeypatch.setattr(tickers, "_refresh_one_symbol", fake_refresh_one)
    monkeypatch.setattr(
        tickers, "get_short_volume",
        lambda symbols: {"tickers": []}, raising=False,
    )
    # short_volume is imported inside the function; patch its module too.
    import app.processing.short_volume as sv
    monkeypatch.setattr(sv, "get_short_volume", lambda symbols: {"tickers": []})

    with session_scope() as s:
        n = tickers.refresh_watchlist_tickers(s)
    assert n >= len(syms)
    assert syms.issubset(refreshed)


# ── Criterion (5): disk cache survives a simulated cold start ──────────────────
def test_disk_cache_roundtrip():
    from app import cache

    cache.set("agg_test_key", {"hello": "world", "n": 42}, ttl=3600)
    got = cache.get("agg_test_key")
    assert got == {"hello": "world", "n": 42}


def test_funds_consensus_cold_start_from_disk(monkeypatch):
    """Write consensus to disk, clear the in-memory global, confirm cold start
    loads from disk WITHOUT triggering a network rebuild."""
    from app import cache
    from app.processing import funds

    payload = {"funds_total": 7, "as_of": "2026-05-15", "quarter": "Q1 2026",
               "consensus": [{"issuer": "NVDA", "fund_count": 5}], "computing": False}
    cache.set("fund_consensus", payload, ttl=funds._CONSENSUS_TTL)

    # Simulate a cold start: in-memory cache is empty.
    monkeypatch.setattr(funds, "_consensus_cache", None, raising=False)
    # If it tried to rebuild, this would raise — proving it loaded from disk instead.
    def _boom(*a, **k):
        raise AssertionError("cold start must not rebuild from network")
    monkeypatch.setattr(funds, "_fund_latest_rows", _boom)

    result = funds.get_consensus_cached()
    assert result["funds_total"] == 7
    assert result["consensus"][0]["issuer"] == "NVDA"


def test_macro_cold_start_from_disk(monkeypatch):
    from app import cache
    from app.processing import macro

    payload = {"indicators": [{"id": "DGS10", "label": "10Y", "value": 4.2}],
               "outlook": {"label": "neutral"}, "regime": "neutral", "updated": None}
    cache.set("macro", payload, ttl=macro._CACHE_TTL)
    monkeypatch.setattr(macro, "_cache", None, raising=False)
    monkeypatch.setattr(macro, "_cache_at", 0.0, raising=False)

    def _boom(series):
        raise AssertionError("cold start must not refetch series")
    monkeypatch.setattr(macro, "fetch_series", _boom)

    result = macro.get_macro()
    assert result["indicators"][0]["id"] == "DGS10"


# ── Criterion (6): signal_fts is gone after migrations ────────────────────────
def test_signal_fts_dropped():
    init_db()  # runs migrations, including DROP TABLE IF EXISTS signal_fts
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE name LIKE 'signal_fts%'")
        ).fetchall()
        assert rows == [], f"signal_fts* tables still present: {rows}"
        # The replacement FTS table must still exist.
        v2 = conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name='sig_fts_v2'")
        ).scalar()
        assert v2 == 1
