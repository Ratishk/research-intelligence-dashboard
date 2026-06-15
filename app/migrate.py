"""Idempotent SQLite column migrations.

Called from init_db() on every startup. Each ALTER TABLE is wrapped in a
try/except so it is safe to run against a database that already has the column.
SQLite only supports adding nullable or defaulted columns — all additions here
satisfy that constraint.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

_MIGRATIONS = [
    # Ticker enrichment — Webull-equivalent fundamentals
    "ALTER TABLE tickers ADD COLUMN short_interest_pct REAL",
    "ALTER TABLE tickers ADD COLUMN short_ratio REAL",
    "ALTER TABLE tickers ADD COLUMN analyst_rating TEXT",
    "ALTER TABLE tickers ADD COLUMN analyst_target REAL",
    "ALTER TABLE tickers ADD COLUMN analyst_count INTEGER",
    # StockTwits social sentiment
    "ALTER TABLE tickers ADD COLUMN st_bull INTEGER DEFAULT 0",
    "ALTER TABLE tickers ADD COLUMN st_bear INTEGER DEFAULT 0",
    "ALTER TABLE tickers ADD COLUMN st_updated_at TEXT",
    # Consensus engine — blended crowd-belief score (-1..+1)
    "ALTER TABLE tickers ADD COLUMN consensus_score REAL",
    "ALTER TABLE tickers ADD COLUMN consensus_label TEXT",
    "ALTER TABLE tickers ADD COLUMN consensus_updated_at TEXT",
    # Volume / momentum (Yahoo v8 chart)
    "ALTER TABLE tickers ADD COLUMN rvol REAL",
    "ALTER TABLE tickers ADD COLUMN latest_volume REAL",
    "ALTER TABLE tickers ADD COLUMN change_pct REAL",
    "ALTER TABLE tickers ADD COLUMN change_5d_pct REAL",
    # Unusual Whales daily REST tables. Base.metadata.create_all() already makes
    # these for a fresh DB, but an EXISTING research.db won't pick up new ORM
    # models unless explicitly created here. CREATE TABLE IF NOT EXISTS is a no-op
    # when the table already exists, so this stays idempotent.
    (
        "CREATE TABLE IF NOT EXISTS uw_flow_alerts ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, ticker VARCHAR(20), "
        "option_chain VARCHAR(60), type VARCHAR(8), total_premium REAL, "
        "total_size REAL, created_at DATETIME, raw_json TEXT, pulled_at DATETIME)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS uw_darkpool ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, ticker VARCHAR(20), "
        "tracking_id VARCHAR(40), price REAL, size REAL, premium REAL, "
        "executed_at DATETIME, raw_json TEXT, pulled_at DATETIME)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS uw_congress ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, ticker VARCHAR(20), "
        "name VARCHAR(200), txn_type VARCHAR(40), amounts VARCHAR(80), "
        "transaction_date VARCHAR(20), filed_at_date VARCHAR(20), "
        "raw_json TEXT, pulled_at DATETIME)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS uw_insider ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, ticker VARCHAR(20), "
        "owner_name VARCHAR(200), transaction_code VARCHAR(8), amount REAL, "
        "price REAL, transaction_date VARCHAR(20), raw_json TEXT, pulled_at DATETIME)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS uw_market_tide ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, timestamp VARCHAR(40), "
        "net_call_premium REAL, net_put_premium REAL, net_volume REAL, "
        "raw_json TEXT, pulled_at DATETIME)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS uw_greeks ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, ticker VARCHAR(20), "
        "date VARCHAR(20), expiry VARCHAR(20), strike VARCHAR(20), "
        "call_gamma REAL, put_gamma REAL, raw_json TEXT, pulled_at DATETIME)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS uw_max_pain ("
        "id INTEGER PRIMARY KEY, uw_hash VARCHAR(64) UNIQUE, ticker VARCHAR(20), "
        "date VARCHAR(20), expiry VARCHAR(20), max_pain REAL, close REAL, "
        "raw_json TEXT, pulled_at DATETIME)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_uw_flow_alerts_uw_hash ON uw_flow_alerts (uw_hash)",
    "CREATE INDEX IF NOT EXISTS ix_uw_darkpool_uw_hash ON uw_darkpool (uw_hash)",
    "CREATE INDEX IF NOT EXISTS ix_uw_congress_uw_hash ON uw_congress (uw_hash)",
    "CREATE INDEX IF NOT EXISTS ix_uw_insider_uw_hash ON uw_insider (uw_hash)",
    "CREATE INDEX IF NOT EXISTS ix_uw_market_tide_uw_hash ON uw_market_tide (uw_hash)",
    "CREATE INDEX IF NOT EXISTS ix_uw_greeks_uw_hash ON uw_greeks (uw_hash)",
    "CREATE INDEX IF NOT EXISTS ix_uw_max_pain_uw_hash ON uw_max_pain (uw_hash)",
    # Drop the orphaned FTS5 table replaced by sig_fts_v2 (see processing/rag.py).
    # DROP TABLE on an fts5 virtual table also removes its shadow tables
    # (signal_fts_data/_idx/_content/_docsize/_config). IF EXISTS keeps it idempotent.
    "DROP TABLE IF EXISTS signal_fts",
]


def run_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        for stmt in _MIGRATIONS:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    pass  # already applied
                else:
                    logger.warning("Migration skipped (%s): %s", stmt, e)
