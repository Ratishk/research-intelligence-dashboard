"""Unusual Whales (UW) daily REST ingestion.

Pulls UW's daily options-flow / smart-money feeds and persists them to research.db
in dedicated ``uw_*`` tables. This REPLACES the yfinance volume-ratio hack as the
options-flow source.

Tier / limits (Basic tier): REST only (NO websockets), ~120 req/min, ~60K/day,
~90-day lookback. We honour 429/5xx with exponential backoff that respects
``Retry-After`` and keep the per-run call count tiny.

Auth: ``Authorization: Bearer <UW_API_KEY>`` (read ONLY via os.getenv; never
hardcoded/printed/logged). The UW skill.md also asks for ``UW-CLIENT-API-ID``.

Endpoints used (all in the UW skill.md whitelist + verified against the OpenAPI
at https://api.unusualwhales.com/api/openapi):
    /api/market/market-tide          -> uw_market_tide
    /api/option-trades/flow-alerts    -> uw_flow_alerts
    /api/darkpool/recent              -> uw_darkpool
    /api/congress/recent-trades       -> uw_congress
    /api/insider/transactions         -> uw_insider
    /api/stock/{ticker}/greeks        -> uw_greeks  (small watchlist set)

Every persisted row carries a content-hash natural key (``uw_hash``, UNIQUE → an
idempotent upsert), a tz-aware UTC ``pulled_at`` and the point-in-time ``raw_json``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    UWCongress,
    UWDarkpool,
    UWFlowAlert,
    UWGreeks,
    UWInsider,
    UWMarketTide,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.unusualwhales.com"
_TIMEOUT = 20
_MAX_RETRIES = 4
_BACKOFF_BASE = 1.5  # seconds; doubled each retry, capped
_BACKOFF_CAP = 30.0

# Endpoint whitelist — every path we may call MUST be listed here (mirrors the
# UW skill.md whitelist). Asserted before each request so we never call an
# endpoint outside the verified set.
WHITELIST = frozenset({
    "/api/market/market-tide",
    "/api/option-trades/flow-alerts",
    "/api/darkpool/recent",
    "/api/congress/recent-trades",
    "/api/insider/transactions",
    "/api/stock/{ticker}/greeks",
})

# Tiny default watchlist for the greeks pull (keeps per-run calls small). The
# real watchlist can be threaded in later; this is intentionally minimal.
_GREEKS_WATCHLIST = ("AAPL", "NVDA", "SPY")


class UWConfigError(RuntimeError):
    """Raised when UW_API_KEY is missing from the environment."""


def _api_key() -> str:
    key = os.getenv("UW_API_KEY")
    if not key:
        raise UWConfigError("UW_API_KEY is not set in the environment")
    return key


def make_session() -> requests.Session:
    """A shared requests.Session with the Bearer auth + UW client headers.

    The key is read via os.getenv and set only on the in-memory header dict; it
    is never logged. ``repr`` of a Session does not expose headers.
    """
    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {_api_key()}",
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
        "User-Agent": "research-dashboard ratishsaaik@gmail.com",
    })
    return sess


def _is_whitelisted(path: str) -> bool:
    """A concrete path matches the whitelist if it equals a whitelist entry or
    matches a ``{ticker}`` template (only the templated segment may differ)."""
    if path in WHITELIST:
        return True
    parts = path.strip("/").split("/")
    for tmpl in WHITELIST:
        tparts = tmpl.strip("/").split("/")
        if len(tparts) != len(parts):
            continue
        if all(t == p or t.startswith("{") for t, p in zip(tparts, parts)):
            return True
    return False


def _get(session: requests.Session, path: str, params: dict | None = None) -> dict | None:
    """GET a whitelisted UW endpoint with 429/5xx exponential backoff.

    Returns the parsed JSON dict, or None on persistent failure. Never logs the
    API key (only the path, status, and our own params are logged).
    """
    if not _is_whitelisted(path):
        raise ValueError(f"refusing to call non-whitelisted UW endpoint: {path}")

    url = _BASE_URL + path
    delay = _BACKOFF_BASE
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=_TIMEOUT)
        except requests.Timeout:
            logger.warning("UW timeout on %s (attempt %d/%d)", path, attempt, _MAX_RETRIES)
            if attempt == _MAX_RETRIES:
                return None
            time.sleep(min(delay, _BACKOFF_CAP))
            delay *= 2
            continue
        except requests.RequestException as exc:
            # Never include exc args verbatim if they could echo the URL/key; the
            # path is already known and safe to log. Log the exception type only.
            logger.warning("UW request error on %s: %s", path, type(exc).__name__)
            if attempt == _MAX_RETRIES:
                return None
            time.sleep(min(delay, _BACKOFF_CAP))
            delay *= 2
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                logger.warning("UW non-JSON 200 response on %s", path)
                return None

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = delay
            else:
                wait = delay
            logger.warning(
                "UW %s on %s — backing off %.1fs (attempt %d/%d)",
                resp.status_code, path, min(wait, _BACKOFF_CAP), attempt, _MAX_RETRIES,
            )
            if attempt == _MAX_RETRIES:
                return None
            time.sleep(min(wait, _BACKOFF_CAP))
            delay *= 2
            continue

        # 4xx other than 429 — not retryable. Log status only (no body/key).
        logger.warning("UW %s on %s — not retrying", resp.status_code, path)
        return None
    return None


def _rows(payload: dict | None) -> list[dict]:
    """Extract the list of row dicts from a UW ``{"data": [...]}`` envelope."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _hash(*parts: object) -> str:
    basis = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()


