"""Central configuration loaded from environment.

A single ``COST_TIER`` knob drives the expensive levers described in the plan:
the Haiku->Sonnet relevance threshold, how often we poll X via Grok, and how
often the discovery engine re-validates sources.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class TierSettings:
    """Knobs that scale cost with coverage."""

    # Items scoring at/above this Haiku relevance (0-1) get the expensive
    # Sonnet classification pass. Higher threshold => fewer Sonnet calls.
    sonnet_relevance_threshold: float
    # How many times per day each X account is polled via Grok.
    x_polls_per_day: int
    # How many X accounts to batch into a single Grok search query.
    x_accounts_per_query: int
    # Discovery engine re-validation cadence, in days.
    discovery_interval_days: int
    # Max industries actively ingested (None = all).
    max_active_industries: int | None


_TIERS: dict[str, TierSettings] = {
    "lean": TierSettings(
        sonnet_relevance_threshold=0.7,
        x_polls_per_day=1,
        x_accounts_per_query=10,
        discovery_interval_days=14,
        max_active_industries=8,
    ),
    "standard": TierSettings(
        sonnet_relevance_threshold=0.5,
        x_polls_per_day=3,
        x_accounts_per_query=10,
        discovery_interval_days=7,
        max_active_industries=None,
    ),
    "max": TierSettings(
        sonnet_relevance_threshold=0.3,
        x_polls_per_day=24,
        x_accounts_per_query=5,
        discovery_interval_days=1,
        max_active_industries=None,
    ),
}


class Config:
    # API keys
    YOUTUBE_API_KEY = _get("YOUTUBE_API_KEY")
    XAI_API_KEY = _get("XAI_API_KEY")
    ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
    PERPLEXITY_API_KEY = _get("PERPLEXITY_API_KEY")
    GOOGLE_API_KEY = _get("GOOGLE_API_KEY")
    # X / Twitter API v2 (Bearer = app-only read access)
    X_BEARER_TOKEN = _get("X_BEARER_TOKEN")
    X_CONSUMER_KEY = _get("X_CONSUMER_KEY")
    X_CONSUMER_SECRET = _get("X_CONSUMER_SECRET")

    # Reddit
    REDDIT_CLIENT_ID = _get("REDDIT_CLIENT_ID")
    REDDIT_CLIENT_SECRET = _get("REDDIT_CLIENT_SECRET")
    REDDIT_USER_AGENT = _get("REDDIT_USER_AGENT", "research-dashboard/0.1")

    # FreshRSS
    FRESHRSS_API_URL = _get("FRESHRSS_API_URL")
    FRESHRSS_API_USER = _get("FRESHRSS_API_USER")
    FRESHRSS_API_PASSWORD = _get("FRESHRSS_API_PASSWORD")

    # Email
    SMTP_HOST = _get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(_get("SMTP_PORT", "587") or "587")
    SMTP_USER = _get("SMTP_USER")
    SMTP_PASS = _get("SMTP_PASS")
    DIGEST_EMAIL = _get("DIGEST_EMAIL")

    # Storage
    DATABASE_URL = _get("DATABASE_URL", "sqlite:///./research.db")

    # Model IDs
    HAIKU_MODEL = "claude-haiku-4-5-20251001"
    SONNET_MODEL = "claude-sonnet-4-6"

    COST_TIER = _get("COST_TIER", "lean").lower()

    @classmethod
    def tier(cls) -> TierSettings:
        return _TIERS.get(cls.COST_TIER, _TIERS["lean"])


config = Config()
