"""Spotnet wire-format parsing — Python port of Spotweb's Services_Format_Parsing.

A Spotnet spot is a Usenet post whose metadata is packed into the From header:

    From: [Nick] <[KEY]@[CAT][KEY-ID][SUBCATS].[SIZE].[RANDOM].[DATE].[CUSTOM-ID].[CUSTOM-VALUE].[SIGNATURE]>

CAT is 1-based on the wire (1=Image 2=Sound 3=Games 4=Applications), internal
representation is 0-based, matching Spotweb. Subcats are letter+digit runs like
`a09b04c02d05` (letters a/b/c/d/z, leading zeros stripped). The posting date is
taken from the XOVER Date header, not the From header.
"""

from __future__ import annotations

import base64
import hashlib
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from .signing import verify_spot

VALID_SUBCAT_LETTERS = frozenset("abcdz")

# Longest run of digits we will ever convert. Guards both int()'s 4300-digit
# limit and pathological subcat strings.
_MAX_DIGITS = 12

# Ceiling for inflated NZB payloads.
MAX_INFLATED_BYTES = 32 * 1024 * 1024


def safe_int(text: str) -> int | None:
    """int() for untrusted wire data. `.isdigit()` accepts '²' and Arabic-Indic
    digits that int() rejects, so ASCII is required explicitly."""
    text = text.strip()
    if not text or len(text) > _MAX_DIGITS or not text.isascii() or not text.isdigit():
        return None
    return int(text)


def safe_website(url: str) -> str:
    """Only http(s) links may reach an href — a spot's <Website> is attacker
    controlled, and `javascript:` there is stored XSS."""
    candidate = (url or "").strip()
    if not candidate:
        return ""
    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://")):
        return candidate
    return ""


# Erotica genre codes for cat0/d (Spotweb createSubcatZ + deprecated 23-26).
_EROTICA_D = {f"d{n}" for n in (23, 24, 25, 26, *range(72, 90))}


def unprepare_base64(value: str) -> str:
    """Spotnet's URL-ish base64: '-s'→'/', '-p'→'+', repad to multiple of 4."""
    padding = len(value) % 4
    if padding:
        value += "=" * (4 - padding)
    return value.replace("-s", "/").replace("-p", "+")


def calculate_spotter_id(user_key_b64: str) -> str:
    """crc32 of the decoded key, packed little-endian, base64, '/+=' stripped."""
    try:
        crc = zlib.crc32(base64.b64decode(user_key_b64 + "==")) & 0xFFFFFFFF
    except (ValueError, binascii_error):
        return ""
    packed = bytes([crc & 0xFF, (crc >> 8) & 0xFF, (crc >> 16) & 0xFF, (crc >> 24) & 0xFF])
    return base64.b64encode(packed).decode().translate(str.maketrans("", "", "/+="))


binascii_error = Exception  # binascii.Error subclasses ValueError; keep broad


def _parse_subcat_string(raw: str) -> dict[str, list[str]]:
    """Scan `a09b04c02d05`-style runs into {'a': ['a9'], 'b': ['b4'], ...}."""
    subcats: dict[str, list[str]] = {letter: [] for letter in "abcdz"}
    build = ""
    for ch in raw.lower() + "!":
        if not ch.isdigit() and build:
            letter, digits = build[0], build[1:]
            number = safe_int(digits)
            if letter in VALID_SUBCAT_LETTERS and number is not None:
                subcats[letter].append(f"{letter}{number}")
            build = ""
        build += ch
    return subcats


def parse_from_header(from_hdr: str) -> dict | None:
    """Unpack the Spotnet From header; None when this is not a valid spot."""
    if "<" not in from_hdr:
        return None
    poster, _, addr = from_hdr.partition("<")
    addr = addr.rstrip(">").strip()
    if "@" not in addr:
        return None
    local, _, domain = addr.partition("@")
    fields = domain.split(".")
    if len(fields) < 6:
        return None

    head = fields[0]
    if len(head) < 2:
        return None
    wire_cat, keyid = safe_int(head[0]), safe_int(head[1])
    filesize = safe_int(fields[1])
    if wire_cat is None or keyid is None or filesize is None:
        return None
    category = wire_cat - 1
    if not 0 <= category <= 3:
        return None

    return {
        "poster": poster.strip(),
        "category": category,
        "keyid": keyid,
        "subcats": _parse_subcat_string(head[2:]),
        "filesize": filesize,
        "headersign": fields[-1],
        "selfsignedpubkey": unprepare_base64(local.split(".")[0]),
    }