def _to_float(val: object) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_dt(val: object) -> datetime | None:
    """Parse a UW ISO-8601 timestamp into a tz-aware UTC datetime."""
    if not val:
        return None
    raw = str(val).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _upsert(session: Session, model, uw_hash: str, **fields) -> bool:
    """Insert a uw_* row keyed by uw_hash if new. Returns True if inserted.

    Mirrors common.upsert_item: check first, then flush-then-catch IntegrityError
    so a concurrent duplicate can't roll back the whole batch.
    """
    existing = session.scalar(select(model.id).where(model.uw_hash == uw_hash))
    if existing:
        return False
    row = model(uw_hash=uw_hash, pulled_at=datetime.now(timezone.utc), **fields)
    session.add(row)
    try:
        session.flush([row])
    except IntegrityError:
        session.rollback()
        return False
    return True


# --- per-endpoint ingesters -------------------------------------------------

def ingest_market_tide(session: Session, http: requests.Session) -> int:
    rows = _rows(_get(http, "/api/market/market-tide"))
    added = 0
    for r in rows:
        ts = r.get("timestamp")
        if _upsert(
            session, UWMarketTide, _hash("tide", ts),
            timestamp=str(ts or ""),
            net_call_premium=_to_float(r.get("net_call_premium")),
            net_put_premium=_to_float(r.get("net_put_premium")),
            net_volume=_to_float(r.get("net_volume")),
            raw_json=json.dumps(r),
        ):
            added += 1
    return added


def ingest_flow_alerts(session: Session, http: requests.Session) -> int:
    rows = _rows(_get(http, "/api/option-trades/flow-alerts"))
    added = 0
    for r in rows:
        key = _hash("flow", r.get("created_at"), r.get("option_chain"), r.get("ticker"))
        if _upsert(
            session, UWFlowAlert, key,
            ticker=(r.get("ticker") or "")[:20],
            option_chain=(r.get("option_chain") or "")[:60],
            type=(r.get("type") or "")[:8],
            total_premium=_to_float(r.get("total_premium")),
            total_size=_to_float(r.get("total_size")),
            created_at=_parse_dt(r.get("created_at")),
            raw_json=json.dumps(r),
        ):
            added += 1
    return added


