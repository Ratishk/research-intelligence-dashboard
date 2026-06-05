"""Famous hedge-fund 13F holdings tracker via free SEC EDGAR 13F-HR filings.

13F-HR ("Information Required of Institutional Investment Managers") filings are
filed quarterly under the FUND's own CIK and disclose every US-listed long
equity position. The holdings live in a SEPARATE information-table XML inside the
filing (sibling to primary_doc.xml), not in primary_doc.xml itself.

Pipeline per fund:
  1. submissions API -> list 13F-HR filings (reverse-chronological).
  2. For a given filing, read its index.json to locate the info-table XML
     (any *.xml that is not primary_doc.xml), fetch + parse it.
  3. Each <infoTable> carries nameOfIssuer (human-readable, e.g. "NVIDIA CORP"),
     cusip, value, and shares (sshPrnamt). Namespaces vary by filer/year, so we
     match by local tag name (tag.rsplit('}',1)[-1]).
  4. Aggregate rows by CUSIP (one issuer can appear in several rows for different
     classes / discretion buckets).

"What are they buying?": diff the two most recent 13F-HR filings by CUSIP ->
new / added / trimmed / exited positions. nameOfIssuer is human-readable so NO
CUSIP->ticker mapping is needed for display.

The `value` field is filed in whole dollars on modern filings but in THOUSANDS
on older ones. We auto-detect by sanity-checking implied price-per-share across
the portfolio and scale by 1000 when the whole book looks ~1000x too cheap.

Everything is wrapped in try/except — EDGAR throttles aggressively; this module
degrades to empty/partial results and never raises.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_TIMEOUT = 20
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

# Verified 13F-HR filers (each CIK confirmed to resolve to a 13F filer via the
# submissions API on 2026-06).
FAMOUS_FUNDS: dict[int, str] = {
    1067983: "Berkshire Hathaway (Warren Buffett)",
    1649339: "Scion Asset Management (Michael Burry)",
    1336528: "Pershing Square (Bill Ackman)",
    1350694: "Bridgewater Associates (Ray Dalio)",
    1656456: "Appaloosa (David Tepper)",
    1037389: "Renaissance Technologies",
    1423053: "Citadel Advisors (Ken Griffin)",
    1167483: "Tiger Global Management",
    1040273: "Third Point (Dan Loeb)",
    1079114: "Greenlight Capital (David Einhorn)",
}

_CACHE_TTL = 12 * 3600  # 12 hours
# key -> (monotonic_timestamp, value)
_holdings_cache: dict[int, tuple[float, dict]] = {}
_changes_cache: dict[int, tuple[float, dict]] = {}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _get_json(url: str):
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _list_13f_filings(cik: int) -> list[dict]:
    """Return 13F-HR filings (newest first) as {accession, date}."""
    try:
        recent = _get_json(_SUBMISSIONS.format(cik=cik)).get("filings", {}).get("recent", {})
    except Exception:
        logger.info("13F submissions fetch failed for CIK %s", cik)
        return []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    out = []
    for i, f in enumerate(forms):
        if f in ("13F-HR", "13F-HR/A"):
            out.append({"accession": accs[i], "date": dates[i]})
    return out


def _find_info_table_url(cik: int, accession: str) -> str | None:
    """Locate the info-table XML inside a filing via its index.json."""
    acc = accession.replace("-", "")
    base = _ARCHIVE.format(cik=cik, acc=acc)
    try:
        items = _get_json(base + "index.json")["directory"]["item"]
    except Exception:
        return None
    xmls = [it["name"] for it in items if it["name"].lower().endswith(".xml")]
    # The info table is the .xml that is not primary_doc.xml. Prefer files whose
    # name hints at an info table, else the first non-primary xml.
    candidates = [x for x in xmls if x.lower() != "primary_doc.xml"]
    if not candidates:
        return None
    for x in candidates:
        if "infotable" in x.lower() or "13f" in x.lower():
            return base + x
    return base + candidates[0]


def _parse_info_table(url: str) -> list[dict]:
    """Parse an info-table XML into aggregated-by-CUSIP holdings."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        tree = ET.fromstring(resp.text)
    except Exception:
        return []

    agg: dict[str, dict] = defaultdict(lambda: {"issuer": "", "value": 0.0, "shares": 0.0})
    for it in tree.iter():
        if _local(it.tag) != "infoTable":
            continue
        d: dict[str, str] = {}
        for c in it.iter():
            ln = _local(c.tag)
            if c.text and c.text.strip() and ln not in d:
                d[ln] = c.text.strip()
        cusip = (d.get("cusip") or d.get("nameOfIssuer") or "").upper()
        if not cusip:
            continue
        try:
            value = float((d.get("value") or "0").replace(",", ""))
        except ValueError:
            value = 0.0
        try:
            shares = float((d.get("sshPrnamt") or "0").replace(",", ""))
        except ValueError:
            shares = 0.0
        row = agg[cusip]
        row["value"] += value
        row["shares"] += shares
        if not row["issuer"]:
            row["issuer"] = d.get("nameOfIssuer") or cusip

    rows = list(agg.values())
    return _normalize_value_units(rows)


