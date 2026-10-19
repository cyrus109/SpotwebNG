"""Spotnet comments (read-only): headers from the comments group, bodies on demand.

A comment's message-id is `<spotlocal.rating.x.y@domain>`; the spot it belongs
to is `<spotlocal@domain>`. Spotweb derives the link from the id rather than the
References header because servers pass References unreliably, and so do we.

Headers come from XOVER like spots. Bodies cost one request each, so they are
fetched only when someone opens the spot. Signatures are not verified: every
comment is shown as unverified, and all text is untrusted (escaped by Jinja,
control characters stripped, size capped).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Comment, UsenetState
from .retriever import INCREMENT, NntpClient, NntpError, NntpTooLarge, _load_provider
from .signing import verify_comment

MAX_BODY = 10 * 1024
MAX_BODY_WIRE = (
    256 * 1024
)  # read budget; anything bigger is not a comment  # Spotweb truncates the same way: some "comments" are mis-posted NZBs
MAX_POSTER = 80
GROUP_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")  # goes into a GROUP command line
_MSGID_RE = re.compile(r"^<([^\s<>@]+)@([^\s<>@]+)>$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str, limit: int) -> str:
    return _CONTROL_RE.sub("", text)[:limit]


def parse_comment_row(row: dict) -> dict | None:
    """XOVER row -> comment header dict, or None if it isn't a usable comment."""
    message_id = str(row.get("Message-ID", "")).strip()
    match = _MSGID_RE.match(message_id)
    if not match or len(message_id) > 255:
        return None
    parts = match.group(1).split(".")
    if len(parts) < 2 or not parts[0]:
        return None
    try:
        parsed = parsedate_to_datetime(str(row["Date"]))
    except (KeyError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:  # '-0000' comes back naive
        parsed = parsed.replace(tzinfo=UTC)
    stamp = min(parsed.astimezone(UTC), datetime.now(UTC))
    poster = _clean(str(row.get("From", "")).split("<", 1)[0].strip(), MAX_POSTER)
    return {
        "message_id": message_id,
        "spot_message_id": f"<{parts[0]}@{match.group(2)}>",
        "poster": poster,
        "posted_at": stamp,
    }


async def retrieve_comments(
    session: AsyncSession,
    group: str,
    max_backfill: int,
    retrieve_all: bool = False,
    min_posted: datetime | None = None,
    max_age_days: int = 0,
) -> dict:
    """Fetch new comment headers. Forward only: the first run starts `max_backfill`
    articles back (or at the group's first article with `retrieve_all`)."""
    if max_age_days:  # same day limit as the spot history
        age_cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        min_posted = age_cutoff if min_posted is None else max(min_posted, age_cutoff)
    if not GROUP_RE.match(group):
        raise ValueError(f"Invalid comments group name: {group!r}")
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
        _, first, last = await client.group(group)
        state = await session.get(UsenetState, "comments")
        start = first if retrieve_all else max(first, last - max_backfill)
        if state and state.cur_article:
            msgid = await client.xhdr_message_id(state.cur_article)  # renumber protection
            if msgid and msgid == state.cur_message_id:
                start = state.cur_article + 1
        if state is None:
            state = UsenetState(infotype="comments", cur_article=0, cur_message_id="")
            session.add(state)

        scanned = added = invalid = 0
        while start <= last:
            end = min(start + INCREMENT - 1, last)
            rows = await client.xover(start, end)
            scanned += len(rows)

            parsed = []
            for row in rows:
                comment = parse_comment_row(row)
                if comment is None:
                    invalid += 1
                elif min_posted is None or comment["posted_at"] >= min_posted:
                    parsed.append(comment)
            if parsed:
                ids = [c["message_id"] for c in parsed]
                known = set(
                    (
                        await session.execute(
                            select(Comment.message_id).where(Comment.message_id.in_(ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                seen: set[str] = set()
                for c in parsed:
                    if c["message_id"] in known or c["message_id"] in seen:
                        continue
                    seen.add(c["message_id"])
                    session.add(Comment(**c))
                    added += 1

            if rows:
                state.cur_article = rows[-1]["Number"]
                state.cur_message_id = str(rows[-1].get("Message-ID", "")).strip()
                start = rows[-1]["Number"] + 1
            else:
                start = end + 1
            await session.commit()
        return {"added": added, "scanned": scanned, "invalid": invalid, "group_last": last}
    finally:
        await client.quit()


def _split_article(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Headers (lower-cased names) and body lines of a raw article."""
    head: dict[str, str] = {}
    for i, line in enumerate(lines):
        if line == "":
            return head, lines[i + 1 :]
        name, _, value = line.partition(":")
        head.setdefault(name.strip().lower(), value.strip())
    return head, []


def _latin1_name(raw_from: str) -> str:
    """Poster name as Spotweb signs it: the UTF-8 text re-encoded to ISO-8859-1."""
    name = raw_from.split("<", 1)[0].strip()
    try:
        return name.encode("latin-1").decode("utf-8").encode("latin-1", "replace").decode("latin-1")
    except UnicodeError:
        return name


def _verify(comment: Comment, head: dict[str, str], raw_body: str) -> tuple[int, str]:
    key = head.get("x-user-key", "")
    modulus = re.search(r"<Modulus>([^<]+)</Modulus>", key)
    exponent = re.search(r"<Exponent>([^<]+)</Exponent>", key)
    return verify_comment(
        comment.message_id,
        head.get("x-user-signature", ""),
        modulus.group(1).strip() if modulus else "",
        exponent.group(1).strip() if exponent else "AQAB",
        raw_body,
        _latin1_name(head.get("from", "")),
    )


async def fetch_comment_bodies(session: AsyncSession, spot_message_id: str, limit: int = 50) -> int:
    """Fetch bodies for a spot's pending comments (state 0). Returns how many were stored."""
    pending = (
        (
            await session.execute(
                select(Comment)
                .where(Comment.spot_message_id == spot_message_id, Comment.body_state == 0)
                .order_by(Comment.posted_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return 0
    provider = await _load_provider(session)
    client = NntpClient()
    await client.connect(
        provider["provider_host"],
        int(provider.get("provider_port", 119)),
        provider.get("provider_ssl", "yes") == "yes",
        provider.get("username", ""),
        provider.get("password", ""),
    )
    stored = 0
    try:
        for comment in pending:
            try:
                lines = await client.multiline(
                    f"ARTICLE {comment.message_id}", "220", max_bytes=MAX_BODY_WIRE
                )
            except NntpTooLarge:
                comment.body_state = 2  # a mis-posted binary; the stream is desynced, stop here
                await session.commit()
                break
            except NntpError:
                comment.body_state = 2  # gone from the server: don't ask again
                await session.commit()
                continue
            except (OSError, TimeoutError):
                break  # connection trouble: retry on the next view
            head, body_lines = _split_article(lines)
            raw_body = "\n".join(body_lines)  # still latin-1-decoded raw bytes: what was signed
            raw = raw_body.encode("latin-1", "replace").decode("utf-8", "replace")
            comment.body = _CONTROL_RE.sub("", raw)[:MAX_BODY]
            comment.body_state = 1
            comment.verified, comment.spotter_id = _verify(comment, head, raw_body)
            stored += 1
            await session.commit()
    finally:
        await client.quit()
    return stored


async def comment_counts(session: AsyncSession, spot_message_ids: list[str]) -> dict[str, int]:
    if not spot_message_ids:
        return {}
    rows = await session.execute(
        select(Comment.spot_message_id, func.count())
        .where(Comment.spot_message_id.in_(spot_message_ids))
        .group_by(Comment.spot_message_id)
    )
    return {mid: n for mid, n in rows.all()}
