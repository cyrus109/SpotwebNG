"""Spotnet signature verification (ported from Spotweb's Services_Signing_*).

Spots are signed over `title + <header after '@', minus the trailing '.signature'> +
poster` (raw bytes, no separators) with RSA PKCS#1 v1.5 / SHA-1. Key ids 2, 3 and 4 use
fixed trusted keys; key id 7 is self-signed with the spotter's own key, which travels
in the From header, plus a hashcash on the message-id and a user signature over
`<message-id>`. Comments carry a user signature over `<message-id>` (or that plus the
body and poster name) with the key in their X-User-Key header.

Verification is plain modular arithmetic, so it is done here rather than through a
crypto library: Spotnet's keys are 384 bits, below what libraries accept as a key.

Status codes stored on spots/comments:
    VERIFIED (1)  signature checks out (self-signed keys: spotter_id is set)
    FAILED   (2)  a signature was present and did not verify
    UNSIGNED (3)  nothing to verify (key id without a trusted key, or unsigned)
    0 means "not evaluated yet" and only exists on rows older than this feature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import zlib

VERIFIED, FAILED, UNSIGNED = 1, 2, 3

# Trusted public keys from Spotweb's settings defaults: (modulus b64, exponent 65537).
TRUSTED_KEYS: dict[int, int] = {
    2: int.from_bytes(
        base64.b64decode("ys8WSlqonQMWT8ubG0tAA2Q07P36E+CJmb875wSR1XH7IFhEi0CCwlUzNqBFhC+P"), "big"
    ),
    3: int.from_bytes(
        base64.b64decode("uiyChPV23eguLAJNttC/o0nAsxXgdjtvUvidV2JL+hjNzc4Tc/PPo2JdYvsqUsat"), "big"
    ),
    4: int.from_bytes(
        base64.b64decode("1k6RNDVD6yBYWR6kHmwzmSud7JkNV4SMigBrs+jFgOK5Ldzwl17mKXJhl+su/GR9"), "big"
    ),
}
EXPONENT = 65537  # 'AQAB': every Spotnet key uses it
_SHA1_PREFIX = bytes.fromhex("3021300906052b0e03021a05000414")  # DER DigestInfo for SHA-1


def b64decode(value: str) -> bytes:
    """Spotnet base64: '-s' is '/', '-p' is '+'; padding is repaired. b'' if invalid."""
    value = value.replace("-s", "/").replace("-p", "+").rstrip("=")
    value += "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return b""


def spotter_id(modulus_b64: str) -> str:
    """crc32 of the key bytes, little-endian, base64 without '/', '+', '='."""
    key = b64decode(modulus_b64)
    if not key:
        return ""
    packed = (zlib.crc32(key) & 0xFFFFFFFF).to_bytes(4, "little")
    return base64.b64encode(packed).decode().translate(str.maketrans("", "", "/+="))


def rsa_verify_sha1(
    modulus: int, message: bytes, signature: bytes, exponent: int = EXPONENT
) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-1 (what PHP's openssl_verify does by default)."""
    if modulus < 3 or not signature:
        return False
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= modulus:
        return False
    encoded = pow(value, exponent, modulus).to_bytes(size, "big")
    digest_info = _SHA1_PREFIX + hashlib.sha1(message).digest()
    if size < len(digest_info) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def hashcash_ok(message_id: str) -> bool:
    """SHA-1 of '<message-id>' starts with four zeros (key id 7 spots)."""
    return hashlib.sha1(f"<{message_id.strip('<>')}>".encode()).hexdigest().startswith("0000")


def _split_from(from_hdr: str) -> dict | None:
    """Pick the From header apart exactly like Spotweb's parseHeader does."""
    from_hdr = from_hdr.rstrip()
    pos = from_hdr.find("<")
    if pos < 1 or not from_hdr.endswith(">"):
        return None
    parts = from_hdr[pos + 1 : -1].split("@")
    if len(parts) < 2:
        return None
    local, header = parts[0], parts[1]
    fields = header.split(".")
    if len(fields) < 6 or len(header) < 2 or not header[1].isdigit():
        return None
    local_parts = local.split(".")
    return {
        "poster": from_hdr[: pos - 1],  # raw text before '<', minus the separating space
        "keyid": int(header[1]),
        "header": header,
        "headersign": fields[-1],
        "pubkey": local_parts[0],  # still in Spotnet base64 form
        "usersig": local_parts[1] if len(local_parts) > 1 else "",
    }


def verify_spot(from_hdr: str, title: str, message_id: str) -> tuple[int, int, str]:
    """Verify a spot's header signature. Returns (status, keyid, spotter_id)."""
    parts = _split_from(from_hdr)
    if parts is None:
        return UNSIGNED, -1, ""
    keyid, sign = parts["keyid"], parts["headersign"]
    if not sign:
        return UNSIGNED, keyid, ""
    signed = (
        title + parts["header"][: len(parts["header"]) - len(sign) - 1] + parts["poster"]
    ).encode("latin-1", "replace")
    signature = b64decode(sign)

    if keyid in TRUSTED_KEYS:
        if keyid == 2 and len(b64decode(parts["pubkey"])) > 37:
            return UNSIGNED, keyid, ""  # a personal moderation message, not a spot
        ok = rsa_verify_sha1(TRUSTED_KEYS[keyid], signed, signature)
        return (VERIFIED if ok else FAILED), keyid, ""
    if keyid == 7:
        key = b64decode(parts["pubkey"])
        if not key or not hashcash_ok(message_id):
            return FAILED, keyid, ""
        modulus = int.from_bytes(key, "big")
        ok = rsa_verify_sha1(modulus, signed, signature)
        if ok and parts["usersig"]:  # the spotter also signed the message-id
            ok = rsa_verify_sha1(
                modulus, f"<{message_id.strip('<>')}>".encode(), b64decode(parts["usersig"])
            )
        return (
            (VERIFIED, 7, spotter_id(parts["pubkey"].replace("-s", "/").replace("-p", "+")))
            if ok
            else (FAILED, 7, "")
        )
    return UNSIGNED, keyid, ""


def verify_comment(
    message_id: str,
    user_signature: str,
    modulus_b64: str,
    exponent_b64: str,
    body: str,
    from_name: str,
) -> tuple[int, str]:
    """Verify a comment (status, spotter_id). The signature covers '<message-id>' or
    '<message-id>' + body lines + CRLF + poster name; both layouts are tried."""
    key, sig = (
        base64.b64decode(modulus_b64 + "=" * (-len(modulus_b64) % 4)) if modulus_b64 else b"",
        b64decode(user_signature),
    )
    if not key or not sig:
        return UNSIGNED, ""
    try:
        exponent = (
            int.from_bytes(base64.b64decode(exponent_b64 + "=" * (-len(exponent_b64) % 4)), "big")
            or EXPONENT
        )
    except ValueError:
        exponent = EXPONENT
    modulus = int.from_bytes(key, "big")
    msg = f"<{message_id.strip('<>')}>"
    lines = body.replace("\r\n", "\n").split("\n")
    candidates = [msg.encode()]
    for variant in (
        lines,
        [line for line in lines if line != ""],
        lines[:-1] if lines and lines[-1] == "" else lines,
    ):
        candidates.append(
            (msg + "\r\n".join(variant) + "\r\n" + from_name).encode("latin-1", "replace")
        )
    if any(rsa_verify_sha1(modulus, c, sig, exponent) for c in candidates):
        return VERIFIED, spotter_id(modulus_b64)
    return FAILED, ""
