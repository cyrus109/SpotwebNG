"""Import an existing classic Spotweb database, so moving to SpotwebNG does not mean
re-reading Usenet.

    python -m app.spotweb_import --url 'mysql+pymysql://user:pass@host/spotweb'
    python -m app.spotweb_import --url 'postgresql+psycopg2://…'   |   'sqlite:////path/spotweb.db'

(The URL can also come from SPOTWEB_DB_URL so the password stays out of your shell history. MySQL
and PostgreSQL need their driver installed: `pip install pymysql` / `psycopg2-binary`.)

Reads, keyset-paged by id: spots ⟕ spotsfull, commentsxover ⟕ commentsfull, spotstatelist
(downloads), spotteridblacklist (blocked / trusted posters). Spotweb stores message-ids without
`<>`; they are normalised. Rows go through the same validation and dedupe as a backup import, so
the run is safe to repeat or resume: what already exists is skipped. Usenet bookmarks are not
copied (article numbers are per server): the next retrieval simply starts from the usual window
and skips spots that are already here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .backup import TABLES, _coerce_row, _flush
from .library import extract_ids
from .spotnet import parse_full_xml, to_newznab
from .titles import title_key

_PIPE = re.compile(r"[a-z]\d+")


def _stamp(value: Any) -> str:
    """Unix seconds -> ISO time ('' when not a usable number)."""
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _bracket(message_id: Any) -> str:
    mid = str(message_id or "").strip().strip("<>")
    return f"<{mid}>" if mid else ""


def _subcats(row: dict) -> dict[str, list[str]]:
    return {letter: _PIPE.findall(str(row.get(f"subcat{letter}") or "")) for letter in "abcdz"}


def spot_row(src: dict) -> dict | None:
    """One joined spots/spotsfull row -> our spot dict (None if unusable)."""
    message_id = _bracket(src.get("messageid"))
    posted = _stamp(src.get("stamp"))
    if not message_id or not posted or not str(src.get("title") or "").strip():
        return None
    raw_category = src.get("category")
    if raw_category is None:
        return None
    try:
        category = int(raw_category)
    except (TypeError, ValueError):
        return None
    subs = _subcats(src)
    row: dict[str, Any] = {
        "message_id": message_id,
        "title": str(src["title"]).strip(),
        "poster": str(src.get("poster") or ""),
        "category": to_newznab(category, subs),
        "spot_category": category,
        "size": int(src.get("filesize") or 0),
        "posted_at": posted,
        "subcat_a": "".join(f"{c}|" for c in subs["a"]),
        "subcat_b": "".join(f"{c}|" for c in subs["b"]),
        "subcat_c": "".join(f"{c}|" for c in subs["c"]),
        "subcat_d": "".join(f"{c}|" for c in subs["d"]),
        "subcat_z": "".join(f"{c}|" for c in subs["z"]),
        "title_key": title_key(str(src["title"]).strip()),
        "nzb_ref": message_id,
        "description": str(src.get("tag") or "")[:500],
        "enriched": 0,
    }
    full = parse_full_xml(src["fullxml"]) if src.get("fullxml") else None
    if full is not None:
        row["description"] = full["description"][:4000]
        row["website"] = full["website"]
        if full["nzb_segments"]:
            row["nzb_segments"] = json.dumps(full["nzb_segments"])
        row["imdb_id"], row["tvmaze_id"] = extract_ids(full["website"], full["description"])
        # The cover images were not copied, so such spots are enriched again (covers only).
        row["enriched"] = 0 if full["image_segments"] or full["image_url"] else 1
    return row


def comment_row(src: dict) -> dict | None:
    message_id, spot_id = _bracket(src.get("messageid")), _bracket(src.get("nntpref"))
    posted = _stamp(src.get("stamp") or src.get("xstamp"))
    if not message_id or not spot_id or not posted:
        return None
    body = str(src.get("body") or "")
    return {
        "message_id": message_id,
        "spot_message_id": spot_id,
        "poster": str(src.get("fromhdr") or "").split("<", 1)[0].strip()[:80],
        "posted_at": posted,
        "body": body[: 10 * 1024],
        "body_state": 1 if body else 0,
    }


SPOT_SQL = """
SELECT s.id, s.messageid, s.poster, s.title, s.tag, s.category,
       s.subcata, s.subcatb, s.subcatc, s.subcatd, s.subcatz,
       s.stamp, s.filesize, f.fullxml
