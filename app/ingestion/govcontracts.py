"""Federal contract-award ingestion via USASpending.gov (no auth, no key).

New federal contract awards are forward revenue visibility for defense,
government-IT, and healthcare names. We query USASpending's award-search API
for each tracked recipient, sorted by award amount, and emit a *bullish*
``funding`` Signal per large award (new revenue on the books).

Sources are configured with the company name in ``source.handle`` (e.g.
"Lockheed Martin") and the ticker in ``source.tags`` (e.g. "LMT").
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.models import Direction, Signal, Source

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_HEADERS = {
    "User-Agent": "research-dashboard/0.1",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
_TIMEOUT = 25
_LIMIT = 8
MIN_AWARD = 1_000_000.0  # skip sub-$1M awards to cut noise

# Contract award type codes (procurement contracts) and their labels.
_AWARD_TYPE_CODES = ["A", "B", "C", "D"]
_TYPE_LABELS = {
    "A": "BPA Call",
    "B": "Purchase Order",
    "C": "Delivery Order",
    "D": "Definitive Contract",
}

# Recent, wide window (system clock may read 2026); covers current awards.
_TIME_PERIOD = [{"start_date": "2024-01-01", "end_date": "2026-12-31"}]
_FIELDS = [
    "Award Amount",
    "Recipient Name",
    "Award Type",
    "Description",
    "Start Date",
    "Award ID",
    "Awarding Agency",
]


def _fetch(company: str) -> list[dict]:
    body = {
        "filters": {
            "recipient_search_text": [company],
            "award_type_codes": _AWARD_TYPE_CODES,
            "time_period": _TIME_PERIOD,
        },
        "fields": _FIELDS,
        "sort": "Award Amount",
        "order": "desc",
        "limit": _LIMIT,
    }
    try:
        resp = requests.post(_ENDPOINT, json=body, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("results", []) or []
    except Exception:
        logger.info("USASpending fetch failed for %s", company)
        return []


def _amount(award: dict) -> float | None:
    try:
        return float(award.get("Award Amount"))
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def ingest_source(session, source: Source) -> int:
    # handle encodes "Company Name|TICKER" (the seeder clobbers tags with the
    # industry name, so we can't rely on source.tags for the ticker).
    raw = (source.handle or source.name or "").strip()
    if "|" in raw:
        company, _, ticker = raw.partition("|")
        company, ticker = company.strip(), ticker.strip()
    else:
        company, ticker = raw, (source.tags or "").strip()
    if not company:
        return 0

    added = 0
    for award in _fetch(company):
        amount = _amount(award)
        if amount is None or amount < MIN_AWARD:
            continue

        award_id = (award.get("Award ID") or "").strip()
        if not award_id:
            continue

        agency = (award.get("Awarding Agency") or "Unknown agency").strip()
        award_type = (award.get("Award Type") or "").strip()
        type_label = _TYPE_LABELS.get(award_type, award_type or "Contract")
        recipient = (award.get("Recipient Name") or company).strip()
        description = (award.get("Description") or "").strip()

        label = ticker or company
        summary = (
            f"{label}: ${amount:,.0f} federal contract — {agency} ({type_label})"
        )
        url = f"https://www.usaspending.gov/award/{award_id}"

        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=url,
            content=(
                f"{recipient} — ${amount:,.0f} federal contract\n"
                f"Awarding agency: {agency}\nType: {type_label}\n"
                f"Award ID: {award_id}\n{description}"
            ).strip(),
            author=recipient,
            media_type="filing",
            published_at=_parse_date(award.get("Start Date")),
        )
        if item is None:
            continue

        item.processed = True
        signal = Signal(
            item=item,
            signal_type="funding",
            confidence=0.6,
            direction=Direction.bullish.value,
            summary=summary,
            entities_json=json.dumps({"tickers": [ticker] if ticker else []}),
        )
        session.add(signal)
        added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(
            Source.type == "govcontract",
            Source.active.is_(True),
        )
        .all()
    )
    total = 0
    for source in sources:
        total += ingest_source(session, source)
    return total
