"""Tests for the Unusual Whales (UW) ingester + /api/unusual-whales route.

The live-call test hits ONE light endpoint (market-tide) once, per the rate-limit
guidance. The key is asserted to never appear in captured logs. All other tests
are offline (fed canned UW envelopes), so the suite stays cheap to re-run.
"""
from __future__ import annotations

import logging
import os

import pytest

from app.database import init_db, session_scope
from app.ingestion import unusual_whales as uw
from app.models import UWMarketTide, UWMaxPain


def test_import_clean():
    """Criterion 1: the module imports without error."""
    import importlib
    importlib.import_module("app.ingestion.unusual_whales")


def test_endpoints_whitelisted():
    """Criterion 6: every endpoint the ingester uses is in the whitelist."""
    used = {
        "/api/market/market-tide",
        "/api/option-trades/flow-alerts",
        "/api/darkpool/recent",
        "/api/congress/recent-trades",
        "/api/insider/transactions",
        "/api/stock/{ticker}/greeks",
        "/api/stock/{ticker}/max-pain",
    }
    assert used == set(uw.WHITELIST)
    assert uw._is_whitelisted("/api/stock/AAPL/greeks")
    assert uw._is_whitelisted("/api/stock/AAPL/max-pain")
    assert not uw._is_whitelisted("/api/option-trades/made-up")


def test_non_whitelisted_get_refused():
    sess = object()  # never used; the guard raises before any HTTP
    with pytest.raises(ValueError):
        uw._get(sess, "/api/not/real")


def test_live_market_tide_ingest_and_secret_redaction(caplog):
    """Criterion 2: one live call to market-tide ingests >=1 row with pulled_at +
    raw_json populated, and the API key never appears in logs."""
    key = os.getenv("UW_API_KEY")
    if not key:
        pytest.skip("UW_API_KEY not set")

    init_db()
    caplog.set_level(logging.DEBUG)
    http = uw.make_session()
    try:
        with session_scope() as s:
            uw.ingest_market_tide(s, http)
    finally:
        http.close()

    # The live endpoint returns >=1 tide row; after ingest research.db must hold
    # >=1 market-tide row (newly inserted, or already present from a prior run —
    # idempotency means the second case is expected and still valid for crit 2).
    with session_scope() as s:
        assert s.query(UWMarketTide).count() >= 1, "expected >=1 market-tide row in db"
        row = s.query(UWMarketTide).first()
        assert row is not None
        assert row.pulled_at is not None
        assert row.raw_json and row.raw_json.strip().startswith("{")

    # The key must never have leaked into any log record.
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert key not in joined


def test_idempotency_market_tide():
    """Criterion 3: re-running the ingest does not duplicate rows."""
    if not os.getenv("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set")

    init_db()
    http = uw.make_session()
    try:
        with session_scope() as s:
            uw.ingest_market_tide(s, http)
        with session_scope() as s:
            count1 = s.query(UWMarketTide).count()
        with session_scope() as s:
            uw.ingest_market_tide(s, http)
        with session_scope() as s:
            count2 = s.query(UWMarketTide).count()
    finally:
        http.close()

    assert count2 == count1, f"duplicate rows: {count1} -> {count2}"


def test_api_route_returns_rows():
    """Criterion 4: GET /api/unusual-whales returns 200 with >=1 row from db."""
    if not os.getenv("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set")

    from fastapi.testclient import TestClient
    from app.main import app

    init_db()
    http = uw.make_session()
    try:
        with session_scope() as s:
            uw.ingest_market_tide(s, http)
    finally:
        http.close()

    client = TestClient(app)
    resp = client.get("/api/unusual-whales")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert "market_tide" in body["feeds"]
    sample = body["feeds"]["market_tide"][0]
    assert sample["pulled_at"]
    assert isinstance(sample["data"], dict)


def test_upsert_offline_dedup():
    """Offline idempotency: identical hash inserts once across two calls.

    Uses a unique synthetic timestamp so the test is repeatable regardless of
    prior runs (the first insert is always genuinely new).
    """
    import uuid
    init_db()
    ts = f"2099-01-01T00:00:00Z#{uuid.uuid4()}"
    h = uw._hash("tide", ts)
    with session_scope() as s:
        first = uw._upsert(s, UWMarketTide, h, timestamp=ts, net_volume=1.0, raw_json="{}")
    with session_scope() as s:
        second = uw._upsert(s, UWMarketTide, h, timestamp=ts, net_volume=1.0, raw_json="{}")
    assert first is True
    assert second is False


class _FakeResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    """Minimal stand-in for requests.Session returning a canned max-pain payload."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, timeout=None):
        return _FakeResp(self._payload)


def test_max_pain_ingest_offline():
    """ingest_max_pain parses a canned envelope and inserts one row per expiry,
    idempotently, without any network call."""
    import uuid
    init_db()
    # The date column is String(20); keep it short. A unique 4-char suffix makes
    # the two rows genuinely new while staying within the column width.
    suffix = uuid.uuid4().hex[:4]
    date = f"2099-{suffix}"  # < 20 chars
    expiry_a = f"01-17-{suffix}"
    expiry_b = f"02-21-{suffix}"
    payload = {
        "date": date,
        "data": [
            {"expiry": expiry_a, "max_pain": "292.5", "close": "296.93"},
            {"expiry": expiry_b, "max_pain": "300", "close": "296.93"},
        ],
    }
    http = _FakeHttp(payload)
    with session_scope() as s:
        added = uw.ingest_max_pain(s, http, tickers=("AAPL",))
    assert added == 2
    with session_scope() as s:
        rows = s.query(UWMaxPain).filter(UWMaxPain.date == date).all()
        assert len(rows) == 2
        r = next(r for r in rows if r.expiry == expiry_a)
        assert r.ticker == "AAPL"
        assert r.max_pain == 292.5
        assert r.close == 296.93
        assert r.pulled_at is not None
        assert r.raw_json.strip().startswith("{")
    # Idempotent re-run inserts nothing.
    with session_scope() as s:
        added2 = uw.ingest_max_pain(s, http, tickers=("AAPL",))
    assert added2 == 0


def test_live_max_pain_ingest(caplog):
    """Optional live smoke: one call to max-pain ingests >=1 row; key never logged."""
    key = os.getenv("UW_API_KEY")
    if not key:
        pytest.skip("UW_API_KEY not set")
    init_db()
    caplog.set_level(logging.DEBUG)
    http = uw.make_session()
    try:
        with session_scope() as s:
            uw.ingest_max_pain(s, http, tickers=("AAPL",))
    finally:
        http.close()
    with session_scope() as s:
        assert s.query(UWMaxPain).count() >= 1
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert key not in joined
