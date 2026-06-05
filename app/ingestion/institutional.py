"""SEC Schedule 13D / 13G ingestion — activist & large institutional stakes.

Filed when an entity crosses 5% beneficial ownership of a company. 13D = active
intent (often activist — bullish/event-driven); 13G = passive large holder
(index funds, long-term institutions). Both are filed under the COMPANY's CIK,
so they map 1:1 to watchlist tickers via the same CIK map used for Form 4.

Since ~Dec 2024 EDGAR mandates structured XML (primary_doc.xml); older filings
are free-form HTML/text — we parse the XML when present and fall back to the
submissions metadata otherwise, never crashing.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.ingestion.form4 import _load_cik_map  # reuse cached ticker->CIK map
from app.models import Direction, Signal, SignalType, Source, SourceType

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_TIMEOUT = 20
MAX_FILINGS = 5

_13D_FORMS = {"SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"}
_13G_FORMS = {"SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
_ALL_FORMS = _13D_FORMS | _13G_FORMS


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(tree: ET.Element, name: str) -> str | None:
    for el in tree.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return None


def _parse_stake(cik: int, accession: str, primary_doc: str) -> dict:
    acc = accession.replace("-", "")
    raw_doc = primary_doc.split("/")[-1]  # strip xsl…/ rendering prefix
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml"
    out = {"filer": None, "pct": None, "type": None}
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        tree = ET.fromstring(resp.text)
        out["filer"] = _find_text(tree, "reportingPersonName")
        out["pct"] = _find_text(tree, "classPercent")
        out["type"] = _find_text(tree, "typeOfReportingPerson")
    except Exception:
        pass
    return out


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
        logger.info("13D/G submissions fetch failed for %s", ticker)
        return 0

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    idxs = [i for i, f in enumerate(forms) if f in _ALL_FORMS][:MAX_FILINGS]
    added = 0
    for i in idxs:
        form = forms[i]
        is_activist = form in _13D_FORMS
        stake = _parse_stake(cik, accs[i], docs[i])
        filer = stake["filer"] or "Institutional filer"
        pct = f"{stake['pct']}% " if stake["pct"] else ""
        kind = "13D — activist" if is_activist else "13G — passive stake"

        if is_activist:
            direction = Direction.bullish.value
            confidence = 0.8
            sig_type = SignalType.commercial_deployment.value
        else:
            direction = Direction.neutral.value
            confidence = 0.65
            sig_type = SignalType.funding.value

        amended = form.endswith("/A")
        summary = (
            f"{filer} discloses {pct}stake in {ticker} ({kind}"
            f"{', amended' if amended else ''})"
        )

        acc = accs[i].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
        published = None
        if dates[i]:
            try:
                published = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=url,
            content=summary,
            author=filer,
            media_type="filing",
            published_at=published,
        )
        if item is None:
            continue
        signal = Signal(
            item_id=item.id,
            signal_type=sig_type,
            confidence=confidence,
            direction=direction,
            summary=summary,
            entities_json=f'{{"tickers": ["{ticker}"], "companies": [], "technologies": []}}',
            speculative=not is_activist,
        )
        session.add(signal)
        item.processed = True
        added += 1
    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.institutional.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("13D/G ingest error for %s", source.name)
    return total
