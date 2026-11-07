"""Protocol-level tests for internal.httpresp, against real loopback sockets.

Every test binds a throwaway server to 127.0.0.1 on an ephemeral port. No
mocks: the point is to exercise the parser against bytes that a real socket
actually delivered, including the awkward cases (early close, chunk framing,
missing length).
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internal.httpresp import (  # noqa: E402
    MalformedChunk,
    ResponseError,
    TruncatedBody,
    body_expected,
    decide_keep_alive,
    read_response,
)


async def _serve_once(payload: bytes, *, close_early: bool = False) -> tuple[str, int]:
    """Start a server that writes `payload` on first connection and returns its address."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(payload)
        await writer.drain()
        if close_early:
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


@contextlib.asynccontextmanager
async def server_for(payload: bytes, *, close_early: bool = False):
    server, port = await _serve_once(payload, close_early=close_early)
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def fetch(port: int, method: str = "GET", timeout: float = 2.0):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    try:
        return await read_response(reader, method, timeout)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ---------------------------------------------------------------- unit rules


def test_keep_alive_http11_default():
    assert decide_keep_alive("HTTP/1.1", []) is True
    assert decide_keep_alive("HTTP/1.1", [("Connection", "close")]) is False


def test_keep_alive_http10_requires_opt_in():
    assert decide_keep_alive("HTTP/1.0", []) is False
    assert decide_keep_alive("HTTP/1.0", [("Connection", "keep-alive")]) is True
    assert decide_keep_alive("HTTP/1.0", [("Connection", "Keep-Alive, foo")]) is True


def test_body_expected_rules():
    assert body_expected("GET", 200) is True
    assert body_expected("GET", 204) is False
    assert body_expected("GET", 304) is False
    assert body_expected("HEAD", 200) is False
    assert body_expected("GET", 101) is False


# ---------------------------------------------------------------- wire tests


def test_http_200_with_content_length():
    payload = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 5\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"hello"
    )

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.status == 200
    assert resp.body == b"hello"
    assert resp.keep_alive is True
    assert resp.chunked is False
    assert resp.total_bytes == len(payload)


def test_http_404():
    payload = b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nnot found"

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.status == 404
    assert resp.reason == "Not Found"
    assert resp.body == b"not found"


def test_empty_body_content_length_zero():
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.status == 200
    assert resp.body == b""
    assert resp.keep_alive is True


def test_no_body_on_204_even_with_content_length():
    payload = b"HTTP/1.1 204 No Content\r\nContent-Length: 99\r\n\r\n"

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.status == 204
    assert resp.body == b""


def test_head_request_has_no_body():
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 1234\r\n\r\n"

    async def run():
        async with server_for(payload) as port:
            return await fetch(port, method="HEAD")

    resp = asyncio.run(run())
    assert resp.status == 200
    assert resp.body == b""


def test_large_body():
    body = b"x" * 200_000
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert len(resp.body) == 200_000
    assert resp.body[:3] == b"xxx"


def test_truncated_body_raises():
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"

    async def run():
        async with server_for(payload, close_early=True) as port:
            return await fetch(port)

    with pytest.raises(TruncatedBody) as exc:
        asyncio.run(run())
    assert exc.value.declared == 100
    assert exc.value.received == 5


def test_chunked_body():
    payload = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n"
        b"6\r\n world\r\n"
        b"0\r\n\r\n"
    )

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.chunked is True
    assert resp.body == b"hello world"
    assert resp.keep_alive is True


def test_chunked_with_trailer():
    payload = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"4\r\nabcd\r\n"
        b"0\r\n"
        b"X-Checksum: deadbeef\r\n\r\n"
    )

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.body == b"abcd"


def test_malformed_chunk_size():
    payload = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"ZZZ\r\nnope\r\n"
    )

    async def run():
        async with server_for(payload, close_early=True) as port:
            return await fetch(port)

    with pytest.raises((MalformedChunk, asyncio.IncompleteReadError, asyncio.TimeoutError)):
        asyncio.run(run())


def test_connection_close_is_parsed():
    payload = b"HTTP/1.0 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nhi"

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.version == "HTTP/1.0"
    assert resp.keep_alive is False
    assert resp.body == b"hi"


def test_http10_keepalive_opt_in():
    payload = b"HTTP/1.0 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nhi"

    async def run():
        async with server_for(payload) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.keep_alive is True


def test_read_until_close_when_no_length():
    payload = b"HTTP/1.0 200 OK\r\n\r\nstreamed-until-eof"

    async def run():
        async with server_for(payload, close_early=True) as port:
            return await fetch(port)

    resp = asyncio.run(run())
    assert resp.body == b"streamed-until-eof"
    assert resp.keep_alive is False


def test_timeout_on_silent_server():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(5)
        writer.close()

    async def run():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            try:
                return await read_response(reader, "GET", timeout=0.3)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())


def test_malformed_status_line():
    payload = b"NOTHTTP\r\n\r\n"

    async def run():
        async with server_for(payload, close_early=True) as port:
            return await fetch(port)

    with pytest.raises(ResponseError):
        asyncio.run(run())


def test_sequenced_responses_on_one_connection():
    """Three responses back to back on a single socket, all parsed cleanly."""

    async def handler(reader, writer):
        for i in range(3):
            await reader.readuntil(b"\r\n\r\n")
            body = f"resp{i}".encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        writer.close()

    async def run():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            out = []
            for _ in range(3):
                writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                resp = await read_response(reader, "GET", timeout=2.0)
                out.append(resp.body)
                assert resp.keep_alive is True
            writer.close()
            return out
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run()) == [b"resp0", b"resp1", b"resp2"]
