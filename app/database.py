"""SQLAlchemy engine + session factory (SQLite by default)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import config

# check_same_thread=False so the APScheduler background jobs and the FastAPI
# request threads can share the SQLite connection pool.
_connect_args = (
    {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
)
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args, future=True)

if config.DATABASE_URL.startswith("sqlite"):
    # The parallel ingestion runner, the per-symbol ticker-refresh pool, and the
    # disk cache now open MANY writer connections to the one research.db file.
    # SQLite allows only one writer at a time; with the defaults an overlapping
    # COMMIT raises "database is locked" immediately, which the per-job handlers
    # swallow -> silent data loss. WAL lets readers run alongside one writer, and
    # busy_timeout makes a writer WAIT for the lock instead of erroring out.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - DB setup
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create all tables, then apply column migrations."""
    from app import models  # noqa: F401
    from app import migrate

    Base.metadata.create_all(bind=engine)
    migrate.run_migrations(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
