"""Minimal HTTP/2 framing + HPACK, hand-rolled on asyncio streams.

Implements just enough of RFC 7540/7541 to open a connection, send HEADERS +
optional DATA on fresh streams, and read back HEADERS + DATA per stream.
Static-table-only HPACK with literal-without-indexing and Huffman-free
encoding - legal, if not maximally compact.
"""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

CLIENT_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_PRIORITY = 0x2
FRAME_RST_STREAM = 0x3
FRAME_SETTINGS = 0x4
FRAME_PUSH_PROMISE = 0x5
FRAME_PING = 0x6
FRAME_GOAWAY = 0x7
FRAME_WINDOW_UPDATE = 0x8
FRAME_CONTINUATION = 0x9

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

SETTINGS_HEADER_TABLE_SIZE = 0x1
SETTINGS_ENABLE_PUSH = 0x2
SETTINGS_MAX_CONCURRENT_STREAMS = 0x3
SETTINGS_INITIAL_WINDOW_SIZE = 0x4
SETTINGS_MAX_FRAME_SIZE = 0x5

# RFC 7541 Appendix A - static table (index 1..61)
STATIC_TABLE: list[tuple[str, str]] = [
    ("", ""),
    (":authority", ""), (":method", "GET"), (":method", "POST"),
    (":path", "/"), (":path", "/index.html"), (":scheme", "http"),
    (":scheme", "https"), (":status", "200"), (":status", "204"),
    (":status", "206"), (":status", "304"), (":status", "400"),
    (":status", "404"), (":status", "500"), ("accept-charset", ""),
    ("accept-encoding", "gzip, deflate"), ("accept-language", ""),
    ("accept-ranges", ""), ("accept", ""), ("access-control-allow-origin", ""),
    ("age", ""), ("allow", ""), ("authorization", ""), ("cache-control", ""),
    ("content-disposition", ""), ("content-encoding", ""), ("content-language", ""),
    ("content-length", ""), ("content-location", ""), ("content-range", ""),
    ("content-type", ""), ("cookie", ""), ("date", ""), ("etag", ""),
    ("expect", ""), ("expires", ""), ("from", ""), ("host", ""),
    ("if-match", ""), ("if-modified-since", ""), ("if-none-match", ""),
    ("if-range", ""), ("if-unmodified-since", ""), ("last-modified", ""),
    ("link", ""), ("location", ""), ("max-forwards", ""),
    ("proxy-authenticate", ""), ("proxy-authorization", ""), ("range", ""),
    ("referer", ""), ("refresh", ""), ("retry-after", ""), ("server", ""),
    ("set-cookie", ""), ("strict-transport-security", ""),
    ("transfer-encoding", ""), ("user-agent", ""), ("vary", ""),
    ("via", ""), ("www-authenticate", ""),
]

STATIC_INDEX: dict[tuple[str, str], int] = {kv: i for i, kv in enumerate(STATIC_TABLE) if i}
STATIC_NAME_INDEX: dict[str, int] = {}
for i, (name, _) in enumerate(STATIC_TABLE):
    if i and name not in STATIC_NAME_INDEX:
        STATIC_NAME_INDEX[name] = i


def _encode_int(value: int, prefix_bits: int, first_byte_mask: int = 0) -> bytes:
    limit = (1 << prefix_bits) - 1
    if value < limit:
        return bytes([first_byte_mask | value])
    out = bytearray([first_byte_mask | limit])
    value -= limit
    while value >= 128:
        out.append((value % 128) + 128)
        value //= 128
    out.append(value)
    return bytes(out)


def _encode_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _encode_int(len(raw), 7, 0x00) + raw


def encode_header_block(headers: list[tuple[str, str]]) -> bytes:
    """Static-table-only HPACK: full-match indexes, else literal-without-indexing."""
    out = bytearray()
    for name, value in headers:
        idx = STATIC_INDEX.get((name, value))
        if idx:
            out += _encode_int(idx, 7, 0x80)   # indexed header field
            continue
        name_idx = STATIC_NAME_INDEX.get(name)
        if name_idx:
            out += _encode_int(name_idx, 4, 0x00)  # literal w/o indexing, indexed name
        else:
            out += b"\x00" + _encode_str(name)     # literal w/o indexing, new name
        out += _encode_str(value)
    return bytes(out)


