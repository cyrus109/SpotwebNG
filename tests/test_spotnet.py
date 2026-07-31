"""Tests for the Spotnet wire-format parser (port of Spotweb's Services_Format_Parsing).

Format reference (verified against spotweb/spotweb master):
From: [Nick] <[KEY]@[CAT][KEY-ID][SUBCATS].[SIZE].[RANDOM].[DATE].[CUSTOM-ID].[CUSTOM-VALUE].[SIGNATURE]>
CAT is 1-based on the wire; internal is 0-based (0=Image 1=Sound 2=Games 3=Apps).
"""

import hashlib
from datetime import UTC, datetime

from app.spotnet import (
    calculate_spotter_id,
    create_subcat_z,
    parse_from_header,
    parse_xover_row,
    split_subject,
    to_newznab,
    unprepare_base64,
)

# 1=Image(cat0) keyid 4, subcats a09 b03 c02 d05 → x264, Retail source (NOT b04=TV!)
FROM_HD_MOVIE = (
    "SpotterNick <qwerty123@14a09b03c02d05.734003200.random.20260101.none.none.SIGBASE64>"
)
# TV series: b04 (TV source) triggers z1
FROM_TV = "TvGuy <abc@11a09b04d11.5000000000.r.20260101.n.n.SIG>"
# Sound (cat1), keyid 2, FLAC (a08) → lossless
FROM_FLAC = "DJ <k@21a08b00c08.900000000.r.20260101.n.n.SIG>"


def test_parse_from_header_basic_fields():
    h = parse_from_header(FROM_HD_MOVIE)
    assert h is not None
    assert h["poster"] == "SpotterNick"
    assert h["category"] == 0  # wire 1 → internal 0 (Image)
    assert h["keyid"] == 4
    assert h["filesize"] == 734003200
    assert h["headersign"] == "SIGBASE64"
    assert h["subcats"]["a"] == ["a9"]  # leading zeros stripped
    assert h["subcats"]["b"] == ["b3"]
    assert h["subcats"]["c"] == ["c2"]
    assert h["subcats"]["d"] == ["d5"]


def test_parse_from_header_ftd_style_no_zero_padding():
    h = parse_from_header("X <k@19a9b4c0d5d15d11.100.r.d.n.n.S>")
    assert h["category"] == 0
    assert h["keyid"] == 9
    assert h["subcats"]["d"] == ["d5", "d15", "d11"]


def test_parse_from_header_rejects_garbage():
    assert parse_from_header("Just A Name") is None
    assert parse_from_header("N <local@short.sig>") is None  # <6 domain fields
    assert parse_from_header("N <a@0Xabc.1.r.d.n.n.S>") is None  # bad category


def test_split_subject_modern_key():
    assert split_subject("Movie Title 2026 | grp", keyid=4) == (
        "Movie Title 2026",
        "grp",
    )
    assert split_subject("No Tag Here", keyid=4) == ("No Tag Here", "")


def test_create_subcat_z_rules():
    assert create_subcat_z(0, {"a": ["a9"], "b": ["b4"], "d": []}) == "z1"  # TV source → series
    assert create_subcat_z(0, {"a": ["a9"], "b": [], "d": ["d11"]}) == "z1"  # Television genre
    assert create_subcat_z(0, {"a": ["a5"], "b": [], "d": []}) == "z2"  # ePub → book
    assert create_subcat_z(0, {"a": ["a9"], "b": [], "d": ["d75"]}) == "z3"  # erotica genre
    assert create_subcat_z(0, {"a": ["a9"], "b": [], "d": []}) == "z0"  # default movie
    assert create_subcat_z(1, {"a": [], "b": [], "d": []}) == "z0"


