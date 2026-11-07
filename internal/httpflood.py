"""HTTP/1.1 + HTTP/2 request storm engine with rate shaping, scenarios,
proxy rotation, adaptive control, retries and per-connection tracking."""
from __future__ import annotations

import asyncio
import base64
import os
import random
import ssl
import sys
import time
from dataclasses import dataclass, field

from .adaptive import AdaptiveController
from .h2 import H2Connection
from .httpresp import TruncatedBody, read_response
from .metrics import Metrics
from .payloads import PayloadStrategy
from .proxypool import Proxy, ProxyPool
from .scope import Target
from .socks import SocksError, socks5_connect

DEBUG = os.environ.get("LOADLAB_DEBUG", "").lower() in ("1", "true", "yes")
_DEFAULT_TIMEOUT = 8.0


@dataclass
class HttpProfile:
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    randomize_ua: bool = True
    keepalive: bool = True
    strategy: str = PayloadStrategy.BROWSER
    cookies: bool = False
    query_buster: bool = False
    max_retries: int = 0
    retry_backoff: float = 0.1
    http2: bool = False


class RateLimiter:
    """Token bucket, resizable at runtime by the scenario director / adaptive."""

    def __init__(self, rps: int | None) -> None:
        self.cap = float(rps) if rps else 0.0
        self.tokens = float(rps) if rps else 0.0
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    def set_cap(self, rps: int | None) -> None:
        new_cap = float(rps) if rps else 0.0
        if new_cap == self.cap:
            return
        self.cap = new_cap
        self.tokens = min(self.tokens, new_cap) if new_cap else 0.0
        self.last = time.monotonic()

    async def acquire(self) -> None:
        if not self.cap:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.last) * self.cap)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.cap)


@dataclass
class ConnStats:
    opened: int = 0
    reused: int = 0
    closed: int = 0
    refused: int = 0
    timeouts: int = 0
    resets: int = 0
    tls_errors: int = 0

    def as_dict(self) -> dict:
        return {
            "opened": self.opened, "reused": self.reused, "closed": self.closed,
            "refused": self.refused, "timeouts": self.timeouts,
            "resets": self.resets, "tls_errors": self.tls_errors,
        }


