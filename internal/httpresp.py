"""HTTP/1.x response parsing: status line, headers, framing, body, reuse decision.

This module owns the correctness that the load loop used to hand-roll inline.
Framing rules follow RFC 7230 section 3.3.3, in priority order:

  1. 1xx / 204 / 304 responses never carry a body
  2. HEAD responses never carry a body
  3. Transfer-Encoding: chunked -> read chunk stream
  4. Content-Length -> read exactly that many bytes
  5. otherwise -> read until the peer closes

Keep-alive is decided from the response, never the request: an HTTP/1.0 peer
is persistent only if it says so, an HTTP/1.1 peer is persistent unless it says
otherwise.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

CRLF = b"\r\n"
HEADER_END = b"\r\n\r\n"
MAX_HEADER_BYTES = 256 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024


class ResponseError(Exception):
    """Protocol-level failure while reading a response."""


class TruncatedBody(ResponseError):
    """The peer closed before the declared body length was satisfied."""

    def __init__(self, declared: int, received: int) -> None:
        super().__init__(f"truncated body: declared {declared}, received {received}")
        self.declared = declared
        self.received = received


class MalformedChunk(ResponseError):
    """Chunked framing was violated."""


@dataclass
class Response:
    version: str = "HTTP/1.0"
    status: int = 0
    reason: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    header_bytes: int = 0
    chunked: bool = False
    keep_alive: bool = False
    body_expected: bool = True
    truncated: bool = False

    def header(self, name: str) -> str | None:
        target = name.lower()
        for k, v in self.headers:
            if k.lower() == target:
                return v
        return None

    def header_all(self, name: str) -> list[str]:
        target = name.lower()
        return [v for k, v in self.headers if k.lower() == target]

    @property
    def total_bytes(self) -> int:
        return self.header_bytes + len(self.body)


def _tokens(value: str) -> set[str]:
    return {t.strip().lower() for t in value.split(",") if t.strip()}


def decide_keep_alive(version: str, headers: list[tuple[str, str]]) -> bool:
    """RFC 7230 6.3: persistence comes from the response, not the request."""
    connection = set()
    for k, v in headers:
        if k.lower() == "connection":
            connection |= _tokens(v)
    if "close" in connection:
        return False
    major_minor = version.upper().replace("HTTP/", "")
    if major_minor == "1.0":
        return "keep-alive" in connection
    return True


def body_expected(method: str, status: int) -> bool:
    if method.upper() == "HEAD":
        return False
    if 100 <= status < 200:
        return False
    if status in (204, 304):
        return False
    return True


async def _read_exact(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    if n <= 0:
        return b""
    return await asyncio.wait_for(reader.readexactly(n), timeout=timeout)


async def read_chunked(
    reader: asyncio.StreamReader,
    timeout: float,
    max_bytes: int = MAX_BODY_BYTES,
) -> bytes:
    """Decode a chunked body into the reassembled payload."""
    out = bytearray()
    while True:
        line = await asyncio.wait_for(reader.readuntil(CRLF), timeout=timeout)
        raw = line[:-2]
        if not raw:
            raise MalformedChunk("empty chunk-size line")
        size_token = raw.split(b";", 1)[0].strip()
        try:
            size = int(size_token, 16)
        except ValueError as exc:
            raise MalformedChunk(f"bad chunk size {size_token!r}") from exc
        if size == 0:
            # trailer section, terminated by an empty line
            while True:
                trailer = await asyncio.wait_for(reader.readuntil(CRLF), timeout=timeout)
                if trailer == CRLF:
                    break
            return bytes(out)
        if len(out) + size > max_bytes:
            raise MalformedChunk(f"chunked body exceeds {max_bytes} bytes")
        out += await _read_exact(reader, size, timeout)
        terminator = await _read_exact(reader, 2, timeout)
        if terminator != CRLF:
            raise MalformedChunk(f"chunk terminator was {terminator!r}")


async def read_response(
    reader: asyncio.StreamReader,
    method: str,
    timeout: float,
    *,
    max_body: int = MAX_BODY_BYTES,
) -> Response:
    """Read exactly one HTTP/1.x response, leaving the stream clean for reuse."""
    head = await asyncio.wait_for(
        reader.readuntil(HEADER_END), timeout=timeout)
    if len(head) > MAX_HEADER_BYTES:
        raise ResponseError(f"header block exceeds {MAX_HEADER_BYTES} bytes")

    lines = head.split(CRLF)
    status_line = lines[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2:
        raise ResponseError(f"malformed status line {status_line!r}")

    version = parts[0].upper()
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise ResponseError(f"malformed status code {parts[1]!r}") from exc
    reason = parts[2] if len(parts) > 2 else ""

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + " " + line.decode("latin-1", "replace").strip())
            continue
        if b":" not in line:
            raise ResponseError(f"malformed header line {line!r}")
        name_b, value_b = line.split(b":", 1)
        headers.append((name_b.decode("latin-1").strip(),
                        value_b.decode("latin-1").strip()))

    resp = Response(
        version=version,
        status=status,
        reason=reason,
        headers=headers,
        header_bytes=len(head),
        keep_alive=decide_keep_alive(version, headers),
        body_expected=body_expected(method, status),
    )
    if not resp.body_expected:
        return resp

    transfer = ",".join(resp.header_all("transfer-encoding")).lower()
    if "chunked" in transfer:
        resp.chunked = True
        resp.body = await read_chunked(reader, timeout, max_body)
        return resp

    clen_raw = resp.header("content-length")
    if clen_raw is not None:
        try:
            clen = int(clen_raw.split(",")[0].strip())
        except ValueError as exc:
            raise ResponseError(f"bad Content-Length {clen_raw!r}") from exc
        if clen < 0:
            raise ResponseError(f"negative Content-Length {clen}")
        if clen > max_body:
            raise ResponseError(f"body of {clen} exceeds {max_body}")
        try:
            resp.body = await _read_exact(reader, clen, timeout)
        except asyncio.IncompleteReadError as exc:
            resp.truncated = True
            raise TruncatedBody(clen, len(exc.partial)) from exc
        return resp

    # No length, no chunking: body runs until the peer closes
    out = bytearray()
    while True:
        chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        if not chunk:
            break
        out += chunk
        if len(out) > max_body:
            raise ResponseError(f"body exceeds {max_body}")
    resp.body = bytes(out)
    resp.keep_alive = False
    return resp
