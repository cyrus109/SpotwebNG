"""SQLAlchemy models — the modern SpotwebNG schema.

Mirrors the essentials of a Spotnet "spot" (a signed Usenet post describing
content) plus the config the engine and API need. Deliberately small for
Phase 1; the legacy Spotweb schema had ~40 columns of cruft we don't carry.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Spot(Base):
    """A single Spotnet spot — the unit Radarr/Sonarr search for."""

    __tablename__ = "spots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    poster: Mapped[str] = mapped_column(String(255), default="")
    # Newznab category, e.g. 2040 (Movies/HD), 5040 (TV/HD). See categories.py.
    category: Mapped[int] = mapped_column(Integer, index=True, default=0)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Optional rich metadata harvested from the spot body / lookups.
    imdb_id: Mapped[str] = mapped_column(String(16), default="", index=True)
    tvmaze_id: Mapped[str] = mapped_column(String(16), default="")
    poster_url: Mapped[str] = mapped_column(String(1024), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # NZB reference: the message-id of the NZB segment to fetch from Usenet.
    nzb_ref: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiUser(Base):
    """A consumer of the Newznab API (e.g. a Radarr/Sonarr instance)."""

    __tablename__ = "api_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    api_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Setting(Base):
    """Key/value store for the setup wizard (Usenet provider creds, etc.)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