def _decode_int(data: bytes, pos: int, prefix_bits: int) -> tuple[int, int]:
    limit = (1 << prefix_bits) - 1
    value = data[pos] & limit
    pos += 1
    if value < limit:
        return value, pos
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value += (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return value, pos


def _decode_str(data: bytes, pos: int) -> tuple[str, int]:
    length, pos = _decode_int(data, pos, 7)
    raw = data[pos:pos + length]
    return raw.decode("utf-8", "replace"), pos + length


def decode_header_block(block: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(block):
        b = block[pos]
        if b & 0x80:                                  # indexed
            idx, pos = _decode_int(block, pos, 7)
            if 0 < idx < len(STATIC_TABLE):
                out.append(STATIC_TABLE[idx])
        elif b & 0x40:                                # literal with incremental indexing
            idx, pos = _decode_int(block, pos, 6)
            name = STATIC_TABLE[idx][0] if idx else ""
            if not idx:
                name, pos = _decode_str(block, pos)
            value, pos = _decode_str(block, pos)
            out.append((name, value))
        elif b & 0x20:                                # dynamic table size update
            _, pos = _decode_int(block, pos, 5)
        else:                                         # literal without indexing
            idx, pos = _decode_int(block, pos, 4)
            name = STATIC_TABLE[idx][0] if idx else ""
            if not idx:
                name, pos = _decode_str(block, pos)
            value, pos = _decode_str(block, pos)
            out.append((name, value))
    return out


def pack_frame(ftype: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload))[1:] + bytes([ftype, flags]) + \
        struct.pack(">I", stream_id & 0x7FFFFFFF) + payload


@dataclass
class H2Response:
    status: int = 0
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    error: str | None = None


class H2Connection:
    """One HTTP/2 connection over an open asyncio stream pair."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._next_stream = 1
        self._lock = asyncio.Lock()
        self.closed = False

    async def handshake(self) -> None:
        self.writer.write(CLIENT_PREFACE)
        settings = (
            struct.pack(">HI", SETTINGS_ENABLE_PUSH, 0)
            + struct.pack(">HI", SETTINGS_MAX_CONCURRENT_STREAMS, 512)
            + struct.pack(">HI", SETTINGS_INITIAL_WINDOW_SIZE, 1 << 20)
        )
        self.writer.write(pack_frame(FRAME_SETTINGS, 0, 0, settings))
        await self.writer.drain()
        await self._await_settings_ack()

    async def _await_settings_ack(self, deadline: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                return
            try:
                ftype, flags, sid, payload = await asyncio.wait_for(
                    self._read_frame(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            if ftype == FRAME_SETTINGS and not flags & FLAG_ACK:
                self.writer.write(pack_frame(FRAME_SETTINGS, FLAG_ACK, 0, b""))
                await self.writer.drain()
                return

    async def _read_frame(self) -> tuple[int, int, int, bytes]:
        head = await self.reader.readexactly(9)
        length = int.from_bytes(head[0:3], "big")
        ftype = head[3]
        flags = head[4]
        sid = int.from_bytes(head[5:9], "big") & 0x7FFFFFFF
        payload = await self.reader.readexactly(length) if length else b""
        return ftype, flags, sid, payload

    async def request(self, headers: list[tuple[str, str]], body: bytes = b"",
                      timeout: float = 8.0) -> H2Response:
        async with self._lock:
            if self.closed:
                return H2Response(error="connection closed")
            sid = self._next_stream
            self._next_stream += 2
            resp = H2Response()
            try:
                block = encode_header_block(headers)
                flags = FLAG_END_HEADERS | (FLAG_END_STREAM if not body else 0)
                self.writer.write(pack_frame(FRAME_HEADERS, flags, sid, block))
                if body:
                    self.writer.write(pack_frame(FRAME_DATA, FLAG_END_STREAM, sid, body))
                await self.writer.drain()
                return await asyncio.wait_for(self._collect(sid, resp), timeout=timeout)
            except asyncio.TimeoutError:
                resp.error = "TimeoutError"
                return resp
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                self.closed = True
                resp.error = type(exc).__name__
                return resp

    async def _collect(self, sid: int, resp: H2Response) -> H2Response:
        pending = bytearray()
        while True:
            ftype, flags, frame_sid, payload = await self._read_frame()
            if ftype == FRAME_SETTINGS and not flags & FLAG_ACK:
                self.writer.write(pack_frame(FRAME_SETTINGS, FLAG_ACK, 0, b""))
                continue
            if ftype == FRAME_PING and not flags & FLAG_ACK:
                self.writer.write(pack_frame(FRAME_PING, FLAG_ACK, 0, payload))
                continue
            if ftype == FRAME_GOAWAY:
                self.closed = True
                if resp.error is None:
                    resp.error = "GOAWAY"
                return resp
            if frame_sid != sid:
                continue
            if ftype in (FRAME_HEADERS, FRAME_CONTINUATION):
                pending += payload
                if flags & FLAG_END_HEADERS:
                    for name, value in decode_header_block(bytes(pending)):
                        if name == ":status":
                            try:
                                resp.status = int(value)
                            except ValueError:
                                pass
                        else:
                            resp.headers.append((name, value))
                    pending.clear()
            elif ftype == FRAME_DATA:
                resp.body += payload
                if payload:
                    inc = len(payload).to_bytes(4, "big")
                    self.writer.write(pack_frame(FRAME_WINDOW_UPDATE, 0, 0, inc))
                    self.writer.write(pack_frame(FRAME_WINDOW_UPDATE, 0, sid, inc))
            elif ftype == FRAME_RST_STREAM:
                resp.error = "RST_STREAM"
                return resp
            if flags & FLAG_END_STREAM and ftype in (FRAME_DATA, FRAME_HEADERS):
                return resp

    def close(self) -> None:
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass
