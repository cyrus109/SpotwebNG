"""Export and re-import the index, so a rebuild doesn't mean re-reading Usenet.

Format: gzip'd JSON Lines. Line 1 is a header (`format`, `version`); every other
line is `{"t": table, "r": {column: value}}`. Spots, comments and the retrieval
bookmarks are exported; credentials, users, sessions and API keys never are.

Importing is additive: rows whose message-id already exists are skipped, and
bookmarks are only taken when this install has none. The file is untrusted
input: every value is coerced to its column type and length, lines are capped.
Cached cover art isn't exported, so spots that pointed at a local cover are
queued for re-enrichment.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import zlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Comment, Spot, UsenetState

FORMAT = "spotwebng-export"
VERSION = 1
CHUNK = 2000
MAX_LINE = 2 * 1024 * 1024
# Not exported: ids differ per install, timestamps are regenerated.
_SKIP = {"id", "created_at", "last_retrieved"}
TABLES: dict[str, tuple[type, str]] = {
    "spots": (Spot, "message_id"),
    "comments": (Comment, "message_id"),
    "usenet_state": (UsenetState, "infotype"),
}


def _columns(model: type) -> list:
    return [c for c in model.__table__.columns if c.name not in _SKIP]  # type: ignore[attr-defined]


def _dump(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


async def export_chunks(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[bytes]:
    """Stream the export as gzip, paging by id so memory stays flat."""
    comp = zlib.compressobj(6, zlib.DEFLATED, 31)

    def emit(obj: dict) -> bytes:
        return comp.compress((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    out = emit({"format": FORMAT, "version": VERSION, "created": datetime.now(UTC).isoformat()})
    async with factory() as session:
        for name, (model, _) in TABLES.items():
            cols = _columns(model)
            has_id = hasattr(model, "id")
            last_id = 0
            while True:
                stmt = select(model)
                if has_id:
                    stmt = stmt.where(model.id > last_id).order_by(model.id).limit(CHUNK)
                rows = (await session.execute(stmt)).scalars().all()
                for row in rows:
                    out += emit(
                        {"t": name, "r": {c.name: _dump(getattr(row, c.name)) for c in cols}}
                    )
                    if len(out) > 64 * 1024:
                        yield out
                        out = b""
                if not has_id or len(rows) < CHUNK:
                    break
                last_id = rows[-1].id
                session.expunge_all()
    out += comp.flush()
    if out:
        yield out


def _coerce_row(model: type, raw: Any) -> dict | None:
    """Validate one untrusted row against the model's columns; None if unusable."""
    if not isinstance(raw, dict):
        return None
    clean: dict[str, Any] = {}
    for col in _columns(model):
        if col.name not in raw:
            continue
        value, kind = raw[col.name], col.type.python_type
        try:
            if kind is datetime:
                parsed = datetime.fromisoformat(str(value))
                clean[col.name] = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            elif kind is int:
                if isinstance(value, bool):
                    raise ValueError
                clean[col.name] = int(value)
            else:
                text = "" if value is None else str(value)
                limit = getattr(col.type, "length", None)
                clean[col.name] = text[:limit] if limit else text
        except (TypeError, ValueError, OverflowError):
            return None
    for col in _columns(model):  # a row missing a required column would fail the whole batch
        if not col.nullable and col.default is None and col.server_default is None:
            if col.name not in clean:
                return None
    return clean


def _read_lines(text: io.TextIOBase, n: int) -> list[str]:
    lines: list[str] = []
    while len(lines) < n:
        line = text.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            raise ValueError("export file contains an oversized line")
        lines.append(line)
    return lines


async def _flush(session: AsyncSession, name: str, rows: list[dict], result: dict) -> None:
    model, key = TABLES[name]
    if not rows:
        return
    keys = [r[key] for r in rows if key in r]
    column = getattr(model, key)
    known = set((await session.execute(select(column).where(column.in_(keys)))).scalars().all())
    seen: set[str] = set()
    for row in rows:
        ident = row.get(key)
        if not ident or ident in known or ident in seen:
            result["skipped"] += 1
            continue
        seen.add(ident)
        if name == "spots" and str(row.get("poster_url", "")).startswith("/image/"):
            row["poster_url"], row["enriched"] = "", 0  # the cover wasn't exported
        session.add(model(**row))
        result["added"][name] = result["added"].get(name, 0) + 1
    await session.commit()


async def import_file(
    factory: async_sessionmaker[AsyncSession],
    path: str,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Merge an export into the database. Raises ValueError for a file that isn't one."""
    total = max(os.path.getsize(path), 1)
    result: dict = {"added": {}, "skipped": 0}
    with open(path, "rb") as raw:
        text = io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8", errors="replace")
        try:
            header_lines = await asyncio.to_thread(_read_lines, text, 1)
            try:
                header = json.loads(header_lines[0]) if header_lines else None
            except ValueError:
                header = None
            if not isinstance(header, dict) or header.get("format") != FORMAT:
                raise ValueError("This is not a SpotwebNG export file")
            if header.get("version") != VERSION:
                raise ValueError(f"Unsupported export version: {header.get('version')!r}")

            async with factory() as session:
                batch: dict[str, list[dict]] = {name: [] for name in TABLES}
                while True:
                    try:
                        lines = await asyncio.to_thread(_read_lines, text, CHUNK)
                    except (OSError, EOFError, zlib.error) as exc:
                        raise ValueError(f"Export file is damaged: {exc}") from exc
                    if not lines:
                        break
                    for line in lines:
                        try:
                            item = json.loads(line)
                            name = item["t"]
                            model = TABLES[name][0]
                        except (ValueError, KeyError, TypeError):
                            result["skipped"] += 1
                            continue
                        row = _coerce_row(model, item.get("r"))
                        if row is None:
                            result["skipped"] += 1
                        else:
                            batch[name].append(row)
                    for name, rows in batch.items():
                        if name != "usenet_state":
                            await _flush(session, name, rows, result)
                            rows.clear()
                    if on_progress:
                        on_progress(min(raw.tell() / total * 100, 99.0), f"{_added(result):,} rows")
                # Bookmarks last, and only where this install has none: they must
                # match the data, and a local bookmark already does.
                await _flush(session, "usenet_state", batch["usenet_state"], result)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError(f"Export file is damaged: {exc}") from exc
    return result


def _added(result: dict) -> int:
    return sum(result["added"].values())
