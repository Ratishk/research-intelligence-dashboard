"""APScheduler jobs wiring the pipeline together.

Cadences are tier-aware where it matters (discovery interval). Each job opens
its own session scope so a failure is isolated and rolled back.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import config
from app.database import session_scope
from app.discovery import engine as discovery_engine
from app.ingestion.runner import run_all_ingestion
from app.processing import digest as digest_mod
from app.processing import insights
from app.processing.classifier import run_classification
from app.processing.tickers import refresh_watchlist_tickers
from app.models import WatchlistItem

logger = logging.getLogger(__name__)


def job_ingest() -> None:
    run_all_ingestion()


def job_classify() -> None:
    with session_scope() as session:
        run_classification(session)


def job_refresh_tickers() -> None:
    with session_scope() as session:
        refresh_watchlist_tickers(session)


def job_discovery() -> None:
    with session_scope() as session:
        discovery_engine.run_discovery(
            session, max_active_industries=config.tier().max_active_industries
        )


def job_digest() -> None:
    with session_scope() as session:
        digest_mod.send_daily_digest(session)


def job_shift_alerts() -> None:
    with session_scope() as session:
        symbols = sorted(
            {row[0] for row in session.query(WatchlistItem.ticker_symbol).all()}
        )
        alerts = insights.detect_shifts(session, symbols)
        if alerts:
            html = "<h3>Narrative shift alerts</h3><ul>" + "".join(
                f"<li>{a['symbol']}: {a['from']} → {a['to']} "
                f"({a['recent_total']} recent signals)</li>"
                for a in alerts
            ) + "</ul>"
            digest_mod.send_email(html, "⚠️ Signal shift alert")


def build_scheduler() -> BackgroundScheduler:
    tier = config.tier()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(job_ingest, "interval", hours=1, id="ingest", max_instances=1)
    scheduler.add_job(job_classify, "interval", hours=1, id="classify", max_instances=1)
    scheduler.add_job(job_refresh_tickers, "interval", hours=6, id="tickers")
    scheduler.add_job(
        job_discovery, "interval", days=tier.discovery_interval_days, id="discovery"
    )
    scheduler.add_job(job_digest, "cron", hour=7, minute=0, id="digest")
    scheduler.add_job(job_shift_alerts, "interval", hours=4, id="shift_alerts")
    return scheduler
