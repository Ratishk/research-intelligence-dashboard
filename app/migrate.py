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
