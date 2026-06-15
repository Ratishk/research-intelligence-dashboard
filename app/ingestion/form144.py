"""SEC Form 144 ingestion — notices of PROPOSED insider sales (no key).

Form 144 is filed when an affiliate intends to sell restricted/control stock —
filed BEFORE the sale, unlike Form 4 which reports it after. Clustered 144s are
a leading bearish indicator (upcoming insider supply). We parse the electronic
filing's raw ``primary_doc.xml`` (mandatory e-filing since April 2023) to pull
the seller, share count, aggregate market value and approximate sale date, then
emit a low-confidence ``insider_intent`` bearish signal per filing.

Raw 144 XML schema (ns http://www.sec.gov/edgar/ownership), the fields we map::

    formData/issuerInfo/nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold  -> person
    formData/issuerInfo/issuerName                                           -> issuer
    formData/securitiesInformation/noOfUnitsSold                             -> shares
    formData/securitiesInformation/aggregateMarketValue                     -> value_usd
    formData/securitiesInformation/approxSaleDate (MM/DD/YYYY)               -> approx sale date
    formData/securitiesInformation/brokerOrMarketmakerDetails/name          -> broker (optional)

The structured fields are stored as a JSON blob in ``Item.content`` (prefixed
``FORM144_JSON ``) so the /api/form144 endpoint can serve contract C2 without a
schema change. ``primaryDocument`` points at the XSLT-rendered HTML, so — as in
form4 — we strip the ``xsl144.../`` prefix to reach the raw XML.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.ingestion.form4 import _load_cik_map
from app.models import Direction, Signal, Source, SourceType

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_TIMEOUT = 20
MAX_FILINGS = 5
_FORMS = {"144", "144/A"}
_NS = "{http://www.sec.gov/edgar/ownership}"
# Marker prefixing the structured JSON we stash in Item.content for the API.
_CONTENT_PREFIX = "FORM144_JSON "


def _text(tree: ET.Element, path: str) -> str | None:
    el = tree.find(path)
    return el.text.strip() if el is not None and el.text else None


def _to_float(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _norm_sale_date(raw: str | None) -> str | None:
    """Form 144 carries the approx sale date as MM/DD/YYYY; emit ISO YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw or None


def _fetch_raw_xml(cik: int, accession: str, primary_doc: str) -> ET.Element | None:
    """Fetch the raw 144 XML. primary_doc is the XSLT-rendered variant; strip its
    rendering prefix (xsl144X0?/...) to reach the underlying primary_doc.xml."""
    acc_nodash = accession.replace("-", "")
    raw_doc = primary_doc.split("/")[-1] if primary_doc else "primary_doc.xml"
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{raw_doc}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return ET.fromstring(resp.text)
    except Exception:
        return None


def _parse_144(tree: ET.Element) -> dict:
    """Extract the structured fields from a parsed Form 144 XML element."""
    g = f".//{_NS}"
    person = _text(tree, g + "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
    issuer = _text(tree, g + "issuerName")
    shares = _to_float(_text(tree, g + "noOfUnitsSold"))
    value_usd = _to_float(_text(tree, g + "aggregateMarketValue"))
    approx = _norm_sale_date(_text(tree, g + "approxSaleDate"))
    # Broker name lives under brokerOrMarketmakerDetails/name (optional).
    broker = None
    broker_el = tree.find(g + "brokerOrMarketmakerDetails")
    if broker_el is not None:
        name_el = broker_el.find(f"{_NS}name")
        broker = name_el.text.strip() if name_el is not None and name_el.text else None
    return {
        "person": person,
        "issuer": issuer,
        "shares": shares,
        "value_usd": value_usd,
        "approx_sale_date": approx,
        "broker": broker,
    }


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
    docs = recent.get("primaryDocument", [])

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

        tree = _fetch_raw_xml(cik, accs[i], docs[i] if i < len(docs) else "")
        parsed = _parse_144(tree) if tree is not None else {}
        person = parsed.get("person") or "Insider"
        issuer = parsed.get("issuer") or ticker
        shares = parsed.get("shares")
        value_usd = parsed.get("value_usd")
        approx = parsed.get("approx_sale_date")

        shares_str = f"{int(shares):,} shares" if shares else "shares"
        value_str = f" (~${value_usd:,.0f})" if value_usd else ""
        when_str = f" around {approx}" if approx else ""
        summary = (
            f"Form 144: {person} plans to sell {shares_str} of {ticker}"
            f"{value_str}{when_str}"
        )

        payload = {
            "filed": published.isoformat() if published else None,
            "person": person,
            "issuer": issuer,
            "ticker": ticker,
            "shares": shares,
            "value_usd": value_usd,
            "approx_sale_date": approx,
            "broker": parsed.get("broker"),
            "url": url,
        }

        item = upsert_item(
            session, source=source, title=summary[:300], url=url,
            content=_CONTENT_PREFIX + json.dumps(payload),
            author=f"{person} — Form 144",
            media_type="filing", published_at=published,
        )
        if item is None:
            continue
        session.add(Signal(
            item_id=item.id, signal_type="insider_intent", confidence=0.75,
            direction=Direction.bearish.value, summary=summary,
            entities_json=json.dumps({"tickers": [ticker], "companies": [], "technologies": []}),
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
