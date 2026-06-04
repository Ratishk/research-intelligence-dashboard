"""Measure signal yield per source and retire chronic under-performers.

signal_yield = signals produced / items ingested. Sources with enough items but
near-zero yield are retired, freeing slots for discovery to refill.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.models import Item, Signal, Source, SourceStatus

logger = logging.getLogger(__name__)

MIN_ITEMS_TO_JUDGE = 15
RETIRE_YIELD_BELOW = 0.02


def update_signal_yields(session) -> int:
    """Recompute signal_yield for every source from stored items/signals."""
    item_counts = dict(
        session.execute(
            select(Item.source_id, func.count(Item.id)).group_by(Item.source_id)
        ).all()
    )
    signal_counts = dict(
        session.execute(
            select(Item.source_id, func.count(Signal.id))
            .join(Signal, Signal.item_id == Item.id)
            .group_by(Item.source_id)
        ).all()
    )
    updated = 0
    for source in session.query(Source).all():
        items = item_counts.get(source.id, 0)
        if items == 0:
            continue
        source.signal_yield = signal_counts.get(source.id, 0) / items
        updated += 1
    return updated


def retire_underperformers(session) -> list[str]:
    """Retire sources with enough history but negligible yield. Returns names."""
    item_counts = dict(
        session.execute(
            select(Item.source_id, func.count(Item.id)).group_by(Item.source_id)
        ).all()
    )
    retired = []
    for source in (
        session.query(Source)
        .filter(Source.status == SourceStatus.active.value)
        .all()
    ):
        if source.discovered_by == "seed":
            continue  # keep human-vetted anchors regardless of yield
        items = item_counts.get(source.id, 0)
        if items >= MIN_ITEMS_TO_JUDGE and source.signal_yield < RETIRE_YIELD_BELOW:
            source.status = SourceStatus.retired.value
            source.active = False
            retired.append(source.name)
    if retired:
        logger.info("Retired %d underperforming sources: %s", len(retired), retired)
    return retired
