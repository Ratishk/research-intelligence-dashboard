"""ORM models for the research intelligence dashboard.

Schema mirrors the four-layer pipeline: a source registry (Industry, Source),
ingested content (Item), classified intelligence (Signal), and the investment
layer (Ticker, WatchlistItem). Digest records the daily email.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, enum.Enum):
    youtube = "youtube"
    twitter = "twitter"
    rss = "rss"
    forum = "forum"
    sec = "sec"
    reddit = "reddit"
    hackernews = "hackernews"
    arxiv = "arxiv"
    form4 = "form4"
    patent = "patent"
    congress = "congress"
    prediction = "prediction"
    institutional = "institutional"
    dilution = "dilution"
    clinicaltrial = "clinicaltrial"
    form8k = "form8k"
    form144 = "form144"
    govcontract = "govcontract"
    fdarecall = "fdarecall"
    bluesky = "bluesky"


class SourceStatus(str, enum.Enum):
    active = "active"
    candidate = "candidate"
    retired = "retired"


class SignalType(str, enum.Enum):
    commercial_deployment = "commercial_deployment"
    pilot = "pilot"
    funding = "funding"
    bottleneck = "bottleneck"
    displacement = "displacement"
    none = "none"


class Direction(str, enum.Enum):
    bullish = "bullish"
    bearish = "bearish"
    neutral = "neutral"


# A source can serve multiple industries (e.g. SemiAnalysis = AI + Tech Hardware).
source_industry = Table(
    "source_industry",
    Base.metadata,
    Column("source_id", ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
    Column("industry_id", ForeignKey("industries.id", ondelete="CASCADE"), primary_key=True),
)


class Industry(Base):
    __tablename__ = "industries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    group: Mapped[str] = mapped_column(String(80), default="")
    target_source_count: Mapped[int] = mapped_column(Integer, default=30)
    discovery_cadence: Mapped[str] = mapped_column(String(20), default="weekly")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    sources: Mapped[list["Source"]] = relationship(
        secondary=source_industry, back_populates="industries"
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    type: Mapped[str] = mapped_column(String(20), index=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    handle: Mapped[str] = mapped_column(String(200), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")

    tier: Mapped[int] = mapped_column(Integer, default=2)
    vetting_score: Mapped[int] = mapped_column(Integer, default=0)
    # Measured downstream: signals produced / items ingested.
    signal_yield: Mapped[float] = mapped_column(Float, default=0.0)
    discovered_by: Mapped[str] = mapped_column(String(40), default="seed")
    status: Mapped[str] = mapped_column(String(20), default=SourceStatus.active.value)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_crawled: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scored: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    industries: Mapped[list[Industry]] = relationship(
        secondary=source_industry, back_populates="sources"
    )
    items: Mapped[list["Item"]] = relationship(back_populates="source")

    __table_args__ = (UniqueConstraint("type", "url", name="uq_source_type_url"),)


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(600), default="")
    url: Mapped[str] = mapped_column(String(800), default="")
    # Hash of (source_domain, normalized_url) for cross-source dedupe.
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    media_type: Mapped[str] = mapped_column(String(20), default="article")

    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    # Set by the Haiku pre-filter; gates the expensive Sonnet pass.
    relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    source: Mapped[Source] = relationship(back_populates="items")
    signals: Mapped[list["Signal"]] = relationship(back_populates="item")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    industry_id: Mapped[int | None] = mapped_column(
        ForeignKey("industries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    signal_type: Mapped[str] = mapped_column(String(40), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    direction: Mapped[str] = mapped_column(String(20), default=Direction.neutral.value)
    summary: Mapped[str] = mapped_column(Text, default="")
    entities_json: Mapped[str] = mapped_column(Text, default="{}")
    research_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    speculative: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    item: Mapped[Item] = relationship(back_populates="signals")


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    sector: Mapped[str] = mapped_column(String(80), default="")
    tags: Mapped[str] = mapped_column(String(300), default="")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    week52_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    week52_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_earnings: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Fundamentals (Webull-equivalent)
    short_interest_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    short_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    analyst_rating: Mapped[str | None] = mapped_column(String(20), nullable=True)
    analyst_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    analyst_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # StockTwits social sentiment
    st_bull: Mapped[int] = mapped_column(Integer, default=0)
    st_bear: Mapped[int] = mapped_column(Integer, default=0)
    st_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Consensus engine — blended crowd-belief score (-1 bearish .. +1 bullish)
    consensus_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    consensus_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    consensus_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Volume / momentum (Yahoo v8 chart)
    rvol: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_5d_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_name: Mapped[str] = mapped_column(String(120), index=True)
    ticker_symbol: Mapped[str] = mapped_column(String(20), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("list_name", "ticker_symbol", name="uq_watchlist_ticker"),
    )


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DailyFinding(Base):
    """A stored daily DeepResearch top-N findings run (one row per day)."""
    __tablename__ = "daily_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    date_key: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # YYYY-MM-DD
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    packet_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class ShiftAlert(Base):
    """Persisted ticker polarity flip detected by insights.detect_shifts."""
    __tablename__ = "shift_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    from_score: Mapped[float] = mapped_column(Float)
    to_score: Mapped[float] = mapped_column(Float)
    recent_total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
