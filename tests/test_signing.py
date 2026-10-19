"""Spotnet signatures: encoding, RSA check, spot and comment verification."""

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import signing


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=1024)


def _modulus(key) -> int:
    return key.public_key().public_numbers().n


def _sign(key, data: bytes) -> bytes:
    return key.sign(data, padding.PKCS1v15(), hashes.SHA1())


def _prepare(raw: bytes) -> str:
    """Spotnet base64: '/' -> '-s', '+' -> '-p'."""
    return base64.b64encode(raw).decode().replace("/", "-s").replace("+", "-p").rstrip("=")


def _mod_bytes(key) -> bytes:
    n = _modulus(key)
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def test_spotnet_base64_roundtrips_the_escaped_characters():
    raw = bytes([0xFB, 0xFF, 0xBF, 0x3E, 0x3F])  # encodes with both '+' and '/'
    prepared = _prepare(raw)
    assert "-p" in prepared or "-s" in prepared
    assert signing.b64decode(prepared) == raw
    assert signing.b64decode("not base64 !!") == b""


def test_spotter_id_matches_the_documented_algorithm():
    mod_b64 = "ys8WSlqonQMWT8ubG0tAA2Q07P36E+CJmb875wSR1XH7IFhEi0CCwlUzNqBFhC+P"
    import zlib

    expected = (
        base64.b64encode((zlib.crc32(base64.b64decode(mod_b64)) & 0xFFFFFFFF).to_bytes(4, "little"))
        .decode()
        .translate(str.maketrans("", "", "/+="))
    )
    assert signing.spotter_id(mod_b64) == expected and 3 <= len(expected) <= 6


