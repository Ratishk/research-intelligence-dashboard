"""One-time seeding: create industries, bootstrap sources, and watchlists.

Idempotent — safe to re-run. Existing industries/sources/watchlist rows are
matched by natural key and left in place.

    python seed_sources.py
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.database import init_db, session_scope
from app.ingestion.common import normalize_url
from app.models import Industry, Source, SourceStatus, WatchlistItem
from app.seed_data import (
    BIOTECH_SOURCES, BLUESKY_SOURCES, DILUTION_SOURCES, EVENT8K_SOURCES,
    FDARECALL_SOURCES, FORM4_SOURCES, FORM144_SOURCES, GOVCONTRACT_SOURCES,
    INDUSTRIES, INSTITUTIONAL_SOURCES, PATENT_SOURCES, SMART_MONEY_SOURCES,
    WATCHLISTS,
)


def _get_or_create_industry(session, name: str, group: str) -> Industry:
    industry = session.scalar(select(Industry).where(Industry.name == name))
    if industry is None:
        industry = Industry(name=name, group=group, target_source_count=30)
        session.add(industry)
        session.flush()
    return industry


def _get_or_create_source(session, name, type_, url, handle, tier, industry) -> Source:
    # Community sources (HN/Reddit keyword queries) have no canonical feed URL;
    # synthesize a stable one so the (type, url) uniqueness holds and dedupe works.
    norm_url = normalize_url(url) if url else f"internal://{type_}/{name}".replace(" ", "_")
    source = session.scalar(select(Source).where(Source.url == norm_url))
    if source is None:
        source = session.scalar(
            select(Source).where(Source.name == name, Source.type == type_)
        )
    if source is None:
        source = Source(
            name=name,
            type=type_,
            url=norm_url,
            handle=handle,
            tags=industry.name,
            tier=tier,
            vetting_score=11 if tier == 1 else 7,
            discovered_by="seed",
            status=SourceStatus.active.value,
            active=True,
            last_scored=datetime.now(timezone.utc),
        )
        session.add(source)
        session.flush()
    if industry not in source.industries:
        source.industries.append(industry)
    return source


def main() -> None:
    init_db()
    with session_scope() as session:
        ind_count = src_count = 0
        for name, spec in INDUSTRIES.items():
            industry = _get_or_create_industry(session, name, spec["group"])
            ind_count += 1
            for (s_name, s_type, s_url, s_handle, s_tier) in spec["sources"]:
                if not s_url and not s_handle:
                    continue  # engine-discovered placeholder slot
                _get_or_create_source(
                    session, s_name, s_type, s_url, s_handle, s_tier, industry
                )
                src_count += 1

        # Form 4 and patent sources — stored globally (no industry association needed)
        # but we create a stub "Insider / Patents" industry to keep the schema consistent.
        meta_industry = _get_or_create_industry(session, "Insider & Patents", "Meta")
        smart_industry = _get_or_create_industry(session, "Smart Money & Forecasts", "Meta")
        for (s_name, s_type, s_url, s_handle, s_tier) in FORM4_SOURCES + PATENT_SOURCES:
            _get_or_create_source(session, s_name, s_type, s_url, s_handle, s_tier, meta_industry)
            src_count += 1
        for (s_name, s_type, s_url, s_handle, s_tier) in (
            SMART_MONEY_SOURCES + INSTITUTIONAL_SOURCES + DILUTION_SOURCES
            + BIOTECH_SOURCES + EVENT8K_SOURCES + FORM144_SOURCES
            + GOVCONTRACT_SOURCES + FDARECALL_SOURCES + BLUESKY_SOURCES
        ):
            _get_or_create_source(session, s_name, s_type, s_url, s_handle, s_tier, smart_industry)
            src_count += 1

        wl_count = 0
        for list_name, symbols in WATCHLISTS.items():
            for symbol in symbols:
                exists = session.scalar(
                    select(WatchlistItem.id).where(
                        WatchlistItem.list_name == list_name,
                        WatchlistItem.ticker_symbol == symbol,
                    )
                )
                if not exists:
                    session.add(
                        WatchlistItem(list_name=list_name, ticker_symbol=symbol)
                    )
                    wl_count += 1

        print(f"Seeded {ind_count} industries, {src_count} source links, "
              f"{wl_count} new watchlist entries.")


if __name__ == "__main__":
    main()