def test_to_newznab_mapping():
    # Image/movie: x264 → Movies/HD; UHD → 2045; BluRay → 2050; SD default 2030
    assert to_newznab(0, {"a": ["a9"], "b": [], "d": [], "z": ["z0"]}) == 2040
    assert to_newznab(0, {"a": ["a15"], "b": [], "d": [], "z": ["z0"]}) == 2045
    assert to_newznab(0, {"a": ["a6"], "b": [], "d": [], "z": ["z0"]}) == 2050
    assert to_newznab(0, {"a": ["a0"], "b": [], "d": [], "z": ["z0"]}) == 2030
    # Series: HD → 5040, UHD → 5045, SD → 5030
    assert to_newznab(0, {"a": ["a9"], "b": ["b4"], "d": [], "z": ["z1"]}) == 5040
    assert to_newznab(0, {"a": ["a15"], "b": ["b4"], "d": [], "z": ["z1"]}) == 5045
    assert to_newznab(0, {"a": ["a0"], "b": ["b4"], "d": [], "z": ["z1"]}) == 5030
    # Sound: FLAC → lossless 3040, MP3 → 3010
    assert to_newznab(1, {"a": ["a8"], "b": [], "d": [], "z": ["z0"]}) == 3040
    assert to_newznab(1, {"a": ["a0"], "b": [], "d": [], "z": ["z0"]}) == 3010
    # Books → 7020 ebook; games → 4050; apps → 4020 Win / 4030 Mac / 4040 mobile
    assert to_newznab(0, {"a": ["a5"], "b": [], "d": [], "z": ["z2"]}) == 7020
    assert to_newznab(2, {"a": ["a0"], "b": [], "d": [], "z": []}) == 4050
    assert to_newznab(3, {"a": ["a0"], "b": [], "d": [], "z": []}) == 4020
    assert to_newznab(3, {"a": ["a1"], "b": [], "d": [], "z": []}) == 4030
    assert to_newznab(3, {"a": ["a7"], "b": [], "d": [], "z": []}) == 4040
    # Erotica → 6040 HD / 6030 SD
    assert to_newznab(0, {"a": ["a9"], "b": [], "d": ["d75"], "z": ["z3"]}) == 6040
    assert to_newznab(0, {"a": ["a0"], "b": [], "d": ["d75"], "z": ["z3"]}) == 6030


def test_unprepare_base64():
    # PHP pads BEFORE replacing '-s'→'/', '-p'→'+' (len%4 on the raw input)
    assert unprepare_base64("ab-scd-p") == "ab/cd+"  # len 8 → no padding added
    assert unprepare_base64("abcde") == "abcde==="
    assert unprepare_base64("abcd") == "abcd"


def test_calculate_spotter_id_matches_php_algorithm():
    # PHP: crc32(base64_decode(key)), packed little-endian, base64, strip /+=
    import base64
    import zlib

    key = base64.b64encode(b"some-user-key").decode()
    crc = zlib.crc32(b"some-user-key") & 0xFFFFFFFF
    packed = bytes([crc & 0xFF, (crc >> 8) & 0xFF, (crc >> 16) & 0xFF, (crc >> 24) & 0xFF])
    expected = base64.b64encode(packed).decode().replace("/", "").replace("+", "").replace("=", "")
    assert calculate_spotter_id(key) == expected


def test_parse_xover_row_full_spot():
    row = {
        "Number": 12345,
        "Subject": "Great Movie 2026 1080p | SpotGroup",
        "From": FROM_HD_MOVIE,
        "Date": "Sat, 25 Jul 2026 20:00:00 +0200",
        "Message-ID": "abc123@spot.net",
    }
    spot = parse_xover_row(row)
    assert spot is not None
    assert spot.message_id == "<abc123@spot.net>"
    assert spot.title == "Great Movie 2026 1080p"
    assert spot.tag == "SpotGroup"
    assert spot.poster == "SpotterNick"
    assert spot.newznab_cat == 2040
    assert spot.filesize == 734003200
    assert spot.stamp == datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


def test_parse_xover_row_keyid7_requires_hashcash():
    bad = {
        "Number": 1,
        "Subject": "T | g",
        "From": "N <k@17a09.100.r.d.n.n.S>",  # keyid 7 = self-signed, needs hashcash
        "Date": "Sat, 25 Jul 2026 20:00:00 +0200",
        "Message-ID": "definitely-not-hashcash@x",
    }
    assert hashlib.sha1(b"<definitely-not-hashcash@x>").hexdigest()[:4] != "0000"
    assert parse_xover_row(bad) is None


