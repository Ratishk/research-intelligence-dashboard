"""Shared HTML fetch helper backed by the Scrapling library, with a graceful fallback.

Scrapling's basic ``Fetcher`` performs plain HTTP but impersonates real browser
TLS fingerprints (via curl_cffi), which helps with sources that 403 a vanilla
``requests`` call while still requiring NO browser download. ``StealthyFetcher``
drives a real (patched) browser and can bypass Cloudflare / JS challenges, but it
needs ``scrapling install`` to download a browser — reserved here for future
blocked sources only.

Every path is wrapped in try/except: on any failure we degrade to an empty
string (matching the project convention of never raising from processing code).
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# Realistic desktop-Chrome UA for the requests fallback.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _extract_html(resp) -> str:
    """Pull the HTML string out of whatever object a Scrapling fetcher returns."""
    for attr in ("html_content", "body", "text", "content"):
        val = getattr(resp, attr, None)
        if isinstance(val, bytes):
            try:
                return val.decode("utf-8", "replace")
            except Exception:
                continue
        if isinstance(val, str) and val:
            return val
    return str(resp)


def fetch_html(url: str, stealth: bool = False, timeout: int = 20) -> str:
    """Fetch ``url`` and return its HTML, or "" on any failure.

    Tries Scrapling's TLS-impersonating ``Fetcher`` first (no browser needed). If
    ``stealth=True`` it tries ``StealthyFetcher`` (real browser — needs
    ``scrapling install``) for Cloudflare-protected sources. On any import or
    network error it falls back to plain ``requests`` with a realistic UA.
    """
    # 1. Optional stealthy (browser) fetch for protected sources.
    if stealth:
        try:
            from scrapling.fetchers import StealthyFetcher

            resp = StealthyFetcher().fetch(url, timeout=timeout * 1000)
            html = _extract_html(resp)
            if html:
                return html
        except Exception:
            logger.info("StealthyFetcher unavailable/failed for %s; falling back", url)

    # 2. Scrapling basic Fetcher: plain HTTP with browser TLS impersonation.
    try:
        from scrapling.fetchers import Fetcher

        resp = Fetcher().get(url, timeout=timeout)
        html = _extract_html(resp)
        if html:
            return html
    except Exception:
        logger.info("Scrapling Fetcher failed for %s; falling back to requests", url)

    # 3. Last resort: plain requests with a realistic User-Agent.
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception:
        logger.info("requests fallback failed for %s", url)
        return ""