FROM spots s LEFT JOIN spotsfull f ON f.messageid = s.messageid
WHERE s.id > :last ORDER BY s.id LIMIT :n
"""
COMMENT_SQL = """
SELECT x.id, x.messageid, x.nntpref, x.stamp AS xstamp, c.fromhdr, c.stamp, c.body
FROM commentsxover x LEFT JOIN commentsfull c ON c.messageid = x.messageid
WHERE x.id > :last ORDER BY x.id LIMIT :n
"""


def _query(engine: Engine, sql: str, **params: Any) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


async def _pump(
    engine: Engine,
    session: AsyncSession,
    sql: str,
    name: str,
    build: Callable[[dict], dict | None],
    batch: int,
    result: dict,
    say: Callable[[str], None],
) -> None:
    model = TABLES[name][0]
    last = 0
    while True:
        rows = await asyncio.to_thread(_query, engine, sql, last=last, n=batch)
        if not rows:
            return
        last = rows[-1]["id"]
        clean = []
        for src in rows:
            built = build(src)
            checked = _coerce_row(name, model, built) if built else None
            if checked is None:
                result["skipped"] += 1
            else:
                clean.append(checked)
        await _flush(session, name, clean, result)
        say(f"  {name}: through id {last} ({result['added'].get(name, 0):,} added)")


async def run_import(
    engine: Engine,
    factory: async_sessionmaker[AsyncSession],
    batch: int = 2000,
    comments: bool = True,
    say: Callable[[str], None] = print,
) -> dict:
    result: dict = {"added": {}, "skipped": 0}
    async with factory() as session:
        say("Importing spots…")
        await _pump(engine, session, SPOT_SQL, "spots", spot_row, batch, result, say)

        say("Importing download history…")
        downloads = await asyncio.to_thread(
            _query,
            engine,
            "SELECT messageid, MIN(download) AS stamp FROM spotstatelist "
            "WHERE download IS NOT NULL GROUP BY messageid",
        )
        dl_rows = []
        for src in downloads:
            when, mid = _stamp(src["stamp"]), _bracket(src["messageid"])
            row = {
                "message_id": mid,
                "first_at": when,
                "last_at": when,
                "count": 1,
                "source": "spotweb",
            }
            checked = (
                _coerce_row("downloads", TABLES["downloads"][0], row) if when and mid else None
            )
            dl_rows.append(checked) if checked else result.__setitem__(
                "skipped", result["skipped"] + 1
            )
        for i in range(0, len(dl_rows), batch):
            await _flush(session, "downloads", dl_rows[i : i + batch], result)

        say("Importing poster block/trust lists…")
        poster_rows = []
        for src in await asyncio.to_thread(
            _query, engine, "SELECT DISTINCT spotterid, idtype FROM spotteridblacklist"
        ):
            status = {1: "blocked", 2: "trusted"}.get(src["idtype"])
            sid = re.sub(r"[^A-Za-z0-9]", "", str(src["spotterid"] or ""))
            if status and sid:
                poster_rows.append(
                    {"spotter_id": sid[:16], "status": status, "note": "imported from Spotweb"}
                )
        checked_rows = [
            r for r in (_coerce_row("spotters", TABLES["spotters"][0], p) for p in poster_rows) if r
        ]
        await _flush(session, "spotters", checked_rows, result)

        if comments:
            say("Importing comments…")
            await _pump(engine, session, COMMENT_SQL, "comments", comment_row, batch, result, say)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import a classic Spotweb database into SpotwebNG."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("SPOTWEB_DB_URL"),
        help="SQLAlchemy URL of the Spotweb database",
    )
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--skip-comments", action="store_true")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("give --url or set SPOTWEB_DB_URL")

    from .db import SessionLocal, init_db  # imported late: it opens the target database

    engine = create_engine(args.url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM spots LIMIT 1"))
    except Exception as exc:
        print(f"Cannot read a Spotweb database there: {type(exc).__name__}", file=sys.stderr)
        return 2

    async def go() -> dict:
        await init_db()
        return await run_import(engine, SessionLocal, args.batch, not args.skip_comments)

    result = asyncio.run(go())
    print(f"Done. Added: {result['added']}; skipped: {result['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