def test_parse_xover_row_skips_moderation_commands():
    # keyid-2 spots titled delete|dispose|remove are moderation commands, not content
    for verb in ("delete", "DISPOSE", "remove"):
        row = {
            "Number": 1,
            "Subject": f"{verb} <target@spot.net>",
            "From": "Mod <k@12a09.100.r.d.n.n.S>",  # keyid 2
            "Date": "Sat, 25 Jul 2026 20:00:00 +0200",
            "Message-ID": "mod1@spot.net",
        }
        assert parse_xover_row(row) is None


def test_parse_xover_row_rejects_invalid_from():
    row = {
        "Number": 1,
        "Subject": "regular usenet post",
        "From": "someone@example.com",
        "Date": "Sat, 25 Jul 2026 20:00:00 +0200",
        "Message-ID": "x@y",
    }
    assert parse_xover_row(row) is None


# ---------- full-spot XML (X-XML headers) ----------

SAMPLE_XML = (
    "<Spotnet><Posting><Key>7</Key><Created>1753500000</Created>"
    "<Poster>NickF</Poster><Title>Great Movie 2026</Title>"
    "<Description>A very nice release[br]Second line</Description>"
    "<Website>https://example.com/info</Website>"
    "<Size>734003200</Size><Category>01<Sub>01a09</Sub><Sub>01b03</Sub></Category>"
    '<Image Width="300" Height="450"><Segment>img-part1@spot.net</Segment></Image>'
    "<NZB><Segment>nzb-part1@spot.net</Segment><Segment>nzb-part2@spot.net</Segment></NZB>"
    "</Posting></Spotnet>"
)


def test_parse_full_xml_extracts_everything():
    from app.spotnet import parse_full_xml

    full = parse_full_xml(SAMPLE_XML)
    assert full is not None
    assert "very nice release" in full["description"]
    assert full["website"] == "https://example.com/info"
    assert full["nzb_segments"] == ["nzb-part1@spot.net", "nzb-part2@spot.net"]
    assert full["image_segments"] == ["img-part1@spot.net"]


def test_parse_full_xml_legacy_image_url():
    from app.spotnet import parse_full_xml

    xml = "<Spotnet><Posting><Title>T</Title><Image>https://img.example/x.jpg</Image></Posting></Spotnet>"
    full = parse_full_xml(xml)
    assert full["image_url"] == "https://img.example/x.jpg"
    assert full["image_segments"] == []


def test_parse_full_xml_rejects_bad_segment_ids():
    from app.spotnet import parse_full_xml

    xml = "<Spotnet><Posting><NZB><Segment>ok@x</Segment><Segment>bad<id@x</Segment></NZB></Posting></Spotnet>"
    full = parse_full_xml(xml.replace("<id", "&lt;id"))
    assert full["nzb_segments"] == []  # one invalid id voids the list (Spotweb rule)


def test_parse_full_xml_survives_control_chars():
    from app.spotnet import parse_full_xml

    assert (
        parse_full_xml("<Spotnet><Posting><Title>T\x02itle</Title></Posting></Spotnet>") is not None
    )
    assert parse_full_xml("not xml at all <<<") is None


def test_decode_spot_binary_roundtrip():
    import zlib

    from app.spotnet import decode_spot_binary

    nzb = b'<?xml version="1.0"?><nzb>content \x00 = \r\n here</nzb>'
    deflated = zlib.compress(nzb)[2:-4]  # raw DEFLATE (strip zlib header+checksum)
    escaped = (
        deflated.replace(b"=", b"=D")
        .replace(b"\x00", b"=A")
        .replace(b"\r", b"=B")
        .replace(b"\n", b"=C")
    )
    assert decode_spot_binary(escaped, compressed=True) == nzb

    raw_image = b"\xff\xd8\xff\xe0 jpeg = data \r\n \x00 end"
    escaped_img = (
        raw_image.replace(b"=", b"=D")
        .replace(b"\x00", b"=A")
        .replace(b"\r", b"=B")
        .replace(b"\n", b"=C")
    )
    assert decode_spot_binary(escaped_img, compressed=False) == raw_image
