"""FINRA daily short-sale volume (free, no key).

FINRA publishes a daily pipe-delimited file of short volume per ticker across
its consolidated tape. Short volume as a % of total volume is a far timelier
read on bearish pressure than the biweekly short-interest report — a rising
ratio flags building distribution or squeeze setups.

File: https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
Columns: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
_HEADERS = {"User-Agent": "research-dashboard/0.1"}
_TIMEOUT = 15

# Cache the parsed latest file: (monotonic_ts, date_str, {symbol: ratio_dict}).
_cache: tuple[float, str, dict] | None = None
_TTL = 3600 * 4


def _fetch_latest() -> tuple[str, dict[str, dict]]:
    """Find the most recent available FINRA file (walk back over weekends/holidays)."""
    # Use a date passed via the real clock; walk back up to 6 days.
    today = datetime.now(timezone.utc).date()
    for back in range(0, 7):
        d = today - timedelta(days=back)
        date_str = d.strftime("%Y%m%d")
        try:
            resp = requests.get(_URL.format(date=date_str), headers=_HEADERS, timeout=_TIMEOUT)
            if resp.status_code != 200 or "|" not in resp.text[:200]:
                continue
        except Exception:
            continue
        rows: dict[str, dict] = {}
        for line in resp.text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) < 5 or not parts[1]:
                continue
            try:
                short_v = float(parts[2])
                total_v = float(parts[4])
            except ValueError:
                continue
            if total_v <= 0:
                continue
            rows[parts[1].upper()] = {
                "short_volume": short_v,
                "total_volume": total_v,
                "short_pct": round(short_v / total_v * 100, 1),
            }
        if rows:
            return date_str, rows
    return "", {}


def _data() -> tuple[str, dict[str, dict]]:
    global _cache
    now = time.monotonic()
    if _cache and (now - _cache[0]) < _TTL:
        return _cache[1], _cache[2]
    date_str, rows = _fetch_latest()
    _cache = (now, date_str, rows)
    return date_str, rows


def get_short_volume(symbols: list[str]) -> dict:
    """Return {date, tickers:[{symbol, short_pct, short_volume, total_volume}]} sorted hi->lo."""
    date_str, rows = _data()
    out = []
    for s in symbols:
        r = rows.get(s.upper())
        if r:
            out.append({"symbol": s.upper(), **r})
    out.sort(key=lambda x: x["short_pct"], reverse=True)
    pretty = ""
    if date_str:
        try:
            pretty = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pretty = date_str
    return {"date": pretty, "tickers": out}
