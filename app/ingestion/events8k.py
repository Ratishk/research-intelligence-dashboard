"""SEC 8-K material-event ingestion via the EDGAR submissions API (no key).

8-Ks are the highest-signal routine filing — companies must report material
events within 4 business days: acquisitions, executive changes, guidance,
delistings, auditor changes, new agreements. We read the structured ``items``
codes from the submissions feed so we can label and direction-tag each event
without fetching the document.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.ingestion import sec_throttle
from app.ingestion.common import new_since_cursor, stamp_crawled, upsert_item
from app.ingestion.form4 import _load_cik_map
from app.models import Direction, Signal, SignalType, Source, SourceType

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_TIMEOUT = 20
MAX_FILINGS = 6

# 8-K item code -> (label, direction, signal_type). Codes not listed are skipped
# as low-signal (e.g. 9.01 exhibits, 7.01 Reg-FD boilerplate).
_ITEMS: dict[str, tuple[str, str, str]] = {
    "1.01": ("entered a material agreement", Direction.bullish.value, SignalType.funding.value),
    "1.02": ("terminated a material agreement", Direction.bearish.value, SignalType.bottleneck.value),
    "1.03": ("bankruptcy / receivership", Direction.bearish.value, SignalType.bottleneck.value),
    "2.01": ("completed an acquisition/disposition", Direction.bullish.value, SignalType.commercial_deployment.value),
    "2.02": ("reported results of operations", Direction.neutral.value, SignalType.pilot.value),
    "2.03": ("took on a material financial obligation", Direction.neutral.value, SignalType.funding.value),
    "2.04": ("triggered an obligation acceleration", Direction.bearish.value, SignalType.bottleneck.value),
    "2.05": ("announced material restructuring costs", Direction.bearish.value, SignalType.bottleneck.value),
    "3.01": ("received a delisting/listing-standard notice", Direction.bearish.value, SignalType.bottleneck.value),
    "4.01": ("changed its accountant/auditor", Direction.bearish.value, SignalType.bottleneck.value),
    "4.02": ("flagged non-reliance on prior financials", Direction.bearish.value, SignalType.bottleneck.value),
    "5.01": ("had a change in control", Direction.bullish.value, SignalType.commercial_deployment.value),
    "5.02": ("had an executive/director change", Direction.neutral.value, SignalType.pilot.value),
}


def ingest_source(session, source: Source) -> int:
    ticker = (source.handle or source.tags or source.name).strip().upper()
    if not ticker:
        return 0
    cik = _load_cik_map().get(ticker)
    if cik is None:
        return 0
    try:
        sec_throttle.acquire()
        resp = requests.get(_SUBMISSIONS.format(cik=cik), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        logger.info("8-K submissions fetch failed for %s", ticker)
        return 0

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    items_col = recent.get("items", [])

    fresh = new_since_cursor(source, dates)
    idxs = [i for i, f in enumerate(forms) if f == "8-K" and i in fresh][:MAX_FILINGS]
    stamp_crawled(source)
    added = 0
    for i in idxs:
        raw_items = (items_col[i] if i < len(items_col) else "") or ""
        codes = [c.strip() for c in raw_items.split(",") if c.strip() in _ITEMS]
        if not codes:
            continue  # only surface labelled, material items
        # Pick the highest-signal item (prefer a directional one).
        codes.sort(key=lambda c: _ITEMS[c][1] == Direction.neutral.value)
        label, direction, sig_type = _ITEMS[codes[0]]

        acc = accs[i].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
        published = None
        if dates[i]:
            try:
                published = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        summary = f"{ticker} 8-K — {label}"

        item = upsert_item(
            session, source=source, title=summary[:300], url=url,
            content=f"{summary}. Items: {raw_items}", author="SEC 8-K",
            media_type="filing", published_at=published,
        )
        if item is None:
            continue
        session.add(Signal(
            item_id=item.id, signal_type=sig_type, confidence=0.55,
            direction=direction, summary=summary,
            entities_json=f'{{"tickers": ["{ticker}"], "companies": [], "technologies": []}}',
            speculative=True,
        ))
        item.processed = True
        added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.form8k.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("8-K ingest error for %s", source.name)
    return total
