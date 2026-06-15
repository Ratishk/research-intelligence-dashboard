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


def job_shift_alerts() -> None:
    from app.models import ShiftAlert
    with session_scope() as session:
        symbols = sorted(
            {row[0] for row in session.query(WatchlistItem.ticker_symbol).all()}
        )
        alerts = insights.detect_shifts(session, symbols)
        for a in alerts:
            session.add(ShiftAlert(
                symbol=a["symbol"],
                from_score=a["from"],
                to_score=a["to"],
                recent_total=a["recent_total"],
            ))
        if alerts:
            logger.info("Shift alerts: %s", [a["symbol"] for a in alerts])


def job_daily_findings() -> None:
    """Pre-compute the DeepResearch top-5 each morning so the page is ready."""
    from app.main import run_daily_findings
    with session_scope() as session:
        result = run_daily_findings(session, force=True)
        logger.info("Daily findings: %d ideas for %s",
                    len(result.get("findings", [])), result.get("date"))


def job_evaluate_theses() -> None:
    """Classify new signals against active theses (confirm/contradict)."""
    from app.processing.theses import evaluate_all
    with session_scope() as session:
        result = evaluate_all(session)
        if result:
            logger.info("Thesis evaluation: %s", result)


def job_fund_consensus_morning() -> None:
    """Warm the hedge-fund 13F consensus each morning. In the quarterly filing
    window it force-refreshes (new quarter's data); otherwise it just ensures the
    quarter-length cache is populated — 13F data is static between filings."""
    from app.processing import funds
    data = funds.get_consensus(force=funds.is_13f_filing_window())
    logger.info("Fund consensus warmed: %d names across %d funds (%s)",
                len(data.get("consensus", [])), data.get("funds_total", 0), data.get("quarter"))


def job_fund_consensus_filing() -> None:
    """During the ~3-week 13F filing window, force-refresh every few hours so new
    filings appear same-day. Cheap no-op the rest of the quarter."""
    from app.processing import funds
    if funds.is_13f_filing_window():
        data = funds.get_consensus(force=True)
        logger.info("Fund consensus (filing window) refreshed: %d names",
                    len(data.get("consensus", [])))


def build_scheduler() -> BackgroundScheduler:
    tier = config.tier()
    scheduler = BackgroundScheduler(timezone="UTC")
    # max_instances=1 + coalesce=True on EVERY job: never overlap a still-running
    # pass with the next fire, and collapse missed runs (e.g. after a sleep/restart)
    # into a single catch-up instead of a backlog burst.
    defaults = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(job_ingest, "interval", hours=1, id="ingest", **defaults)
    scheduler.add_job(job_classify, "interval", hours=1, id="classify", **defaults)
    scheduler.add_job(job_refresh_tickers, "interval", hours=6, id="tickers", **defaults)
    scheduler.add_job(
        job_discovery, "interval", days=tier.discovery_interval_days, id="discovery", **defaults
    )
    scheduler.add_job(job_shift_alerts, "interval", hours=4, id="shift_alerts", **defaults)
    # Daily DeepResearch findings at 11:00 UTC (~7am ET, pre-market).
    scheduler.add_job(job_daily_findings, "cron", hour=11, minute=0, id="daily_findings", **defaults)
    # Re-evaluate active theses against new signals every 6 hours.
    scheduler.add_job(job_evaluate_theses, "interval", hours=6, id="theses", **defaults)
    # Hedge-fund 13F consensus: warm every morning (~5am ET = 09:07 UTC), and
    # force-refresh every 3h during the quarterly filing window for same-day catch.
    scheduler.add_job(job_fund_consensus_morning, "cron", hour=9, minute=7, id="fund_consensus", **defaults)
    scheduler.add_job(job_fund_consensus_filing, "interval", hours=3, id="fund_consensus_filing", **defaults)
    return scheduler
