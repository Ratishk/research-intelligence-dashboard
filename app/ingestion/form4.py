"""SEC Form 4 (insider trade) ingestion via the EDGAR submissions API.

For each watched ticker we resolve its CIK, pull the company's recent filings
(reverse-chronological), keep the Form 4s, and parse each filing's XML to extract
the transaction. Signals are created directly at confidence=0.85 — insider
trades are structured data, not LLM-classified prose.

Transaction codes (Table I, nonDerivative):
  P = open-market / private purchase  → bullish
  S = open-market / private sale      → bearish
  A = grant/award, G = gift, others   → neutral
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.models import Direction, Signal, SignalType, Source, SourceType

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_TIMEOUT = 20
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
MAX_FILINGS = 5

_CODE_DIRECTION = {
    "P": Direction.bullish.value,
    "S": Direction.bearish.value,
}

# Cached ticker -> CIK map (loaded once per process).
_cik_map: dict[str, int] | None = None


def _load_cik_map() -> dict[str, int]:
    global _cik_map
    if _cik_map is not None:
        return _cik_map
    try:
        resp = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        _cik_map = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
    except Exception:
        logger.exception("Failed to load SEC ticker->CIK map")
        _cik_map = {}
    return _cik_map


def _text(tree: ET.Element, path: str) -> str | None:
    el = tree.find(path)
    return el.text.strip() if el is not None and el.text else None


def _insider_role(tree: ET.Element) -> str:
    rel = ".//reportingOwner/reportingOwnerRelationship/"
    title = _text(tree, rel + "officerTitle")
    if title:
        return title
    if _text(tree, rel + "isDirector") in ("1", "true"):
        return "Director"
    if _text(tree, rel + "isOfficer") in ("1", "true"):
        return "Officer"
    if _text(tree, rel + "isTenPercentOwner") in ("1", "true"):
        return "10% owner"
    return "Insider"


def _parse_transaction(tree: ET.Element) -> dict | None:
    """Extract the first non-derivative transaction from a Form 4 XML."""
    for tx in tree.findall(".//nonDerivativeTransaction"):
        code = _text(tx, ".//transactionCoding/transactionCode")
        if not code:
            continue
        try:
            shares = float((_text(tx, ".//transactionAmounts/transactionShares/value") or "0").replace(",", ""))
        except ValueError:
            shares = 0.0
        try:
            price = float((_text(tx, ".//transactionAmounts/transactionPricePerShare/value") or "0").replace(",", ""))
        except ValueError:
            price = 0.0
        return {"code": code.upper(), "shares": shares, "price": price}
    return None


def _fetch_form4_xml(cik: int, accession: str, primary_doc: str) -> ET.Element | None:
    """Fetch the raw Form 4 XML. primary_doc may be the XSLT-rendered variant."""
    acc_nodash = accession.replace("-", "")
    # Strip the xslF345X0N/ rendering prefix to reach the raw XML.
    raw_doc = primary_doc.split("/")[-1]
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{raw_doc}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return ET.fromstring(resp.text), url
    except Exception:
        return None, url


def ingest_source(session, source: Source) -> int:
    ticker = (source.handle or source.tags or source.name).strip().upper()
    if not ticker:
        return 0
    cik = _load_cik_map().get(ticker)
    if cik is None:
        logger.warning("No CIK for ticker %s", ticker)
        return 0

    try:
        resp = requests.get(_SUBMISSIONS.format(cik=cik), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
    except Exception:
        logger.exception("Submissions fetch failed for %s (CIK %s)", ticker, cik)
        return 0

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    form4_idx = [i for i, f in enumerate(forms) if f == "4"][:MAX_FILINGS]
    added = 0
    for i in form4_idx:
        accession = accs[i]
        tree_result = _fetch_form4_xml(cik, accession, docs[i])
        tree, filing_url = tree_result
        published = None
        if dates[i]:
            try:
                published = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        if tree is not None:
            tx = _parse_transaction(tree)
            owner = _text(tree, ".//reportingOwner/reportingOwnerId/rptOwnerName") or "Insider"
            role = _insider_role(tree)
        else:
            tx, owner, role = None, "Insider", "Insider"

        if tx:
            code = tx["code"]
            direction = _CODE_DIRECTION.get(code, Direction.neutral.value)
            action = "buys" if code == "P" else "sells" if code == "S" else "transfers"
            price_str = f" at ${tx['price']:,.2f}" if tx["price"] else ""
            shares_str = f"{int(tx['shares']):,} shares" if tx["shares"] else "shares"
            summary = f"{owner} ({role}) {action} {shares_str} of {ticker}{price_str}"
        else:
            direction = Direction.neutral.value
            summary = f"Form 4 insider filing — {ticker} ({owner})"

        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=filing_url,
            content=summary,
            author=f"{owner} — {role}",
            media_type="filing",
            published_at=published,
        )
        if item is None:
            continue  # duplicate

        signal = Signal(
            item_id=item.id,
            signal_type=SignalType.commercial_deployment.value
            if direction == Direction.bullish.value
            else "pilot",
            confidence=0.85,
            direction=direction,
            summary=summary,
            entities_json=f'{{"tickers": ["{ticker}"], "companies": [], "technologies": []}}',
            speculative=False,
        )
        session.add(signal)
        item.processed = True
        added += 1

    return added


def ingest_all(session) -> int:
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.form4.value)
        .all()
    )
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:
            logger.exception("Form 4 ingest error for %s", source.name)
    return total