def test_rsa_verify_accepts_valid_and_rejects_everything_else(key):
    n, msg = _modulus(key), b"hello spotnet"
    sig = _sign(key, msg)
    assert signing.rsa_verify_sha1(n, msg, sig)
    assert not signing.rsa_verify_sha1(n, b"tampered", sig)
    assert not signing.rsa_verify_sha1(n, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not signing.rsa_verify_sha1(n, msg, sig[1:])  # wrong length
    assert not signing.rsa_verify_sha1(n, msg, b"")
    assert not signing.rsa_verify_sha1(n + 2, msg, sig)


def test_hashcash_rule():
    n = 0
    while True:
        mid = f"abc{n}@spot.example"
        if hashlib.sha1(f"<{mid}>".encode()).hexdigest().startswith("0000"):
            break
        n += 1
    assert signing.hashcash_ok(mid) and signing.hashcash_ok(f"<{mid}>")
    assert not signing.hashcash_ok("plain@spot.example")


def _find_hashcash_id() -> str:
    n = 0
    while True:
        mid = f"hc{n}@spot.example"
        if signing.hashcash_ok(mid):
            return mid
        n += 1


def _self_signed_from(key, title: str, poster: str, mid: str, tamper: bool = False) -> str:
    header = "1701.123456.randomxx.1700000000.0.0"  # [cat][keyid][subcat].size.rand.date.cid.cval
    signed = (title + header + poster).encode("latin-1")
    sig = _prepare(_sign(key, signed))
    user = _prepare(_sign(key, f"<{mid}>".encode()))
    pub = _prepare(_mod_bytes(key))
    if tamper:
        header = header.replace("123456", "654321")
    return f"{poster} <{pub}.{user}@{header}.{sig}>"


def test_a_self_signed_spot_verifies_and_gets_a_spotter_id(key):
    mid = _find_hashcash_id()
    from_hdr = _self_signed_from(key, "Dune Part Two", "Nick", mid)
    status, keyid, sid = signing.verify_spot(from_hdr, "Dune Part Two", mid)
    assert (status, keyid) == (signing.VERIFIED, 7) and 3 <= len(sid) <= 6
    assert sid == signing.spotter_id(
        _prepare(_mod_bytes(key)).replace("-s", "/").replace("-p", "+")
    )


@pytest.mark.parametrize("what", ["title", "header", "poster_swap", "message_id"])
def test_tampering_with_any_signed_part_fails_verification(key, what):
    mid = _find_hashcash_id()
    from_hdr = _self_signed_from(key, "Dune Part Two", "Nick", mid)
    title = "Dune Part Two"
    if what == "title":
        title = "Dune Part Three"
    elif what == "header":
        from_hdr = _self_signed_from(key, title, "Nick", mid, tamper=True)
    elif what == "poster_swap":
        from_hdr = from_hdr.replace("Nick <", "Mallory <")
    elif what == "message_id":
        mid = _find_hashcash_id().replace("hc", "hd")  # also fails hashcash
    status, _, sid = signing.verify_spot(from_hdr, title, mid)
    assert status == signing.FAILED and sid == ""


def test_other_key_ids_and_malformed_headers_are_unsigned_not_failed():
    assert signing.verify_spot("Nick <x@1501.1.r.1.0.0.sig>", "T", "a@b")[0] == signing.UNSIGNED
    assert signing.verify_spot("garbage", "T", "a@b")[0] == signing.UNSIGNED
    assert (
        signing.verify_spot("Nick <x@1701.1.r.1.0.0.>", "T", "a@b")[0] == signing.UNSIGNED
    )  # no signature


def test_comments_verify_on_the_message_id_or_the_body_layout(key):
    mid = "spot1.5.abc.def@spot.example"
    pub = base64.b64encode(_mod_bytes(key)).decode()
    sig_id = _prepare(_sign(key, f"<{mid}>".encode()))
    assert signing.verify_comment(mid, sig_id, pub, "AQAB", "any body", "Al")[0] == signing.VERIFIED
    body = "line one\nline two"
    sig_body = _prepare(_sign(key, (f"<{mid}>" + "line one\r\nline two" + "\r\n" + "Al").encode()))
    assert signing.verify_comment(mid, sig_body, pub, "AQAB", body, "Al")[0] == signing.VERIFIED
    assert signing.verify_comment(mid, sig_body, pub, "AQAB", "edited", "Al")[0] == signing.FAILED
    assert signing.verify_comment(mid, "", pub, "AQAB", body, "Al")[0] == signing.UNSIGNED


# ---- integration: ingest, stored raw material, the re-verification job, blocking ----

from datetime import UTC, datetime, timedelta  # noqa: E402

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import jobs, spotters  # noqa: E402
from app.auth import ADMIN, create_user  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiUser, Base, Setting, Spot  # noqa: E402
from app.spotnet import parse_xover_row  # noqa: E402
from app.spots import search_spots  # noqa: E402


def _xover(key, title="Dune Part Two", nick="Nick", tamper=False):
    mid = _find_hashcash_id()
    from_hdr = _self_signed_from(key, title, nick, mid, tamper=tamper)
    return {
        "Number": 1,
        "Subject": f"{title} | tag",
        "From": from_hdr,
        "Date": "Fri, 24 Jul 2026 10:00:00 +0200",
        "Message-ID": f"<{mid}>",
    }, mid


def test_ingest_verifies_and_keeps_the_raw_header(key):
    row, mid = _xover(key)
    spot = parse_xover_row(row)
    assert spot.verified == signing.VERIFIED and spot.spotter_id and spot.from_hdr == row["From"]
    forged, _ = _xover(key, tamper=True)
    assert parse_xover_row(forged).verified == signing.FAILED


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_the_job_verifies_rows_stored_with_a_raw_header_but_never_evaluated(factory, key):
    row, mid = _xover(key)
    good = parse_xover_row(row)
    async with factory() as s:
        s.add(
            Spot(
                message_id=good.message_id,
                title=good.title,
                posted_at=good.stamp,
                from_hdr=good.from_hdr,
                verified=0,
            )
        )  # as if stored by an older version
        s.add(
            Spot(message_id="<old@x>", title="Old", posted_at=good.stamp)
        )  # no raw header: untouched
        await s.commit()
    await jobs.run_jobs(factory, pause=0)
    async with factory() as s:
        rows = {r.message_id: r for r in (await s.execute(select(Spot))).scalars().all()}
    assert rows[good.message_id].verified == signing.VERIFIED and rows[good.message_id].spotter_id
    assert rows["<old@x>"].verified == 0


async def test_blocked_posters_vanish_from_search_and_counts_and_failed_can_be_hidden(factory):
    when = datetime(2026, 7, 1, tzinfo=UTC)
    async with factory() as s:
        s.add(
            Spot(message_id="<a@x>", title="Good", posted_at=when, spotter_id="GOODID", verified=1)
        )
        s.add(
            Spot(
                message_id="<b@x>",
                title="Spam",
                posted_at=when - timedelta(days=1),
                spotter_id="SPAMID",
                verified=1,
            )
        )
        s.add(
            Spot(message_id="<c@x>", title="Forged", posted_at=when - timedelta(days=2), verified=2)
        )
        s.add(Spot(message_id="<d@x>", title="Anon", posted_at=when - timedelta(days=3)))
        await s.commit()
        await spotters.set_status(s, "SPAMID", "blocked", "spammer")
        rows, total = await search_spots(s)
        assert {r.title for r in rows} == {"Good", "Forged", "Anon"} and total == 3
        rows, _ = await search_spots(s, hide_failed=True)
        assert {r.title for r in rows} == {"Good", "Anon"}  # unsigned stays visible
        await spotters.set_status(s, "SPAMID", "neutral")
        assert (await search_spots(s))[1] == 4
        with pytest.raises(ValueError):
            await spotters.set_status(s, "X" * 40, "blocked")
        with pytest.raises(ValueError):
            await spotters.set_status(s, "ID", "banned")


async def test_blocking_through_the_web_ui_is_admin_only_and_hides_the_spots():
    override = app.dependency_overrides[get_session]
    async for session in override():
        maker = async_sessionmaker(session.bind, expire_on_commit=False)
        break
    async with maker() as s:
        s.add(ApiUser(name="d", api_key="k" * 32))
        s.add(Setting(key="provider_host", value="news.invalid"))
        s.add(
            Spot(
                message_id="<z@x>",
                title="Spammy Spot",
                posted_at=datetime(2026, 7, 1, tzinfo=UTC),
                spotter_id="SPAMID",
                verified=1,
                subcat_z="z0|",
                spot_category=0,
                enriched=1,
            )
        )
        await create_user(s, "boss", "pw-boss-12345", ADMIN)
        await create_user(s, "view", "pw-view-12345", "viewer")
        await s.commit()
        spot_id = (await s.execute(select(Spot.id))).scalar_one()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/login", data={"username": "view", "password": "pw-view-12345"})
        denied = await c.post("/spotter/SPAMID/status", data={"status": "blocked"})
        assert denied.status_code == 403
        await c.post("/logout")
        await c.post("/login", data={"username": "boss", "password": "pw-boss-12345"})
        page = (await c.get(f"/spot/{spot_id}")).text
        assert "Block poster" in page and "✓ verified" in page
        assert "Spammy Spot" in (await c.get("/")).text
        ok = await c.post("/spotter/SPAMID/status", data={"status": "blocked", "note": "spam"})
        assert ok.json()["ok"] is True
        assert "Spammy Spot" not in (await c.get("/")).text
        assert "Spammy Spot" not in (await c.get(f"/api?t=search&q=Spammy&apikey={'k' * 32}")).text
        assert (
            "SPAMID" in (await c.get("/settings")).text
            and "spam" in (await c.get("/settings")).text
        )
        assert "blocked" in (await c.get(f"/spot/{spot_id}")).text  # still reachable to unblock
        bad = await c.post("/spotter/SPAMID/status", data={"status": "banned"})
        assert bad.status_code == 400
