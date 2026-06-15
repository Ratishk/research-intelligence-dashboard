"""openFDA biotech catalysts — drug approvals + recalls (keyless).

Two free, no-auth openFDA endpoints, one bearish / one bullish:

* ``drug/drugsfda.json``  — approval history. We surface recently *approved*
  (submission_status == "AP") products as a **bullish** ``fda_approval`` signal.
* ``drug/enforcement.json`` — recalls/enforcement. A recent recall is a
  **bearish** ``fda_recall`` signal.

Name -> ticker matching is fuzzy and we stay conservative: sources carry an
explicit openFDA sponsor token so we query by that token rather than guessing
from the corporate name. Handle is encoded ``"Firm Name|TICKER|FDA_TOKEN"``
(FDA_TOKEN optional; falls back to the firm's first word). Everything degrades
gracefully — any failure returns 0 and never raises out of ``ingest_all``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from app.ingestion.common import content_hash, upsert_item
from app.models import Direction, Signal, Source, SourceType

logger = logging.getLogger(__name__)

_DRUGSFDA = "https://api.fda.gov/drug/drugsfda.json"
_ENFORCEMENT = "https://api.fda.gov/drug/enforcement.json"
_HEADERS = {"User-Agent": "research-dashboard/0.1", "Accept": "application/json"}
_TIMEOUT = 6
_LIMIT = 8
# Only surface approvals/recalls newer than this many days (recent catalysts).
_APPROVAL_WINDOW_DAYS = 365
_RECALL_WINDOW_DAYS = 365
MAX_PER_SOURCE = 5


def _parse_handle(source: Source) -> tuple[str, str, str]:
    """Return (firm, ticker, fda_token) from a "Firm|TICKER|TOKEN" handle."""
    raw = (source.handle or source.name or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    firm = parts[0] if parts else ""
    ticker = parts[1] if len(parts) > 1 else (source.tags or "").strip()
    token = parts[2] if len(parts) > 2 and parts[2] else (firm.split()[0] if firm else "")
    return firm, ticker, token.upper()


def _yyyymmdd(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch(url: str, params: dict) -> list[dict]:
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        # openFDA returns 404 with an error body when a query has no matches.
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("results", []) or []
    except Exception:
        logger.info("openFDA fetch failed: %s %s", url, params.get("search"))
        return []


def _ingest_approvals(session, source, firm, ticker, token) -> int:
    if not token:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=_APPROVAL_WINDOW_DAYS)
    results = _fetch(
        _DRUGSFDA,
        {
            "search": f"sponsor_name:{token}",
            "sort": "submissions.submission_status_date:desc",
            "limit": _LIMIT,
        },
    )
    added = 0
    for app in results:
        sponsor = (app.get("sponsor_name") or "").strip()
        appno = (app.get("application_number") or "").strip()
        brand = ((app.get("openfda", {}) or {}).get("brand_name") or [None])[0]
        if not brand:
            prods = app.get("products") or []
            brand = prods[0].get("brand_name") if prods else None
        # Pick the most recent approval (AP) submission within the window.
        best = None
        for sub in (app.get("submissions") or []):
            if sub.get("submission_status") != "AP":
                continue
            dt = _yyyymmdd(sub.get("submission_status_date"))
            if dt is None or dt < cutoff:
                continue
            if best is None or dt > best[0]:
                best = (dt, sub)
        if best is None:
            continue
        published, sub = best
        label = brand or appno or sponsor
        summary = f"{ticker} FDA approval: {label} ({appno})"
        url = f"https://api.fda.gov/drug/drugsfda.json?search=application_number:{appno}"

        item = upsert_item(
            session, source=source, title=summary[:300], url=url,
            content=(
                f"FDA approval — {label}\nSponsor: {sponsor}\n"
                f"Application: {appno}\nSubmission: {sub.get('submission_type')} "
                f"{sub.get('submission_number')}\nApproved: {sub.get('submission_status_date')}"
            ),
            author=sponsor or firm, media_type="filing", published_at=published,
        )
        if item is None:
            continue
        item.processed = True
        session.add(Signal(
            item_id=item.id, signal_type="fda_approval", confidence=0.7,
            direction=Direction.bullish.value, summary=summary,
            entities_json=json.dumps({"tickers": [ticker] if ticker else []}),
            speculative=True,
        ))
        added += 1
        if added >= MAX_PER_SOURCE:
            break
    return added


def _ingest_recalls(session, source, firm, ticker) -> int:
    if not firm:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RECALL_WINDOW_DAYS)
    results = _fetch(
        _ENFORCEMENT,
        {
            "search": f'recalling_firm:"{firm}"',
            "sort": "report_date:desc",
            "limit": _LIMIT,
        },
    )
    added = 0
    for recall in results:
        published = _yyyymmdd(recall.get("report_date"))
        if published is None or published < cutoff:
            continue
        product = (recall.get("product_description") or "").strip()
        reason = (recall.get("reason_for_recall") or "").strip()
        classification = (recall.get("classification") or "").strip()
        recall_number = (recall.get("recall_number") or "").strip()
        is_class_i = classification.lower() == "class i"

        if recall_number:
            url = f"https://api.fda.gov/drug/enforcement.json?search=recall_number:{recall_number}"
        else:
            digest = content_hash("", f"{firm}|{product}|{recall.get('report_date')}")
            url = f"https://api.fda.gov/drug/enforcement.json?search=recall:{digest}"
        summary = f"{ticker} FDA recall ({classification}): {product[:60]} — {reason[:60]}"

        item = upsert_item(
            session, source=source, title=summary[:300], url=url,
            content=(
                f"{product}\nFirm: {firm}\nClassification: {classification}\n"
                f"Reason: {reason}\nRecall #: {recall_number}"
            ),
            author=firm, media_type="filing", published_at=published,
        )
        if item is None:
            continue
        item.processed = True
        session.add(Signal(
            item_id=item.id, signal_type="fda_recall",
            confidence=0.7 if is_class_i else 0.6,
            direction=Direction.bearish.value, summary=summary,
            entities_json=json.dumps({"tickers": [ticker] if ticker else []}),
            speculative=True,
        ))
        added += 1
        if added >= MAX_PER_SOURCE:
            break
    return added


def ingest_source(session, source: Source) -> int:
    firm, ticker, token = _parse_handle(source)
    if not firm:
        return 0
    total = 0
    try:
        total += _ingest_approvals(session, source, firm, ticker, token)
    except Exception:
        logger.exception("openFDA approvals error for %s", firm)
    try:
        total += _ingest_recalls(session, source, firm, ticker)
    except Exception:
        logger.exception("openFDA recalls error for %s", firm)
    return total


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.openfda.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("openFDA ingest error for %s", source.name)
    return total
