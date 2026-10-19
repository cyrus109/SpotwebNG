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
    # Spotnet sub-categories, pipe-terminated like Spotweb ('a9|a11|'), so a
    # LIKE '%a9|%' matches exactly one code. Meaning depends on the head
    # category: cat0 a=Format b=Source c=Language d=Genre z=Type.
    subcat_a: Mapped[str] = mapped_column(String(64), default="")
    subcat_b: Mapped[str] = mapped_column(String(64), default="")
    subcat_c: Mapped[str] = mapped_column(String(64), default="")
    subcat_d: Mapped[str] = mapped_column(String(128), default="")
    subcat_z: Mapped[str] = mapped_column(String(32), default="")
    spot_category: Mapped[int] = mapped_column(Integer, default=-1, index=True)
    # Optional rich metadata harvested from the spot body / lookups.
    imdb_id: Mapped[str] = mapped_column(String(16), default="", index=True)
    tvmaze_id: Mapped[str] = mapped_column(String(16), default="")
    poster_url: Mapped[str] = mapped_column(String(1024), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # NZB reference: the message-id of the NZB segment to fetch from Usenet.
    nzb_ref: Mapped[str] = mapped_column(String(255), default="")
    # Full-spot (X-XML) enrichment: 0 = pending, 1 = done, 2 = failed.
    enriched: Mapped[int] = mapped_column(Integer, default=0, index=True)
    website: Mapped[str] = mapped_column(String(1024), default="")
    # JSON list of NZB segment message-ids from the full-spot XML.
    nzb_segments: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Signature material (see signing.py). The raw From header is kept so verification can be
    # redone locally if the rules change — Usenet is never re-read for it.
    keyid: Mapped[int] = mapped_column(Integer, default=-1)
    spotter_id: Mapped[str] = mapped_column(String(16), default="", index=True)
    verified: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0 not evaluated
    from_hdr: Mapped[str] = mapped_column(String(700), default="")


class Spotter(Base):
    """A poster identified by key (spotter_id), with the admin's verdict on them."""

    __tablename__ = "spotters"

    spotter_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str] = mapped_column(String(12), default="neutral")  # neutral|trusted|blocked
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Comment(Base):
    """A Spotnet comment. Header comes from XOVER; the body is fetched on demand."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Message-id of the spot this comments on (derived from our own id, see comments.py).
    spot_message_id: Mapped[str] = mapped_column(String(255), index=True)
    poster: Mapped[str] = mapped_column(String(80), default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    # 0 = body not fetched yet, 1 = fetched, 2 = gone from the server.
    body_state: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0)
    spotter_id: Mapped[str] = mapped_column(String(16), default="")


class Download(Base):
    """A spot whose NZB has been delivered (see downloads.py)."""

    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="api")


class JobState(Base):
    """Progress of a derived-data background job (app/jobs.py)."""

    __tablename__ = "job_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending|running|done|failed
    processed: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiUser(Base):
    """A consumer of the Newznab API (e.g. a Radarr/Sonarr instance)."""

    __tablename__ = "api_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    api_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # Stamped on each authenticated API call so a key can be safely retired.
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """A person who can sign in. `admin` configures; `viewer` only browses."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stored lowercased: usernames are matched case-insensitively.
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Session(Base):
    """A logged-in browser session (opaque token in the sw_session cookie)."""

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsenetState(Base):
    """Incremental retrieval bookmark per stream (mirrors Spotweb's usenetstate)."""

    __tablename__ = "usenet_state"

    infotype: Mapped[str] = mapped_column(String(32), primary_key=True)  # 'spots'
    cur_article: Mapped[int] = mapped_column(BigInteger, default=0)
    cur_message_id: Mapped[str] = mapped_column(String(255), default="")
    last_retrieved: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Setting(Base):
    """Key/value store for the setup wizard (Usenet provider creds, etc.)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
