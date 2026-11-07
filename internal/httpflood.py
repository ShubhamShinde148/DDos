"""HTTP/1.1 + HTTP/2 request storm engine with rate shaping."""
from __future__ import annotations

import asyncio
import random
import ssl
import time
from dataclasses import dataclass

from .metrics import Metrics
from .scope import Target

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
]


@dataclass
class HttpProfile:
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    randomize_ua: bool = True
    http2: bool = False
    keepalive: bool = True

    def build_headers(self, target: Target) -> dict[str, str]:
        base = {
            "Host": target.netloc,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive" if self.keepalive else "close",
        }
        base["User-Agent"] = random.choice(USER_AGENTS) if self.randomize_ua else USER_AGENTS[0]
        for k, v in self.headers:
            base[k] = v
        return base


class HttpFlooder:
    """asyncio keep-alive storm with a token-bucket RPS limiter."""

    def __init__(
        self,
        target: Target,
        metrics: Metrics,
        concurrency: int = 128,
        rps_limit: int | None = None,
        duration: float = 30.0,
        profile: HttpProfile | None = None,
        per_conn_requests: int = 50,
    ) -> None:
        self.target = target
        self.metrics = metrics
        self.concurrency = concurrency
        self.rps_limit = rps_limit
        self.duration = duration
        self.profile = profile or HttpProfile()
        self.per_conn_requests = max(1, per_conn_requests)
        self._stop = asyncio.Event()
        self._tokens = float(rps_limit) if rps_limit else 0.0
        self._last_refill = time.monotonic()
        self._rate_lock = asyncio.Lock()

    async def _acquire(self) -> None:
        if not self.rps_limit:
            return
        async with self._rate_lock:
            now = time.monotonic()
            self._tokens = min(
                float(self.rps_limit),
                self._tokens + (now - self._last_refill) * self.rps_limit,
            )
            self._last_refill = now
            if self._tokens < 1.0:
                await self._rate_lock.release()
                try:
                    await asyncio.sleep((1.0 - self._tokens) / self.rps_limit)
                finally:
                    await self._rate_lock.acquire()
                now = time.monotonic()
                self._tokens = min(
                    float(self.rps_limit),
                    self._tokens + (now - self._last_refill) * self.rps_limit,
                )
                self._last_refill = now
            self._tokens -= 1.0

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

    async def _timed_stop(self) -> None:
        await asyncio.sleep(self.duration)
        self._stop.set()

    async def worker(self, wid: int) -> None:
        headers = self.profile.build_headers(self.target)
        hdr_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        if self.profile.body:
            hdr_blob += f"Content-Length: {len(self.profile.body)}\r\n"
        request = f"{self.profile.method} {self.target.path} HTTP/1.1\r\n{hdr_blob}\r\n".encode() + self.profile.body

        while not self._stop.is_set():
            wrote_conn = False
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self.target.host,
                        self.target.port,
                        ssl=self._ssl_ctx(),
                        server_hostname=self.target.host if self.target.scheme == "https" else None,
                        limit=65536,
                    ),
                    timeout=8.0,
                )
                self.metrics.open_connections += 1
                wrote_conn = True

                for _ in range(self.per_conn_requests):
                    if self._stop.is_set():
                        break
                    await self._acquire()
                    self.metrics.inflight += 1
                    start = time.perf_counter()
                    try:
                        writer.write(request)
                        await asyncio.wait_for(writer.drain(), timeout=8.0)
                        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=8.0)
                        status_line = head.split(b"\r\n", 1)[0]
                        try:
                            status = int(status_line.split(b" ", 2)[1])
                        except (IndexError, ValueError):
                            status = 0
                        size = len(head)
                        clen = 0
                        for line in head.split(b"\r\n")[1:]:
                            if line.lower().startswith(b"content-length:"):
                                try:
                                    clen = int(line.split(b":", 1)[1].strip())
                                except ValueError:
                                    clen = 0
                                break
                        if clen:
                            size += len(await asyncio.wait_for(reader.read(min(clen, 1 << 20)), timeout=8.0))
                        latency = time.perf_counter() - start
                        self.metrics.record(status, latency, size)
                        if headers.get("Connection") == "close":
                            break
                    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                        self.metrics.record_error(type(exc).__name__, time.perf_counter() - start)
                        break
                    except asyncio.CancelledError:
                        raise
                    except RuntimeError:
                        break
                    except Exception as exc:
                        self.metrics.record_error(type(exc).__name__, time.perf_counter() - start)
                        break
                    finally:
                        self.metrics.inflight -= 1
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                self.metrics.record_error(type(exc).__name__)
                self.metrics.reconnects += 1
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
                if wrote_conn:
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

    async def run(self) -> None:
        stop_task = asyncio.create_task(self._timed_stop())
        try:
            await asyncio.gather(*(self.worker(i) for i in range(self.concurrency)), return_exceptions=True)
        finally:
            stop_task.cancel()
            self._stop.set()



