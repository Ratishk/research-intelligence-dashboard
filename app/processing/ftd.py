"""SEC Fails-to-Deliver (FTD) data (free, no key).

The SEC publishes semimonthly fails-to-deliver data: the aggregate number of
shares that failed to settle (CNS fails) per security. Persistent or spiking
FTDs are a classic squeeze-fuel / naked-short proxy — they flag settlement
stress that often precedes or accompanies short-squeeze dynamics.

File: https://www.sec.gov/files/data/fails-deliver-data/cnsfails{YYYYMM}{a|b}.zip
  (a = first half of month, b = second half). The zip holds one pipe-delimited
  .txt with columns:
  SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date

import requests

logger = logging.getLogger(__name__)

_URL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{period}.zip"
_HEADERS = {"User-Agent": "research-dashboard test@example.com"}
_TIMEOUT = 20

# Cache the parsed latest file: (monotonic_ts, period_label, {symbol: total_fails}).
_cache: tuple[float, str, dict] | None = None
_TTL = 3600 * 12


def _periods(n: int) -> list[str]:
    """Yield the last n half-month period labels (e.g. '202605b'), newest first."""
    out: list[str] = []
    today = date.today()
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y}{m:02d}b")
        out.append(f"{y}{m:02d}a")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _fetch_latest() -> tuple[str, dict[str, float]]:
    """Find the most recent available FTD zip (walk back over unposted periods)."""
    for period in _periods(2):
        try:
            resp = requests.get(_URL.format(period=period), headers=_HEADERS, timeout=_TIMEOUT)
            if resp.status_code != 200:
                continue
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except Exception:
            continue
        fails: dict[str, float] = {}
        try:
            name = zf.namelist()[0]
            text = zf.read(name).decode("latin-1")
        except Exception:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) < 4 or not parts[2]:
                continue
            try:
                qty = float(parts[3])
            except ValueError:
                continue
            sym = parts[2].strip().upper()
            if not sym:
                continue
            fails[sym] = fails.get(sym, 0.0) + qty
        if fails:
            return period, fails
    return "", {}


def _data() -> tuple[str, dict[str, float]]:
    global _cache
    now = time.monotonic()
    if _cache and (now - _cache[0]) < _TTL:
        return _cache[1], _cache[2]
    period, fails = _fetch_latest()
    _cache = (now, period, fails)
    return period, fails


def get_ftd(symbols: list[str]) -> dict:
    """Return {date, tickers:[{symbol, fails}]} sorted by fails desc."""
    try:
        period, fails = _data()
        out = []
        for s in symbols:
            f = fails.get(s.upper())
            if f:
                out.append({"symbol": s.upper(), "fails": int(f)})
        out.sort(key=lambda x: x["fails"], reverse=True)
        return {"date": period, "tickers": out}
    except Exception:
        return {"date": "", "tickers": []}
