"""Live NNTP connection check for the setup wizard.

Raw asyncio implementation (stdlib nntplib is deprecated). Speaks just
enough NNTP to verify reachability and credentials: greeting, AUTHINFO
USER/PASS, QUIT.
"""

from __future__ import annotations

import asyncio
import ssl as ssl_module

# NNTP is a line protocol: a CRLF in a credential injects a command. And an
# unrestricted port turns this endpoint into a LAN port scanner.
ALLOWED_PORTS = frozenset({119, 433, 563, 8080, 9119})


def credentials_are_safe(*values: str) -> bool:
    return not any(ch in v for v in values for ch in ("\r", "\n", "\0"))


async def check_nntp(
    host: str,
    port: int,
    use_ssl: bool,
    username: str = "",
    password: str = "",
    timeout: float = 8.0,
) -> dict:
    """Return {"ok": bool, "field": None|"host"|"auth", "message": str}."""
    if not credentials_are_safe(username, password):
        return {
            "ok": False,
            "field": "auth",
            "message": "Username and password cannot contain line breaks.",
        }
    if port not in ALLOWED_PORTS and port < 1024:
        return {
            "ok": False,
            "field": "host",
            "message": f"Port {port} is not a Usenet port. Use 119, 563 or another news port.",
        }
    try:
        return await asyncio.wait_for(_check(host, port, use_ssl, username, password), timeout)
    except TimeoutError:
        return {
            "ok": False,
            "field": "host",
            "message": f"Timed out after {timeout:.0f}s connecting to {host}:{port}",
        }
    except (OSError, ssl_module.SSLError) as exc:
        return {
            "ok": False,
            "field": "host",
            "message": _os_error_message(exc, host, port),
        }


async def _check(host: str, port: int, use_ssl: bool, username: str, password: str) -> dict:
    ssl_ctx = ssl_module.create_default_context() if use_ssl else None
    reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
    try:
        greeting = (await reader.readline()).decode(errors="replace").strip()
        if not greeting.startswith(("200", "201")):
            return {
                "ok": False,
                "field": "host",
                "message": f"Unexpected server greeting: {greeting or '(empty)'}",
            }

        if username:
            reply = await _command(reader, writer, f"AUTHINFO USER {username}")
            if reply.startswith("381"):
                reply = await _command(reader, writer, f"AUTHINFO PASS {password}")
            if not reply.startswith("281"):
                return {
                    "ok": False,
                    "field": "auth",
                    "message": f"Provider rejected the credentials: {reply}",
                }
            message = f"Connected and authenticated ({reply})"
        else:
            message = f"Connected ({greeting})"

        writer.write(b"QUIT\r\n")
        await writer.drain()
        return {"ok": True, "field": None, "message": message}
    finally:
        writer.close()


async def _command(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, line: str) -> str:
    writer.write(line.encode() + b"\r\n")
    await writer.drain()
    return (await reader.readline()).decode(errors="replace").strip()


def _os_error_message(exc: Exception, host: str, port: int) -> str:
    if isinstance(exc, ssl_module.SSLError):
        return (
            f"SSL failed for {host}:{port} — try port 119 without SSL? ({exc.__class__.__name__})"
        )
    reason = getattr(exc, "strerror", None) or exc
    return f"Cannot reach {host}:{port} — {reason}"
