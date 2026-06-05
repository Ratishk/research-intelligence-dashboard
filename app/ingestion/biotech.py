"""Biotech clinical-trial catalyst ingestion via ClinicalTrials.gov v2 (no auth).

Clinical-trial status changes (especially Phase 2/3 readouts) are major biotech
price catalysts. We pull each sponsor's most recently updated studies from the
free ClinicalTrials.gov v2 API and, for Phase 2/3 trials, emit a *neutral*
catalyst-to-watch Signal tagged with the sponsor's ticker.

Sources are configured with the sponsor name in ``source.handle`` (e.g.
"Moderna") and the ticker in ``source.tags`` (e.g. "MRNA").
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.models import Direction, Item, Signal, Source

logger = logging.getLogger(__name__)

_ENDPOINT = "https://clinicaltrials.gov/api/v2/studies"
_HEADERS = {"User-Agent": "research-dashboard/0.1", "Accept": "application/json"}
_TIMEOUT = 25
_PAGE_SIZE = 10
MAX_STUDIES_PER_SOURCE = 5

# Phases that move stock prices on readout.
_CATALYST_PHASES = {"PHASE2", "PHASE3"}


def _phase_label(phases: list[str]) -> str | None:
    """Return a human label if any catalyst phase is present, else None."""
    if not phases:
        return None
    if not any(p in _CATALYST_PHASES for p in phases):
        return None
    pretty = {"PHASE1": "Phase 1", "PHASE2": "Phase 2", "PHASE3": "Phase 3", "PHASE4": "Phase 4"}
    return "/".join(pretty.get(p, p.title()) for p in phases)


def _fetch(handle: str) -> list[dict]:
    try:
        resp = requests.get(
            _ENDPOINT,
            params={
                "query.spons": handle,
                "pageSize": _PAGE_SIZE,
                "sort": "LastUpdatePostDate:desc",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("studies", []) or []
    except Exception:
        logger.info("ClinicalTrials fetch failed for %s", handle)
        return []


def ingest_source(session, source: Source) -> int:
    # handle encodes "Sponsor Name|TICKER" (the seeder clobbers tags with the
    # industry name, so we can't rely on source.tags for the ticker).
    raw = (source.handle or source.name or "").strip()
    if "|" in raw:
        company, _, ticker = raw.partition("|")
        company, ticker = company.strip(), ticker.strip()
    else:
        company, ticker = raw, (source.tags or "").strip()
    if not company:
        return 0

    studies = _fetch(company)
    added = 0
    for study in studies:
        proto = study.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        status_mod = proto.get("statusModule", {})
        design = proto.get("designModule", {})

        nct_id = ident.get("nctId")
        if not nct_id:
            continue

        phase = _phase_label(design.get("phases", []))
        if phase is None:
            continue

        brief_title = ident.get("briefTitle", "") or ""
        overall_status = status_mod.get("overallStatus", "") or ""
        last_update = (status_mod.get("lastUpdatePostDateStruct") or {}).get("date")

        published = None
        if last_update:
            for fmt in ("%Y-%m-%d", "%Y-%m"):
                try:
                    published = datetime.strptime(last_update, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

        url = f"https://clinicaltrials.gov/study/{nct_id}"
        summary = f"{company} {phase} trial: {brief_title[:80]} — {overall_status}"

        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=url,
            content=(
                f"{brief_title}\nSponsor: {company}\nPhase: {phase}\n"
                f"Status: {overall_status}\nLast updated: {last_update}\nNCT: {nct_id}"
            ),
            author=company,
            media_type="filing",
            published_at=published,
        )
        if item is None:
            continue

        item.processed = True
        signal = Signal(
            item=item,
            signal_type="pilot",
            confidence=0.6,
            direction=Direction.neutral.value,
            summary=summary,
            entities_json=json.dumps({"tickers": [ticker] if ticker else []}),
        )
        session.add(signal)
        added += 1
        if added >= MAX_STUDIES_PER_SOURCE:
            break
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(
            Source.type == "clinicaltrial",
            Source.active.is_(True),
        )
        .all()
    )
    total = 0
    for source in sources:
        total += ingest_source(session, source)
    return total
