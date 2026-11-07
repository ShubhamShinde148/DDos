"""Proxy pool with health tracking, rotation, and per-proxy scoring."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Proxy:
    raw: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    successes: int = 0
    failures: int = 0
    last_used: float = 0.0
    cooldown_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def parse(cls, line: str) -> "Proxy | None":
        line = line.strip().lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            return None

        if "://" not in line:
            parts = line.split(":")
            if len(parts) == 2:
                host, port = parts
                return cls(line, "http", host, int(port))
            if len(parts) == 4:
                host, port, user, pw = parts
                return cls(line, "http", host, int(port), user, pw)
            return None

        u = urlparse(line)
        if not u.hostname:
            return None
        return cls(
            line,
            u.scheme or "http",
            u.hostname,
            u.port or (1080 if (u.scheme or "").startswith("socks") else 8080),
            u.username,
            u.password,
        )

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    @property
    def score(self) -> float:
        total = self.successes + self.failures
        if total == 0:
            return 0.5
        return self.successes / total

    def mark_success(self) -> None:
        with self._lock:
            self.successes += 1
            self.last_used = time.monotonic()

    def mark_failure(self, cooldown: float = 15.0) -> None:
        with self._lock:
            self.failures += 1
            self.last_used = time.monotonic()
            if self.failures > 2:
                self.cooldown_until = time.monotonic() + cooldown


class ProxyPool:
    """Round-robin / weighted-random / least-used rotation over a proxy file."""

    def __init__(self, proxies: list[Proxy], strategy: str = "weighted") -> None:
        self.proxies = proxies
        self.strategy = strategy
        self._idx = 0
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path, strategy: str = "weighted") -> "ProxyPool":
        entries = []
        for line in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            p = Proxy.parse(line)
            if p:
                entries.append(p)
        return cls(entries, strategy)

    def __len__(self) -> int:
        return len(self.proxies)

    def acquire(self) -> Proxy | None:
        if not self.proxies:
            return None
        with self._lock:
            healthy = [p for p in self.proxies if p.healthy] or self.proxies
            if self.strategy == "round_robin":
                self._idx = (self._idx + 1) % len(healthy)
                return healthy[self._idx]
            if self.strategy == "random":
                return random.choice(healthy)
            if self.strategy == "least_used":
                return min(healthy, key=lambda p: p.successes + p.failures)
            # weighted by score
            weights = [max(p.score, 0.05) for p in healthy]
            return random.choices(healthy, weights=weights, k=1)[0]

    def health_report(self) -> list[dict]:
        return [
            {
                "proxy": p.key,
                "scheme": p.scheme,
                "successes": p.successes,
                "failures": p.failures,
                "score": round(p.score, 3),
                "healthy": p.healthy,
            }
            for p in sorted(self.proxies, key=lambda x: -x.score)
        ]