def split_subject(subject: str, keyid: int) -> tuple[str, str]:
    """Title | tag split. FTD-era spots (keyid 1) put the tag last instead."""
    if "=?" in subject:
        try:
            subject = str(make_header(decode_header(subject)))
        except Exception:
            pass
    if "|" not in subject:
        return subject.strip(), ""
    if keyid == 1:
        *title_parts, tag = subject.split("|")
        return "|".join(title_parts).strip(), tag.strip()
    title, _, tag = subject.partition("|")
    return title.strip(), tag.strip()


def create_subcat_z(category: int, subcats: dict[str, list[str]]) -> str:
    """Derive the z 'type' subcat when the poster omitted it (Spotweb rules)."""
    if category == 1:
        return "z0"
    if category != 0:
        return ""
    combined = set(subcats.get("a", [])) | set(subcats.get("b", [])) | set(subcats.get("d", []))
    if combined & _EROTICA_D:
        return "z3"
    if combined & {"b4", "d11"}:
        return "z1"
    if combined & {"a5", "a11"}:
        return "z2"
    if combined & {"a12", "a13"}:
        return "z4"
    return "z0"


def to_newznab(category: int, subcats: dict[str, list[str]]) -> int:
    """Map a Spotnet category+subcats to our Newznab category ids."""
    a = set(subcats.get("a", []))
    z = subcats.get("z", []) or [create_subcat_z(category, subcats)]
    ztype = z[0] if z else "z0"

    if category == 0:  # Image
        if ztype == "z1":  # series
            if "a15" in a:
                return 5045
            if a & {"a4", "a6", "a7", "a8", "a9"}:
                return 5040
            return 5030
        if ztype == "z2":
            return 7020
        if ztype == "z3":  # erotica
            return 6040 if a & {"a4", "a6", "a7", "a8", "a9", "a15"} else 6030
        if ztype == "z4":
            return 8000
        # movie
        if "a15" in a:
            return 2045
        if "a6" in a:
            return 2050
        if "a14" in a:
            return 2060
        if a & {"a4", "a7", "a8", "a9"}:
            return 2040
        return 2030
    if category == 1:  # Sound
        if a & {"a2", "a4", "a7", "a8"}:  # WAV/EAC/APE/FLAC
            return 3040
        return 3010
    if category == 2:  # Games (all platforms)
        return 4050
    if category == 3:  # Applications: a1=Mac, a4=WinPhone a6=iOS a7=Android
        if "a1" in a:
            return 4030
        if a & {"a4", "a6", "a7"}:
            return 4040
        return 4020
    return 8000


@dataclass
class SpotHeader:
    """A spot parsed from one XOVER row — everything except the full XML."""

    message_id: str
    article_number: int
    title: str
    tag: str
    poster: str
    category: int
    subcats: dict[str, list[str]] = field(default_factory=dict)
    filesize: int = 0
    stamp: datetime | None = None
    keyid: int = 0
    newznab_cat: int = 8000
    # Raw material kept so signatures can be re-checked later without Usenet.
    from_hdr: str = ""
    verified: int = 0  # signing.VERIFIED / FAILED / UNSIGNED
    spotter_id: str = ""


