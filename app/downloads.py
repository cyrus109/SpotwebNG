"""Download history: which spots have had their NZB handed out, so the UI can
show a checkmark. Keyed by message-id, not spot id, so the history survives
re-retrieving or wiping the spots table."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Download


async def record_download(session: AsyncSession, message_id: str, source: str) -> None:
    """Note that a spot's NZB was delivered ('api' = a Newznab client or the
    download link, 'send' = SABnzbd / black hole)."""
    now = datetime.now(UTC)
    for _ in range(2):  # a concurrent first download can lose the unique race once
        row = (
            await session.execute(select(Download).where(Download.message_id == message_id))
        ).scalar_one_or_none()
        if row is None:
            session.add(
                Download(
                    message_id=message_id[:255], first_at=now, last_at=now, count=1, source=source
                )
            )
        else:
            row.last_at, row.count, row.source = now, row.count + 1, source
        try:
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()


async def downloaded_map(session: AsyncSession, message_ids: list[str]) -> dict[str, Download]:
    if not message_ids:
        return {}
    rows = (
        (await session.execute(select(Download).where(Download.message_id.in_(message_ids))))
        .scalars()
        .all()
    )
    return {row.message_id: row for row in rows}
