"""FDA drug-recall / enforcement ingestion via openFDA (no auth).

Drug recalls and enforcement actions are negative catalysts for biotech/pharma
names. We pull each firm's most recent enforcement records from the free
openFDA ``drug/enforcement.json`` endpoint and emit a *bearish* Signal per
recall, tagged with the firm's ticker. Class I recalls are the most serious.

Sources are configured with the firm name and ticker encoded in
``source.handle`` as "Firm Name|TICKER" (the seeder clobbers tags with the
industry name, so we can't rely on source.tags for the ticker).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from app.ingestion.common import content_hash, upsert_item
from app.models import Direction, Signal, Source

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.fda.gov/drug/enforcement.json"
_HEADERS = {"User-Agent": "research-dashboard/0.1", "Accept": "application/json"}
_TIMEOUT = 25
_LIMIT = 8


def _fetch(firm: str) -> list[dict]:
    try:
        resp = requests.get(
            _ENDPOINT,
            params={
                "search": f'recalling_firm:"{firm}"',
                "sort": "report_date:desc",
                "limit": _LIMIT,
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        # openFDA returns 404 with an error body when a query has no matches.
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("results", []) or []
    except Exception:
        logger.info("openFDA enforcement fetch failed for %s", firm)
        return []


def ingest_source(session, source: Source) -> int:
    # handle encodes "Firm Name|TICKER" (the seeder clobbers tags with the
    # industry name, so we can't rely on source.tags for the ticker).
    raw = (source.handle or source.name or "").strip()
    if "|" in raw:
        firm, _, ticker = raw.partition("|")
        firm, ticker = firm.strip(), ticker.strip()
    else:
        firm, ticker = raw, (source.tags or "").strip()
    if not firm:
        return 0

    recalls = _fetch(firm)
    added = 0
    for recall in recalls:
        product = (recall.get("product_description") or "").strip()
        reason = (recall.get("reason_for_recall") or "").strip()
        classification = (recall.get("classification") or "").strip()
        report_date = (recall.get("report_date") or "").strip()
        recall_number = (recall.get("recall_number") or "").strip()
        status = (recall.get("status") or "").strip()

        published = None
        if report_date:
            try:
                published = datetime.strptime(report_date, "%Y%m%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                published = None

        # Stable dedupe URL: prefer the recall_number, else a hash of the
        # firm + product + date so re-runs collapse to the same item.
        if recall_number:
            url = f"https://api.fda.gov/drug/enforcement.json?search=recall_number:{recall_number}"
        else:
            digest = content_hash("", f"{firm}|{product}|{report_date}")
            url = f"https://api.fda.gov/drug/enforcement.json?search=recall:{digest}"

        is_class_i = classification.lower() == "class i"
        summary = (
            f"{ticker} FDA recall ({classification}): "
            f"{product[:60]} — {reason[:60]}"
        )

        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=url,
            content=(
                f"{product}\nFirm: {firm}\nClassification: {classification}\n"
                f"Reason: {reason}\nStatus: {status}\n"
                f"Recall #: {recall_number}\nReport date: {report_date}"
            ),
            author=firm,
            media_type="filing",
            published_at=published,
        )
        if item is None:
            continue

        item.processed = True
        signal = Signal(
            item=item,
            signal_type="bottleneck",
            confidence=0.7 if is_class_i else 0.6,
            direction=Direction.bearish.value,
            summary=summary,
            speculative=True,
            entities_json=json.dumps({"tickers": [ticker] if ticker else []}),
        )
        session.add(signal)
        added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(
            Source.type == "fdarecall",
            Source.active.is_(True),
        )
        .all()
    )
    total = 0
    for source in sources:
        total += ingest_source(session, source)
    return total
