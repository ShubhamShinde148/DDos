"""Hand-rolled SOCKS5 client (RFC 1928 + RFC 1929 auth) on asyncio streams."""
from __future__ import annotations

import asyncio
import ipaddress
import struct


class SocksError(ConnectionError):
    """SOCKS5 negotiation failure."""


REPLY_CODES = {
    0x00: "succeeded",
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


async def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    dest_host: str,
    dest_port: int,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 8.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a SOCKS5 tunnel and return the raw stream pair to the destination."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port), timeout=timeout)

    try:
        # --- greeting ---
        methods = b"\x00\x02" if username else b"\x00"
        writer.write(bytes([0x05, len(methods)]) + methods)
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        ver, method = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if ver != 0x05:
            raise SocksError(f"bad SOCKS version {ver:#x}")
        if method == 0xFF:
            raise SocksError("no acceptable auth method")
        if method == 0x02:
            if not username:
                raise SocksError("proxy demands user/pass but none supplied")
            user_b = username.encode()
            pass_b = (password or "").encode()
            writer.write(bytes([0x01, len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            _, status = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            if status != 0x00:
                raise SocksError("SOCKS5 auth rejected")

        # --- CONNECT request ---
        try:
            ip = ipaddress.ip_address(dest_host)
            if ip.version == 4:
                addr = b"\x01" + ip.packed
            else:
                addr = b"\x04" + ip.packed
        except ValueError:
            name = dest_host.encode()
            addr = b"\x03" + bytes([len(name)]) + name

        writer.write(bytes([0x05, 0x01, 0x00]) + addr + struct.pack(">H", dest_port))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        head = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        ver, rep, _, atyp = head
        if ver != 0x05:
            raise SocksError(f"bad reply version {ver:#x}")
        if rep != 0x00:
            raise SocksError(f"CONNECT failed: {REPLY_CODES.get(rep, hex(rep))}")

        # drain the bound address so the stream is clean
        if atyp == 0x01:
            await asyncio.wait_for(reader.readexactly(4 + 2), timeout=timeout)
        elif atyp == 0x04:
            await asyncio.wait_for(reader.readexactly(16 + 2), timeout=timeout)
        elif atyp == 0x03:
            ln = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
            await asyncio.wait_for(reader.readexactly(ln + 2), timeout=timeout)
        else:
            raise SocksError(f"unknown address type {atyp:#x}")

        return reader, writer
    except Exception:
        try:
            writer.close()
        except Exception:
            pass
        raise
