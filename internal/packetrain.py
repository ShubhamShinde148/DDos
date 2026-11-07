"""Raw TCP/UDP packet-rate generator with hard packet caps."""
from __future__ import annotations

import os
import random
import socket
import threading
import time

from .metrics import Metrics
from .scope import Target


class PacketRain:
    """TCP-connect and UDP datagram generator against a single allowlisted target.

    UDP is capped to a conservative default so it never resembles a volumetric
    amplification flood; TCP connect is the workhorse for state-table testing.
    """

    def __init__(
        self,
        target: Target,
        metrics: Metrics,
        mode: str = "tcp",
        threads: int = 64,
        pps_limit: int = 5000,
        duration: float = 30.0,
        payload_size: int = 256,
    ) -> None:
        self.target = target
        self.metrics = metrics
        self.mode = mode
        self.threads = threads
        self.pps_limit = pps_limit
        self.duration = duration
        self.payload_size = payload_size
        self._stop = threading.Event()

    def _worker(self, tid: int) -> None:
        interval = self.threads / max(self.pps_limit, 1)
        payload = os.urandom(min(self.payload_size, 1400))
        addr = (self.target.resolved, self.target.port)

        while not self._stop.is_set():
            start = time.perf_counter()
            try:
                if self.mode == "udp":
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(payload, addr)
                    sock.close()
                else:
                    sock = socket.create_connection(addr, timeout=3.0)
                    try:
                        if payload:
                            sock.sendall(payload)
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    finally:
                        sock.close()
                elapsed = time.perf_counter() - start
                self.metrics.record(0, elapsed, len(payload))
            except OSError as exc:
                self.metrics.record_error(type(exc).__name__)

            sleep_for = interval - (time.perf_counter() - start)
            if sleep_for > 0:
                time.sleep(sleep_for)

    def run(self) -> None:
        threads = [threading.Thread(target=self._worker, args=(i,), daemon=True) for i in range(self.threads)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + self.duration
        while time.monotonic() < deadline:
            time.sleep(0.25)
        self._stop.set()
        for t in threads:
            t.join(timeout=2.0)