def ingest_darkpool(session: Session, http: requests.Session) -> int:
    rows = _rows(_get(http, "/api/darkpool/recent"))
    added = 0
    for r in rows:
        tracking = r.get("tracking_id")
        key = _hash("dp", tracking) if tracking else _hash(
            "dp", r.get("executed_at"), r.get("ticker"), r.get("price"), r.get("size")
        )
        if _upsert(
            session, UWDarkpool, key,
            ticker=(r.get("ticker") or "")[:20],
            tracking_id=str(tracking or "")[:40],
            price=_to_float(r.get("price")),
            size=_to_float(r.get("size")),
            premium=_to_float(r.get("premium")),
            executed_at=_parse_dt(r.get("executed_at")),
            raw_json=json.dumps(r),
        ):
            added += 1
    return added


def ingest_congress(session: Session, http: requests.Session) -> int:
    rows = _rows(_get(http, "/api/congress/recent-trades", params={"limit": 100}))
    added = 0
    for r in rows:
        key = _hash(
            "cong", r.get("politician_id"), r.get("ticker"),
            r.get("transaction_date"), r.get("txn_type"), r.get("filed_at_date"),
        )
        amounts = r.get("amounts")
        if _upsert(
            session, UWCongress, key,
            ticker=(r.get("ticker") or "")[:20],
            name=(r.get("name") or "")[:200],
            txn_type=(r.get("txn_type") or "")[:40],
            amounts=(str(amounts) if amounts is not None else "")[:80],
            transaction_date=str(r.get("transaction_date") or "")[:20],
            filed_at_date=str(r.get("filed_at_date") or "")[:20],
            raw_json=json.dumps(r),
        ):
            added += 1
    return added


def ingest_insider(session: Session, http: requests.Session) -> int:
    rows = _rows(_get(http, "/api/insider/transactions", params={"limit": 100}))
    added = 0
    for r in rows:
        key = _hash(
            "ins", r.get("ticker"), r.get("owner_name"),
            r.get("transaction_date"), r.get("transaction_code"), r.get("amount"),
        )
        if _upsert(
            session, UWInsider, key,
            ticker=(r.get("ticker") or "")[:20],
            owner_name=(r.get("owner_name") or "")[:200],
            transaction_code=(r.get("transaction_code") or "")[:8],
            amount=_to_float(r.get("amount")),
            price=_to_float(r.get("price")),
            transaction_date=str(r.get("transaction_date") or "")[:20],
            raw_json=json.dumps(r),
        ):
            added += 1
    return added


def ingest_greeks(session: Session, http: requests.Session,
                  tickers: tuple[str, ...] = _GREEKS_WATCHLIST) -> int:
    added = 0
    for ticker in tickers:
        sym = ticker.strip().upper()
        if not sym:
            continue
        rows = _rows(_get(http, f"/api/stock/{sym}/greeks"))
        for r in rows:
            key = _hash("gex", sym, r.get("date"), r.get("expiry"), r.get("strike"))
            if _upsert(
                session, UWGreeks, key,
                ticker=sym,
                date=str(r.get("date") or "")[:20],
                expiry=str(r.get("expiry") or "")[:20],
                strike=str(r.get("strike") or "")[:20],
                call_gamma=_to_float(r.get("call_gamma")),
                put_gamma=_to_float(r.get("put_gamma")),
                raw_json=json.dumps(r),
            ):
                added += 1
    return added


def ingest_all(session: Session) -> int:
    """Run every UW ingester once. Returns total new rows across all uw_* tables.

    Each endpoint is isolated so one failure can't abort the rest. Returns 0 if
    the key is missing (logged, not raised) so the pipeline degrades gracefully.
    """
    try:
        http = make_session()
    except UWConfigError:
        logger.warning("UW ingest skipped: UW_API_KEY not configured")
        return 0

    total = 0
    ingesters = (
        ("market_tide", ingest_market_tide),
        ("flow_alerts", ingest_flow_alerts),
        ("darkpool", ingest_darkpool),
        ("congress", ingest_congress),
        ("insider", ingest_insider),
        ("greeks", ingest_greeks),
    )
    for name, fn in ingesters:
        try:
            n = fn(session, http)
            total += n
            logger.info("UW %s: +%d rows", name, n)
        except (requests.RequestException, IntegrityError, ValueError) as exc:
            logger.warning("UW %s ingest error: %s", name, type(exc).__name__)
    http.close()
    return total
