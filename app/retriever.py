"""Incremental spot retrieval from the Spotnet header group (port of Spotweb's
Services_Retriever_Spots, reduced: no comments/reports/full-XML yet).

Reads XOVER ranges from `free.pt`, parses each row with app.spotnet, and stores
new spots. The last processed article number + message-id are kept in
`usenet_state`; on the next run the article number is re-validated by
message-id (article renumbering protection) before continuing from it.
"""

from __future__ import annotations

import asyncio
import json
import ssl as ssl_module
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting, Spot, UsenetState
from .spotnet import decode_spot_binary, parse_full_xml, parse_xover_row, safe_website


def pack_subcats(codes: list[str], limit: int) -> str:
    """Pipe-terminated codes truncated on a code boundary — slicing the string
    mid-code turned 'd33|' into 'd3', silently changing a spot's genre."""
    out = ""
    for code in codes:
        token = f"{code}|"
        if len(out) + len(token) > limit:
            break
        out += token
    return out


# One retrieval at a time, whichever entry point starts it: the background loop
# and POST /retrieve previously raced and hit UNIQUE violations.
RETRIEVE_LOCK = asyncio.Lock()

HDR_GROUP = "free.pt"
INCREMENT = 5000
DEFAULT_BACKFILL = 20000  # first run: only this many most-recent articles
XOVER_FIELDS = (
    "Number",
    "Subject",
    "From",
    "Date",
    "Message-ID",
    "References",
    ":bytes",
    ":lines",
)


class NntpError(Exception):
    pass


