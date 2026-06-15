"""SEC share-dilution / offering ingestion via the EDGAR submissions API.

For each watched ticker we resolve its CIK, pull the company's recent filings
(reverse-chronological), and keep the registration / offering forms that signal a
potential share issuance or dilution event (S-1, S-3 shelf, 424B prospectus, etc.).

Signals are created directly — no LLM. A new dilution/offering filing is treated as
a bearish, non-consensus (speculative) signal: management is preparing to sell
shares, which dilutes existing holders.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.ingestion import sec_throttle
from app.ingestion.common import new_since_cursor, stamp_crawled, upsert_item
from app.ingestion.form4 import _load_cik_map
from app.models import Direction, Signal, SignalType, Source

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_TIMEOUT = 20
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
MAX_FILINGS = 5

# Registration / offering forms that indicate potential share dilution.
_DILUTION_FORMS = {
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "424B5",
    "424B3",
    "424B4",
    "S-3ASR",
}


def ingest_source(session, source: Source) -> int:
    ticker = (source.handle or source.tags or source.name).strip().upper()
    if not ticker:
        return 0
    cik = _load_cik_map().get(ticker)
    if cik is None:
        logger.warning("No CIK for ticker %s", ticker)
        return 0

    try:
        sec_throttle.acquire()
        resp = requests.get(_SUBMISSIONS.format(cik=cik), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        logger.exception("Submissions fetch failed for %s (CIK %s)", ticker, cik)
        return 0

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])

    fresh = new_since_cursor(source, dates)
    dilution_idx = [
        i for i, f in enumerate(forms) if f in _DILUTION_FORMS and i in fresh
    ][:MAX_FILINGS]
    stamp_crawled(source)
    added = 0
    for i in dilution_idx:
        form = forms[i]
        accession = accs[i]
        acc_nodash = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"

        published = None
        if dates[i]:
            try:
                published = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        summary = f"{ticker} files {form} — potential share dilution/offering"

        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=filing_url,
            content=summary,
            author="SEC EDGAR",
            media_type="filing",
            published_at=published,
        )
        if item is None:
            continue  # duplicate

        signal = Signal(
            item_id=item.id,
            signal_type=SignalType.bottleneck.value,
            confidence=0.7,
            direction=Direction.bearish.value,
            summary=summary,
            entities_json=f'{{"tickers": ["{ticker}"], "companies": [], "technologies": []}}',
            speculative=True,
        )
        session.add(signal)
        item.processed = True
        added += 1

    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == "dilution")
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("Dilution ingest error for %s", source.name)
    return total