def _normalize_value_units(rows: list[dict]) -> list[dict]:
    """`value` is whole dollars on modern filings, THOUSANDS on older ones.

    Detect by median implied price-per-share: if the typical position prices well
    under $1/share, the values are in thousands -> scale by 1000.
    """
    prices = [r["value"] / r["shares"] for r in rows if r["shares"] > 0 and r["value"] > 0]
    if prices:
        prices.sort()
        median_px = prices[len(prices) // 2]
        if median_px < 1.0:  # implausible for equities -> values are in thousands
            for r in rows:
                r["value"] *= 1000.0
    return rows


def _holdings_for_filing(cik: int, accession: str) -> list[dict]:
    url = _find_info_table_url(cik, accession)
    if not url:
        return []
    rows = _parse_info_table(url)
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def get_fund_holdings(cik: int) -> dict:
    """Latest 13F top-15 holdings by value.

    Returns: {
        "cik": int, "name": str, "filing_date": str|None,
        "holdings": [{"issuer": str, "value_usd": int, "shares": int}, ...]
    }
    """
    now = time.monotonic()
    cached = _holdings_cache.get(cik)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    name = FAMOUS_FUNDS.get(cik, "")
    result = {"cik": cik, "name": name, "filing_date": None, "holdings": []}
    try:
        filings = _list_13f_filings(cik)
        if filings:
            latest = filings[0]
            result["filing_date"] = latest["date"]
            rows = _holdings_for_filing(cik, latest["accession"])
            result["holdings"] = [
                {
                    "issuer": r["issuer"],
                    "value_usd": int(r["value"]),
                    "shares": int(r["shares"]),
                }
                for r in rows[:15]
            ]
    except Exception:
        logger.exception("get_fund_holdings failed for CIK %s", cik)

    _holdings_cache[cik] = (now, result)
    return result


def get_fund_changes(cik: int) -> dict:
    """Diff the two most recent 13F-HR filings by CUSIP.

    Returns: {
        "cik": int, "name": str,
        "latest_date": str|None, "prior_date": str|None,
        "new":     [{"issuer": str, "value_usd": int}, ...],  # brand-new positions
        "added":   [{"issuer": str, "value_usd": int}, ...],  # increased share count
        "trimmed": [{"issuer": str, "value_usd": int}, ...],  # reduced share count
        "exited":  [{"issuer": str}, ...],                    # fully sold out
    }
    """
    now = time.monotonic()
    cached = _changes_cache.get(cik)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    name = FAMOUS_FUNDS.get(cik, "")
    result = {
        "cik": cik,
        "name": name,
        "latest_date": None,
        "prior_date": None,
        "new": [],
        "added": [],
        "trimmed": [],
        "exited": [],
    }
    try:
        filings = _list_13f_filings(cik)
        if len(filings) >= 2:
            latest, prior = filings[0], filings[1]
            result["latest_date"] = latest["date"]
            result["prior_date"] = prior["date"]
            cur = {
                r["issuer"].upper(): r
                for r in _holdings_for_filing(cik, latest["accession"])
            }
            old = {
                r["issuer"].upper(): r
                for r in _holdings_for_filing(cik, prior["accession"])
            }
            for key, r in cur.items():
                if key not in old:
                    result["new"].append(
                        {"issuer": r["issuer"], "value_usd": int(r["value"])}
                    )
                elif r["shares"] > old[key]["shares"] * 1.001:
                    result["added"].append(
                        {"issuer": r["issuer"], "value_usd": int(r["value"])}
                    )
                elif r["shares"] < old[key]["shares"] * 0.999:
                    result["trimmed"].append(
                        {"issuer": r["issuer"], "value_usd": int(r["value"])}
                    )
            for key, r in old.items():
                if key not in cur:
                    result["exited"].append({"issuer": r["issuer"]})

            result["new"].sort(key=lambda x: x["value_usd"], reverse=True)
            result["added"].sort(key=lambda x: x["value_usd"], reverse=True)
            result["trimmed"].sort(key=lambda x: x["value_usd"], reverse=True)
    except Exception:
        logger.exception("get_fund_changes failed for CIK %s", cik)

    _changes_cache[cik] = (now, result)
    return result


def get_all_funds() -> list[dict]:
    """Best-effort summary across all famous funds (skips failures).

    Returns a list of: {
        "cik": int, "name": str, "filing_date": str|None,
        "top_holdings": [{"issuer", "value_usd", "shares"}, ...],   # up to 3
        "recent_changes": {"new": int, "added": int, "trimmed": int, "exited": int},
    }
    """
    out = []
    for cik in FAMOUS_FUNDS:
        try:
            holdings = get_fund_holdings(cik)
            changes = get_fund_changes(cik)
            out.append(
                {
                    "cik": cik,
                    "name": FAMOUS_FUNDS[cik],
                    "filing_date": holdings.get("filing_date"),
                    "top_holdings": holdings.get("holdings", [])[:3],
                    "recent_changes": {
                        "new": len(changes.get("new", [])),
                        "added": len(changes.get("added", [])),
                        "trimmed": len(changes.get("trimmed", [])),
                        "exited": len(changes.get("exited", [])),
                    },
                }
            )
        except Exception:
            logger.exception("get_all_funds: skipping CIK %s", cik)
        time.sleep(0.3)  # be polite to EDGAR
    return out