class NntpClient:
    """Minimal async NNTP reader: auth, GROUP, XOVER, XHDR Message-ID."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(
        self, host: str, port: int, use_ssl: bool, username: str, password: str
    ) -> None:
        ssl_ctx = ssl_module.create_default_context() if use_ssl else None
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx), self.timeout
        )
        greeting = await self._line()
        if not greeting.startswith(("200", "201")):
            raise NntpError(f"Bad greeting: {greeting}")
        if username:
            if any(ch in username + password for ch in ("\r", "\n", "\0")):
                raise NntpError("Stored credentials contain line breaks")
            reply = await self.command(f"AUTHINFO USER {username}")
            if reply.startswith("381"):
                reply = await self.command(f"AUTHINFO PASS {password}")
            if not reply.startswith("281"):
                raise NntpError(f"Authentication failed: {reply}")

    async def _line(self) -> str:
        assert self.reader is not None, "connect() must run first"
        raw = await asyncio.wait_for(self.reader.readline(), self.timeout)
        if not raw:
            raise NntpError("Connection closed by server")
        return raw.decode("latin-1").rstrip("\r\n")

    async def command(self, line: str) -> str:
        assert self.writer is not None, "connect() must run first"
        self.writer.write(line.encode("latin-1") + b"\r\n")
        await self.writer.drain()
        return await self._line()

    async def multiline(self, cmd: str, expect: str) -> list[str]:
        reply = await self.command(cmd)
        if not reply.startswith(expect):
            raise NntpError(f"{cmd.split(' ')[0]} failed: {reply}")
        lines: list[str] = []
        while True:
            line = await self._line()
            if line == ".":
                return lines
            lines.append(line[1:] if line.startswith("..") else line)

    async def group(self, name: str) -> tuple[int, int, int]:
        reply = await self.command(f"GROUP {name}")
        if not reply.startswith("211"):
            raise NntpError(f"GROUP {name} failed: {reply}")
        _, count, first, last, *_ = reply.split(" ")
        return int(count), int(first), int(last)

    async def xover(self, start: int, end: int) -> list[dict]:
        rows = []
        for line in await self.multiline(f"XOVER {start}-{end}", "224"):
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            row: dict[str, object] = dict(zip(XOVER_FIELDS, parts, strict=False))
            try:
                row["Number"] = int(str(row["Number"]))
            except ValueError:
                continue
            rows.append(row)
        return rows

    async def xhdr_message_id(self, article: int) -> str | None:
        try:
            lines = await self.multiline(f"XHDR MESSAGE-ID {article}", "221")
        except NntpError:
            return None
        for line in lines:
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0] == str(article):
                return parts[1].strip()
        return None

    async def quit(self) -> None:
        try:
            await self.command("QUIT")
        except (TimeoutError, NntpError, OSError):
            pass
        finally:
            if self.writer:
                self.writer.close()


async def _load_provider(session: AsyncSession) -> dict:
    settings = {s.key: s.value for s in (await session.execute(select(Setting))).scalars().all()}
    if not settings.get("provider_host"):
        raise ValueError("No Usenet provider configured — complete the setup wizard first")
    return settings


async def retrieve_spots(session: AsyncSession, max_backfill: int = DEFAULT_BACKFILL) -> dict:
    """Fetch new spot headers and store them. Returns a summary dict."""
    provider = await _load_provider(session)
    client = NntpClient()
    await client.connect(
        provider["provider_host"],
        int(provider.get("provider_port", 119)),
        provider.get("provider_ssl", "yes") == "yes",
        provider.get("username", ""),
        provider.get("password", ""),
    )
    try:
        _, first, last = await client.group(HDR_GROUP)

        state = await session.get(UsenetState, "spots")
        start = max(first, last - max_backfill)
        if state and state.cur_article:
            # Renumber protection: trust the bookmark only if the article
            # number still carries the same message-id.
            msgid = await client.xhdr_message_id(state.cur_article)
            if msgid and msgid == state.cur_message_id:
                start = state.cur_article + 1
        if state is None:
            state = UsenetState(infotype="spots", cur_article=0, cur_message_id="")
            session.add(state)

        scanned = added = invalid = backfilled = 0
        while start <= last:
            end = min(start + INCREMENT - 1, last)
            rows = await client.xover(start, end)
            scanned += len(rows)

            spots = []
            for row in rows:
                # Belt and braces: a parser crash here would escape before the
                # commit below and freeze the bookmark permanently.
                try:
                    spot = parse_xover_row(row)
                except Exception:
                    spot = None
                if spot is None:
                    invalid += 1
                else:
                    spots.append(spot)

            if spots:
                ids = [s.message_id for s in spots]
                existing = {
                    row.message_id: row
                    for row in (await session.execute(select(Spot).where(Spot.message_id.in_(ids))))
                    .scalars()
                    .all()
                }
                seen_batch: set[str] = set()
                for s in spots:
                    if s.message_id in seen_batch:
                        continue
                    seen_batch.add(s.message_id)
                    known = existing.get(s.message_id)
                    if known is not None:
                        # Rows stored before subcats existed: fill them in place,
                        # leaving enrichment (images/NZB) untouched.
                        if not known.subcat_z and not known.subcat_a:
                            known.subcat_a = pack_subcats(s.subcats.get("a", []), 64)
                            known.subcat_b = pack_subcats(s.subcats.get("b", []), 64)
                            known.subcat_c = pack_subcats(s.subcats.get("c", []), 64)
                            known.subcat_d = pack_subcats(s.subcats.get("d", []), 128)
                            known.subcat_z = pack_subcats(s.subcats.get("z", []), 32)
                            known.spot_category = s.category
                            backfilled += 1
                        continue

                    def _sub(letter: str, limit: int, spot=s) -> str:
                        # bind the current spot: a late-binding closure here
                        # would pack the last spot's subcats into every row
                        return pack_subcats(spot.subcats.get(letter) or [], limit)

                    session.add(
                        Spot(
                            message_id=s.message_id[:255],
                            title=s.title[:512],
                            poster=s.poster[:255],
                            category=s.newznab_cat,
                            spot_category=s.category,
                            subcat_a=_sub("a", 64),
                            subcat_b=_sub("b", 64),
                            subcat_c=_sub("c", 64),
                            subcat_d=_sub("d", 128),
                            subcat_z=_sub("z", 32),
                            size=s.filesize,
                            posted_at=s.stamp,
                            description=s.tag[:500] if s.tag else "",
                            nzb_ref=s.message_id[:255],
                        )
                    )
                    added += 1

            if rows:
                state.cur_article = rows[-1]["Number"]
                state.cur_message_id = rows[-1].get("Message-ID", "").strip()
                start = rows[-1]["Number"] + 1
            else:
                start = end + 1
            await session.commit()

        return {
            "added": added,
            "backfilled": backfilled,
            "scanned": scanned,
            "invalid": invalid,
            "group_first": first,
            "group_last": last,
        }
    finally:
        await client.quit()


# ---------- full-spot enrichment (X-XML headers, images, NZB binaries) ----------


async def _client_head(client: NntpClient, message_id: str) -> list[str] | None:
    try:
        return await client.multiline(f"HEAD {message_id}", "221")
    except NntpError:
        return None


def _extract_xml(head_lines: list[str]) -> str:
    return "".join(line[7:] for line in head_lines if line.lower().startswith("x-xml:")).strip()


async def read_full_spot(client: NntpClient, message_id: str) -> dict | None:
    """Fetch HEAD by message-id and parse the concatenated X-XML headers."""
    head = await _client_head(client, message_id)
    if not head:
        return None
    xml = _extract_xml(head)
    if not xml or len(xml) > 50 * 1024:  # Spotweb rejects oversized spots
        return None
    return parse_full_xml(xml)


MAX_SEGMENTS = 64
MAX_BINARY_BYTES = 12 * 1024 * 1024


async def fetch_binary(client: NntpClient, segments: list[str], compressed: bool) -> bytes | None:
    """Fetch and decode a spot binary spread over segment articles (NZB/image).

    Segment count and total size are attacker-controlled, so both are capped.
    """
    if len(segments) > MAX_SEGMENTS:
        return None
    parts: list[bytes] = []
    total = 0
    for seg in segments[:MAX_SEGMENTS]:
        try:
            lines = await client.multiline(f"BODY <{seg}>", "222")
        except NntpError:
            return None
        chunk = "".join(lines).encode("latin-1")
        total += len(chunk)
        if total > MAX_BINARY_BYTES:
            return None
        parts.append(chunk)
    blob = b"".join(parts)
    try:
        return decode_spot_binary(blob, compressed)
    except Exception:
        return None


async def enrich_new_spots(
    session: AsyncSession, images_dir, limit: int = 50, only_id: int | None = None
) -> dict:
    """Enrich pending spots (newest first) with description, website, NZB
    segment list, and the spot's own image saved to images_dir."""
    provider = await _load_provider(session)
    stmt = select(Spot).where(Spot.enriched == 0).order_by(Spot.posted_at.desc()).limit(limit)
    if only_id is not None:
        stmt = select(Spot).where(Spot.id == only_id, Spot.enriched == 0)
    pending = (await session.execute(stmt)).scalars().all()
    if not pending:
        return {"enriched": 0, "failed": 0}

    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)

    client = NntpClient()
    await client.connect(
        provider["provider_host"],
        int(provider.get("provider_port", 119)),
        provider.get("provider_ssl", "yes") == "yes",
        provider.get("username", ""),
        provider.get("password", ""),
    )
    enriched = failed = 0
    try:
        for spot in pending:
            try:
                full = await read_full_spot(client, spot.message_id)
            except Exception:
                # A dropped connection or an oversized header must not discard
                # the whole batch — mark nothing, retry this spot next round.
                await session.commit()
                break
            if full is None:
                spot.enriched = 2
                failed += 1
                await session.commit()
                continue

            if full["description"]:
                spot.description = full["description"][:4000]
            spot.website = safe_website(full["website"])[:1024]
            if full["nzb_segments"]:
                spot.nzb_segments = json.dumps(full["nzb_segments"])

            if full["image_segments"]:
                try:
                    image = await fetch_binary(client, full["image_segments"], compressed=False)
                except Exception:
                    image = None
                if image:
                    (images_dir / f"{spot.id}.jpg").write_bytes(image)
                    spot.poster_url = f"/image/{spot.id}"
            elif full["image_url"]:
                spot.poster_url = full["image_url"][:1024]

            spot.enriched = 1
            enriched += 1
            # Commit per spot: a later failure then costs one spot, not fifty,
            # and never orphans an image that was already written to disk.
            await session.commit()
    finally:
        await client.quit()
    return {"enriched": enriched, "failed": failed}
