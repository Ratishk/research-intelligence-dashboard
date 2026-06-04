"""Single entry point: start the scheduler + serve the dashboard.

    python run.py            # starts background scheduler + FastAPI on :8000
    python run.py --no-sched # API only (useful for local UI work)
"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from app.database import init_db
from app.main import app
from app.scheduler import build_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("run")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-sched", action="store_true", help="skip background scheduler")
    args = parser.parse_args()

    init_db()

    if not args.no_sched:
        scheduler = build_scheduler()
        scheduler.start()
        logger.info("Scheduler started (jobs: %s)", [j.id for j in scheduler.get_jobs()])

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