def parse_xover_row(row: dict) -> SpotHeader | None:
    """Turn one XOVER overview row into a SpotHeader; None if not a valid spot.

    RSA signature verification is intentionally deferred (Spotweb uses it as a
    forgery filter only); the keyid-7 hashcash check is kept because it is cheap.
    """
    header = parse_from_header(row.get("From", ""))
    if header is None:
        return None

    message_id = row.get("Message-ID", "").strip().strip("<>")
    if not message_id:
        return None
    bracketed = f"<{message_id}>"

    if header["keyid"] == 7 and not hashlib.sha1(bracketed.encode()).hexdigest().startswith("0000"):
        return None

    title, tag = split_subject(row.get("Subject", ""), header["keyid"])
    if not title:
        return None
    # keyid-2 moderation commands (delete/dispose/remove <msgid>) are not content
    if header["keyid"] == 2 and title.split(" ")[0].lower() in (
        "delete",
        "dispose",
        "remove",
    ):
        return None

    try:
        parsed = parsedate_to_datetime(row["Date"])
    except (KeyError, TypeError, ValueError):
        return None
    # '-0000' is legal and common on Usenet; email.utils returns it naive, and
    # astimezone() would then read it as the server's local time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    stamp = parsed.astimezone(UTC)
    now = datetime.now(UTC)
    if stamp > now:
        stamp = now

    subcats = header["subcats"]
    if not subcats.get("z"):
        z = create_subcat_z(header["category"], subcats)
        if z:
            subcats["z"] = [z]

    status, _, sid = verify_spot(row.get("From", ""), title, message_id)
    return SpotHeader(
        from_hdr=row.get("From", "")[:700],
        verified=status,
        spotter_id=sid,
        message_id=bracketed,
        article_number=int(row.get("Number", 0)),
        title=title,
        tag=tag,
        poster=header["poster"],
        category=header["category"],
        subcats=subcats,
        filesize=header["filesize"],
        stamp=stamp,
        keyid=header["keyid"],
        newznab_cat=to_newznab(header["category"], subcats),
    )


# ---------- full-spot XML (from the article's X-XML headers) ----------

_CTRL_CHARS = dict.fromkeys(range(0x20))
for _keep in (0x09, 0x0A, 0x0D):
    _CTRL_CHARS.pop(_keep, None)


def _valid_message_id(msgid: str) -> bool:
    return bool(msgid) and "<" not in msgid and ">" not in msgid


def parse_full_xml(xml_text: str) -> dict | None:
    """Parse the <Spotnet><Posting> XML; None when unparseable.

    Returns {description, website, nzb_segments, image_segments, image_url,
    image_width, image_height}. Any invalid NZB segment id voids the whole
    segment list (Spotweb rule).
    """
    import xml.etree.ElementTree as ET

    cleaned = xml_text.translate(_CTRL_CHARS)
    # known wire corruption Spotweb also patches
    cleaned = cleaned.replace("spot.net></Segment>", "spot.net</Segment>").replace(
        "spot.ne</Segment>", "spot.net</Segment>"
    )
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return None
    posting = root.find("Posting")
    if posting is None:
        return None

    def _text(tag: str) -> str:
        el = posting.find(tag)
        return (el.text or "").strip() if el is not None else ""

    nzb_segments = [(seg.text or "").strip() for seg in posting.findall("NZB/Segment")]
    if any(not _valid_message_id(s) for s in nzb_segments):
        nzb_segments = []

    image_url = ""
    image_segments: list[str] = []
    width = height = 0
    image = posting.find("Image")
    if image is not None:
        width = int(image.get("Width", 0) or 0)
        height = int(image.get("Height", 0) or 0)
        image_segments = [(seg.text or "").strip() for seg in image.findall("Segment")]
        if any(not _valid_message_id(s) for s in image_segments):
            image_segments = []
        if not image_segments and image.text and image.text.strip().startswith("http"):
            image_url = image.text.strip()

    return {
        "description": _text("Description").replace("[br]", "\n"),
        "website": _text("Website"),
        "nzb_segments": nzb_segments,
        "image_segments": image_segments,
        "image_url": image_url,
        "image_width": width,
        "image_height": height,
    }


def decode_spot_binary(data: bytes, compressed: bool) -> bytes:
    """Decode a spot binary body: unescape =A/=B/=C/=D, optionally raw-inflate.

    NZB payloads are deflated (compressed=True), images are not.
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x3D and i + 1 < n:  # '='
            nxt = data[i + 1]
            mapped = {0x41: 0x00, 0x42: 0x0D, 0x43: 0x0A, 0x44: 0x3D}.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
        out.append(b)
        i += 1
    raw = bytes(out)
    if compressed:
        # Bounded inflate: an unbounded decompress on spot data is a zip bomb.
        obj = zlib.decompressobj(-15)
        raw = obj.decompress(raw, MAX_INFLATED_BYTES)
        if obj.unconsumed_tail:
            raise ValueError("Compressed payload exceeds the size limit")
    return raw
