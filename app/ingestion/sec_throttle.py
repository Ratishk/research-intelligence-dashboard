"""Shared per-host token bucket for data.sec.gov / sec.gov / efts.sec.gov.

EDGAR's fair-access policy caps clients at 10 requests/second per host. The SEC
ingesters (sec, form4, institutional, dilution, form144, events8k) each hit EDGAR
independently; once ingestion runs them in parallel threads they could collectively
blow past 10/s. This module provides a single process-wide token bucket they all
share so the *aggregate* SEC request rate stays within limits regardless of how
many ingester threads are running.

We cap to ~9/s (a small margin under 10) by enforcing a minimum interval between
the *starts* of successive requests. Network round-trips still overlap across
threads, so parallelism speeds up wall-clock time while the start-rate stays safe.
"""
from __future__ import annotations

import threading
import time

# ~9 req/s aggregate across all SEC ingester threads (margin under EDGAR's 10/s).
_MIN_INTERVAL = 0.11
_lock = threading.Lock()
_next_allowed = [0.0]


def acquire() -> None:
    """Block until it is safe to start the next SEC request (process-wide)."""
    with _lock:
        now = time.monotonic()
        start = max(now, _next_allowed[0])
        # Reserve this slot, then schedule the following one one interval later.
        _next_allowed[0] = start + _MIN_INTERVAL
        wait = start - now
    if wait > 0:
        time.sleep(wait)
