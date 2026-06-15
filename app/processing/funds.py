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

import csv as _csv
import datetime as _dt
import logging
import os as _os
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "research-dashboard ratishsaaik@gmail.com"}
_TIMEOUT = 20
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

# Path to the hedge-fund universe CSV (cik,name,featured). Built from the SEC
# EDGAR quarterly form index (every entity actually filed a 13F-HR).
_FUNDS_CSV = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "data",
    "hedge_funds.csv",
)

# Hardcoded fallback: the 10 canonical funds, used only if the CSV is missing or
# unreadable so the app never breaks. Each CIK is a verified 13F-HR filer.
_FALLBACK_FUNDS: dict[int, str] = {
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


def _load_funds(path: str = _FUNDS_CSV) -> tuple[dict[int, str], dict[int, str]]:
    """Load the hedge-fund universe from CSV.

    Returns (all_funds, featured_funds) as {cik: name} dicts. `all_funds` is the
    full universe (hundreds) used for cross-fund consensus; `featured_funds` is
    the small famous subset (featured==1) rendered as per-fund cards. Falls back
    to the 10 canonical funds if the CSV is missing/empty/unreadable.
    """
    all_funds: dict[int, str] = {}
    featured: dict[int, str] = {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                try:
                    cik = int(row["cik"])
                except (KeyError, ValueError, TypeError):
                    continue
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                all_funds[cik] = name
                if str(row.get("featured", "")).strip() == "1":
                    featured[cik] = name
    except Exception:
        logger.warning("hedge_funds.csv unreadable; using fallback fund list", exc_info=True)

    if not all_funds:
        logger.warning("hedge_funds.csv empty/missing; using fallback fund list")
        all_funds = dict(_FALLBACK_FUNDS)
    if not featured:
        featured = dict(_FALLBACK_FUNDS)
    return all_funds, featured


# FAMOUS_FUNDS: the FULL universe (hundreds) -> drives cross-fund consensus and
# all name lookups. FEATURED_FUNDS: the famous subset -> drives per-fund cards.
FAMOUS_FUNDS, FEATURED_FUNDS = _load_funds()

_CACHE_TTL = 12 * 3600  # 12 hours
# key -> (monotonic_timestamp, value)
_holdings_cache: dict[int, tuple[float, dict]] = {}
_changes_cache: dict[int, tuple[float, dict]] = {}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# Cross-thread rate limiter — EDGAR fair-access is 10 req/s. We delegate to the
# shared per-host token bucket (app.ingestion.sec_throttle) so the consensus thread
# pool AND the SEC ingesters cap their *aggregate* start rate to ~9/s on data.sec.gov.
from app.ingestion import sec_throttle


def _throttle() -> None:
    sec_throttle.acquire()


def _get_json(url: str):
    _throttle()
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
        _throttle()
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
        row["cusip"] = cusip
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
    """Best-effort per-fund summary across the FEATURED funds (skips failures).

    Iterates only FEATURED_FUNDS (the famous ~15), not the full FAMOUS_FUNDS
    universe: rendering cards + EDGAR calls for hundreds of funds would be far
    too slow. Cross-fund consensus (get_consensus) still uses the full universe.

    Returns a list of: {
        "cik": int, "name": str, "filing_date": str|None,
        "top_holdings": [{"issuer", "value_usd", "shares"}, ...],   # up to 3
        "recent_changes": {"new": int, "added": int, "trimmed": int, "exited": int},
    }
    """
    out = []
    for cik in FEATURED_FUNDS:
        try:
            holdings = get_fund_holdings(cik)
            changes = get_fund_changes(cik)
            out.append(
                {
                    "cik": cik,
                    "name": FEATURED_FUNDS[cik],
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


_consensus_cache: tuple[float, dict] | None = None
# 13F data only changes quarterly, so cache the consensus for ~a quarter; the
# scheduler force-refreshes it during the filing window (see is_13f_filing_window).
_CONSENSUS_TTL = 80 * 24 * 3600


def is_13f_filing_window(today: _dt.date | None = None) -> bool:
    """True during the ~3-week window each quarter when 13F-HRs are filed.

    13F-HRs are due 45 days after quarter-end: ~Feb 14, May 15, Aug 14, Nov 14.
    We treat (deadline - 7 days) .. (deadline + 16 days) as "filing season", when
    new filings stream in and the consensus should refresh aggressively."""
    d = today or _dt.date.today()
    deadlines = [_dt.date(d.year, 2, 14), _dt.date(d.year, 5, 15),
                 _dt.date(d.year, 8, 14), _dt.date(d.year, 11, 14)]
    return any(-7 <= (d - dl).days <= 16 for dl in deadlines)


def _fund_latest_rows(cik: int) -> tuple[int, str | None, list[dict]]:
    """Fetch one fund's latest-13F holdings (the parallelizable network work)."""
    try:
        filings = _list_13f_filings(cik)
        if not filings:
            return cik, None, []
        latest = filings[0]
        return cik, latest.get("date"), _holdings_for_filing(cik, latest["accession"])
    except Exception:
        logger.exception("consensus: CIK %s fetch failed", cik)
        return cik, None, []


def _report_quarter(date_str: str | None) -> str | None:
    """Map a 13F-HR *filing* date (~45d after quarter-end) to the quarter it reports."""
    if not date_str:
        return None
    try:
        y, m, _ = (int(x) for x in date_str.split("-"))
    except (ValueError, AttributeError):
        return None
    if m <= 3:
        return f"Q4 {y - 1}"
    if m <= 6:
        return f"Q1 {y}"
    if m <= 9:
        return f"Q2 {y}"
    return f"Q3 {y}"


def get_consensus(top_per_fund: int = 25, min_funds: int = 2, force: bool = False) -> dict:
    """Cross-fund overlap: which names the famous funds are collectively in.

    Aggregates each fund's latest-13F top positions by CUSIP issuer (first 6 digits,
    so share classes merge) and counts how many funds hold each. The "consensus bets"
    are the issuers held by the most funds. Per-fund EDGAR fetches run in parallel
    (rate-limited to EDGAR's 10 req/s). Cached ~a quarter; the scheduler force-refreshes
    during the filing window so new filings appear same-day. Pass force=True to refresh.

    Returns: {
        "funds_total": int,            # funds we got holdings for
        "as_of": str|None,             # latest filing date seen
        "quarter": str|None,           # e.g. "Q1 2026"
        "consensus": [{
            "issuer": str, "cusip6": str, "fund_count": int, "total_value_usd": int,
            "holders": [{"name","cik","value_usd","shares","weight_pct","date"}, ...],
        }, ...]                        # sorted by fund_count, then total value
    }
    """
    global _consensus_cache
    now = time.monotonic()
    if not force and _consensus_cache and now - _consensus_cache[0] < _CONSENSUS_TTL:
        return _consensus_cache[1]
    # Cold start: warm the in-memory cache from disk before the expensive scrape,
    # so a restart doesn't re-fetch hundreds of 13F filings.
    if not force and _consensus_cache is None:
        from app import cache as _disk_cache
        disk = _disk_cache.get("fund_consensus")
        if disk is not None:
            _consensus_cache = (now, disk)
            return disk

    # Fetch every fund's latest holdings in parallel (network-bound; throttled to
    # ~9 req/s globally so we stay within EDGAR's 10/s limit).
    fetched: dict[int, tuple[str | None, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fund_latest_rows, cik): cik for cik in FAMOUS_FUNDS}
        for fut in as_completed(futs):
            cik, fdate, rows = fut.result()
            fetched[cik] = (fdate, rows)

    agg: dict[str, dict] = {}
    funds_seen = 0
    dates: list[str] = []
    for cik, name in FAMOUS_FUNDS.items():  # deterministic aggregation order
        fdate, rows = fetched.get(cik, (None, []))
        if not rows:
            continue
        funds_seen += 1
        if fdate:
            dates.append(fdate)
        port_val = sum(r["value"] for r in rows) or 1.0
        for r in rows[:top_per_fund]:
            key = (r.get("cusip") or r.get("issuer") or "")[:6].upper()
            if not key:
                continue
            a = agg.setdefault(key, {"issuer": r["issuer"], "total_value": 0.0, "by_fund": {}})
            a["total_value"] += r["value"]
            # keep the shortest issuer label (usually the cleanest base name)
            if r["issuer"] and (not a["issuer"] or len(r["issuer"]) < len(a["issuer"])):
                a["issuer"] = r["issuer"]
            # one entry per fund — a fund holding multiple share classes (same CUSIP-6)
            # is merged so fund_count counts DISTINCT funds.
            hf = a["by_fund"].setdefault(cik, {
                "name": name, "cik": cik, "value_usd": 0, "shares": 0, "weight_pct": 0.0, "date": fdate,
            })
            hf["value_usd"] += int(r["value"])
            hf["shares"] += int(r["shares"])
            hf["weight_pct"] = round(hf["weight_pct"] + r["value"] / port_val * 100, 1)

    consensus = []
    for key, a in agg.items():
        n = len(a["by_fund"])
        if n >= min_funds:
            consensus.append({
                "issuer": a["issuer"],
                "cusip6": key,
                "fund_count": n,
                "total_value_usd": int(a["total_value"]),
                "holders": sorted(a["by_fund"].values(), key=lambda x: x["value_usd"], reverse=True),
            })
    consensus.sort(key=lambda x: (x["fund_count"], x["total_value_usd"]), reverse=True)
    consensus = consensus[:150]                  # cap the list (mega-caps dominate the tail)
    for c in consensus:
        c["holders"] = c["holders"][:25]         # cap holders per name (payload size)

    result = {
        "funds_total": funds_seen,
        "as_of": max(dates) if dates else None,
        "quarter": _report_quarter(max(dates)) if dates else None,
        "consensus": consensus,
        "computing": False,
    }
    _consensus_cache = (now, result)
    # Persist to disk so a restart can load-from-disk instead of cold-rebuilding.
    if result.get("consensus"):
        from app import cache as _disk_cache
        _disk_cache.set("fund_consensus", result, ttl=_CONSENSUS_TTL)
    return result


# ── Non-blocking access + background warming ────────────────────────────────
# With hundreds of funds a cold scrape takes minutes, so the API never calls
# get_consensus() directly — it serves the cache and warms in the background.
_warming_lock = threading.Lock()
_is_warming = False


def warm_consensus(force: bool = False) -> None:
    """Build/refresh the consensus in a background thread (one at a time)."""
    global _is_warming
    with _warming_lock:
        if _is_warming:
            return
        _is_warming = True

    def _run() -> None:
        global _is_warming
        try:
            get_consensus(force=force)
        except Exception:
            logger.exception("consensus warm failed")
        finally:
            with _warming_lock:
                _is_warming = False

    threading.Thread(target=_run, daemon=True, name="consensus-warm").start()


def get_consensus_cached() -> dict:
    """Non-blocking: return the cached consensus immediately. If it's cold or
    stale, kick a background build and return a 'computing' placeholder (or the
    stale data flagged) so the request never blocks for minutes."""
    global _consensus_cache
    now = time.monotonic()
    if _consensus_cache and now - _consensus_cache[0] < _CONSENSUS_TTL:
        return _consensus_cache[1]
    # Cold start: serve the disk cache immediately if present (avoids the minutes-long
    # cold rebuild after a restart) and warm the in-memory cache from it.
    if _consensus_cache is None:
        from app import cache as _disk_cache
        disk = _disk_cache.get("fund_consensus")
        if disk is not None:
            _consensus_cache = (now, disk)
            return disk
    warm_consensus(force=False)
    if _consensus_cache:  # serve stale while refreshing
        return {**_consensus_cache[1], "computing": True}
    return {"funds_total": 0, "as_of": None, "quarter": None, "consensus": [], "computing": True}