class HttpFlooder:
    def __init__(
        self,
        target: Target,
        metrics: Metrics,
        concurrency: int = 128,
        rps_limit: int | None = None,
        duration: float = 30.0,
        profile: HttpProfile | None = None,
        per_conn_requests: int = 50,
        proxy_pool: ProxyPool | None = None,
        adaptive: AdaptiveController | None = None,
        scenario=None,
        timeout: float = 8.0,
    ) -> None:
        self.target = target
        self.metrics = metrics
        self.concurrency = concurrency
        self.limiter = RateLimiter(rps_limit)
        self.duration = duration
        self.profile = profile or HttpProfile()
        self.per_conn_requests = max(1, per_conn_requests)
        self.proxy_pool = proxy_pool
        self.adaptive = adaptive
        self.scenario = scenario
        self.timeout = timeout
        self.conn_stats = ConnStats()
        self._stop = asyncio.Event()
        self._stopped_at: float | None = None
        self._payload = PayloadStrategy(
            strategy=self.profile.strategy,
            method=self.profile.method,
            cookies=self.profile.cookies,
            query_buster=self.profile.query_buster,
        )

    def force_stop(self) -> None:
        self._stopped_at = time.monotonic()
        self._stop.set()

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if self.target.scheme != "https":
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_alpn_protocols(["h2", "http/1.1"])
        except NotImplementedError:
            pass
        return ctx

    async def _open(self, proxy: Proxy | None, ssl_ctx) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if proxy is None:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    self.target.host, self.target.port, ssl=ssl_ctx,
                    server_hostname=self.target.host if ssl_ctx else None,
                    limit=65536,
                ),
                timeout=self.timeout,
            )

        if proxy.scheme.startswith("socks"):
            reader, writer = await socks5_connect(
                proxy.host, proxy.port,
                self.target.host, self.target.port,
                proxy.username, proxy.password,
                timeout=self.timeout,
            )
            if ssl_ctx is not None:
                await writer.start_tls(ssl_ctx, server_hostname=self.target.host)
            return reader, writer

        # HTTP CONNECT tunnel through the proxy, then TLS inside if needed
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port, limit=65536),
            timeout=self.timeout,
        )
        dest = f"{self.target.host}:{self.target.port}"
        req = f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n"
        if proxy.username:
            cred = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
            req += f"Proxy-Authorization: Basic {cred}\r\n"
        req += "\r\n"
        writer.write(req.encode())
        await asyncio.wait_for(writer.drain(), timeout=self.timeout)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.timeout)
        if b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise ConnectionError(f"proxy CONNECT rejected: {head.split(b'\r\n',1)[0]!r}")
        if ssl_ctx is not None:
            await writer.start_tls(ssl_ctx, server_hostname=self.target.host)
        return reader, writer

    def _build_request(self) -> tuple[bytes, bool]:
        p = self.profile
        use_headers = self._payload.headers(self.target.netloc) if p.randomize_ua else {
            "Host": self.target.netloc,
            "User-Agent": "loadlab/1.1",
            "Connection": "keep-alive" if p.keepalive else "close",
        }
        body, ctype = (p.body, None)
        if not body and p.strategy != PayloadStrategy.BROWSER:
            body, ctype = self._payload.body()
        if ctype:
            use_headers["Content-Type"] = ctype
        for k, v in p.headers:
            use_headers[k] = v

        path = self._payload.path(self.target.path, p.method == "GET")
        hdr = "".join(f"{k}: {v}\r\n" for k, v in use_headers.items())
        if body:
            hdr += f"Content-Length: {len(body)}\r\n"
        line = f"{p.method} {path} HTTP/1.1\r\n{hdr}\r\n".encode()
        return line + body, use_headers.get("Connection") == "close"

    async def _one_request(self, writer, reader) -> tuple[bool, int, bool]:
        """Send one request, read exactly one response.

        Returns (success, status, reusable). `reusable` reflects the *server's*
        decision to keep the connection open, so the worker stops writing into
        a socket the peer has already closed.
        """
        request, _ = self._build_request()
        start = time.perf_counter()
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=self.timeout)

        try:
            resp = await read_response(reader, self.profile.method, self.timeout)
        except TruncatedBody as exc:
            self.metrics.record_error("TruncatedBody", time.perf_counter() - start)
            if DEBUG:
                print(f"[loadlab] truncated body on {self.target.netloc}: "
                      f"{exc.declared} declared, {exc.received} received", file=sys.stderr)
            return False, 0, False

        self.metrics.record(resp.status, time.perf_counter() - start, resp.total_bytes)
        if DEBUG:
            reuse = "reuse" if resp.keep_alive else "close"
            print(f"[loadlab] {resp.version} {resp.status} "
                  f"{len(resp.body)}B hdr={resp.header_bytes}B {reuse}", file=sys.stderr)
        return True, resp.status, resp.keep_alive

    async def _one_request_h2(self, conn, reader, writer) -> tuple[bool, int]:
        p = self.profile
        start = time.perf_counter()
        headers = [
            (":method", p.method),
            (":scheme", self.target.scheme),
            (":authority", self.target.netloc),
            (":path", self._payload.path(self.target.path, p.method == "GET")),
        ]
        extra = self._payload.headers(self.target.netloc) if p.randomize_ua else {}
        body = p.body
        if not body and p.strategy != PayloadStrategy.BROWSER:
            body, ctype = self._payload.body()
            if ctype:
                extra["content-type"] = ctype
        for k, v in p.headers:
            extra[k] = v
        for k, v in extra.items():
            if k.lower() in ("host", "connection", "transfer-encoding", "upgrade"):
                continue
            headers.append((k.lower(), v))

        resp = await conn.request(headers, body, timeout=self.timeout)
        latency = time.perf_counter() - start
        if resp.error:
            self.metrics.record_error(resp.error, latency)
            return False, 0
        size = len(resp.body) + sum(len(k) + len(v) for k, v in resp.headers)
        self.metrics.record(resp.status, latency, size)
        return True, resp.status

    async def worker(self, wid: int, stop: asyncio.Event | None = None) -> None:
        stop = stop or self._stop
        ssl_ctx = self._ssl_ctx()
        while not stop.is_set() and not self._stop.is_set():
            proxy = self.proxy_pool.acquire() if self.proxy_pool else None
            reader = writer = None
            opened = False
            try:
                reader, writer = await self._open(proxy, ssl_ctx)
                self.conn_stats.opened += 1
                self.metrics.open_connections += 1
                opened = True
                if proxy:
                    proxy.mark_success()

                h2conn = None
                if self.profile.http2:
                    h2conn = H2Connection(reader, writer)
                    await h2conn.handshake()

                reusable = True
                for _ in range(self.per_conn_requests):
                    if stop.is_set() or self._stop.is_set() or not reusable:
                        break
                    await self.limiter.acquire()
                    if self.adaptive is not None:
                        self.limiter.set_cap(self.adaptive.tick(self.metrics.snapshot()))
                    self.metrics.inflight += 1
                    attempt = 0
                    try:
                        while True:
                            try:
                                if h2conn is not None:
                                    ok, _ = await self._one_request_h2(h2conn, reader, writer)
                                    if not ok:
                                        break
                                else:
                                    ok, _, reusable = await self._one_request(writer, reader)
                                    if not ok:
                                        break
                                break
                            except (ConnectionError, OSError) as exc:
                                attempt += 1
                                if attempt > self.profile.max_retries:
                                    raise
                                self.metrics.record_error(f"retry:{type(exc).__name__}")
                                await asyncio.sleep(self.profile.retry_backoff * attempt)
                                reader, writer = await self._open(proxy, ssl_ctx)
                    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                        self.metrics.record_error(type(exc).__name__)
                        name = type(exc).__name__
                        if isinstance(exc, ConnectionRefusedError):
                            self.conn_stats.refused += 1
                        elif isinstance(exc, asyncio.TimeoutError):
                            self.conn_stats.timeouts += 1
                        elif isinstance(exc, ssl.SSLError):
                            self.conn_stats.tls_errors += 1
                        elif isinstance(exc, ConnectionResetError):
                            self.conn_stats.resets += 1
                        if proxy and name not in ("IncompleteReadError",):
                            proxy.mark_failure()
                        break
                    except asyncio.CancelledError:
                        raise
                    except RuntimeError:
                        break
                    except Exception as exc:
                        self.metrics.record_error(type(exc).__name__)
                        break
                    finally:
                        self.metrics.inflight -= 1
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                self.metrics.record_error(type(exc).__name__)
                self.metrics.reconnects += 1
                if proxy:
                    proxy.mark_failure()
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                self.metrics.reconnects += 1
                await asyncio.sleep(0.02)
            except Exception as exc:
                self.metrics.record_error(type(exc).__name__)
                self.metrics.reconnects += 1
                await asyncio.sleep(0.02)
            finally:
                if opened:
                    self.conn_stats.closed += 1
                    self.metrics.open_connections -= 1
                    self.metrics.reconnects += 1
                    try:
                        writer.close()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                            ConnectionError, OSError, RuntimeError):
                        pass

    async def _timed_stop(self, duration: float) -> None:
        await asyncio.sleep(duration)
        self.force_stop()

    async def run(self) -> None:
        from .scenario import StageDirector

        if self.scenario is not None:
            director = StageDirector(self.scenario, self.metrics)
            self._director = director

            async def factory(wid, stop):
                self.concurrency = director._target_conns or self.concurrency
                self.limiter.set_cap(director.metrics.rps_cap)
                await self.worker(wid, stop)

            timer = asyncio.create_task(self._timed_stop(self.duration))
            try:
                await director.run(factory)
            finally:
                timer.cancel()
            return

        stop_task = asyncio.create_task(self._timed_stop(self.duration))
        try:
            await asyncio.gather(
                *(self.worker(i) for i in range(self.concurrency)),
                return_exceptions=True,
            )
        finally:
            stop_task.cancel()
            self._stop.set()
