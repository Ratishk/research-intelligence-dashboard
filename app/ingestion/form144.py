"""SEC Form 144 ingestion — notices of PROPOSED insider sales (no key).

Form 144 is filed when an affiliate intends to sell restricted/control stock —
filed BEFORE the sale, unlike Form 4 which reports it after. Clustered 144s are
a leading bearish indicator (upcoming insider supply). We surface them as
low-confidence bearish signals from the EDGAR submissions feed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.ingestion.form4 import _load_cik_map
from app.models import Direction, Signal, SignalType, Source, SourceType

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_TIMEOUT = 20
MAX_FILINGS = 5
_FORMS = {"144", "144/A"}


def ingest_source(session, source: Source) -> int:
    ticker = (source.handle or source.tags or source.name).strip().upper()
    if not ticker:
        return 0
    cik = _load_cik_map().get(ticker)
    if cik is None:
        return 0
    try:
        resp = requests.get(_SUBMISSIONS.format(cik=cik), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        logger.info("Form 144 submissions fetch failed for %s", ticker)
        return 0

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])

    idxs = [i for i, f in enumerate(forms) if f in _FORMS][:MAX_FILINGS]
    added = 0
    for i in idxs:
        acc = accs[i].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
        published = None
        if dates[i]:
            try:
                published = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        summary = f"{ticker} Form 144 — insider notice of intent to sell"

        item = upsert_item(
            session, source=source, title=summary[:300], url=url,
            content=summary, author="SEC Form 144",
            media_type="filing", published_at=published,
        )
        if item is None:
            continue
        session.add(Signal(
            item_id=item.id, signal_type=SignalType.bottleneck.value, confidence=0.6,
            direction=Direction.bearish.value, summary=summary,
            entities_json=f'{{"tickers": ["{ticker}"], "companies": [], "technologies": []}}',
            speculative=True,
        ))
        item.processed = True
        added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.form144.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("Form 144 ingest error for %s", source.name)
    return total
