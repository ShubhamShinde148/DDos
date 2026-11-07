"""Live metrics collection and terminal rendering."""
from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass, field


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
MAGENTA = "\x1b[35m"
BG_DARK = "\x1b[48;5;236m"


@dataclass
class Bucket:
    requests: int = 0
    errors: int = 0
    bytes: int = 0
    latencies: list[float] = field(default_factory=list)


class Metrics:
    """Thread-safe rolling collector with 1-second buckets."""

    def __init__(self, window: int = 60) -> None:
        self._lock = threading.Lock()
        self.window = window
        self.buckets: list[Bucket] = [Bucket()]
        self.started = time.monotonic()
        self.total_requests = 0
        self.total_errors = 0
        self.total_bytes = 0
        self.inflight = 0
        self.open_connections = 0
        self.reconnects = 0
        self.status_counts: dict[int, int] = {}
        self.error_counts: dict[str, int] = {}
        self._last_roll = self.started

    def record(self, status: int, latency: float, size: int) -> None:
        with self._lock:
            b = self.buckets[-1]
            b.requests += 1
            b.bytes += size
            b.latencies.append(latency)
            self.total_requests += 1
            self.total_bytes += size
            self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def record_error(self, kind: str, latency: float = 0.0) -> None:
        with self._lock:
            b = self.buckets[-1]
            b.requests += 1
            b.errors += 1
            if latency:
                b.latencies.append(latency)
            self.total_requests += 1
            self.total_errors += 1
            self.error_counts[kind] = self.error_counts.get(kind, 0) + 1

    def roll(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._last_roll < 1.0:
                return
            self.buckets.append(Bucket())
            self._last_roll = now
            if len(self.buckets) > self.window:
                self.buckets.pop(0)

    def snapshot(self) -> dict:
        with self._lock:
            b = self.buckets[-1]
            all_lat = [x for bucket in self.buckets for x in bucket.latencies]
            pct = {}
            if all_lat:
                ordered = sorted(all_lat)
                for name, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
                    idx = min(len(ordered) - 1, int(q * len(ordered)))
                    pct[name] = ordered[idx] * 1000.0
            elapsed = max(time.monotonic() - self.started, 1e-9)
            return {
                "rps": b.requests,
                "avg_rps": self.total_requests / elapsed,
                "errors": b.errors,
                "bytes": b.bytes,
                "total": self.total_requests,
                "total_errors": self.total_errors,
                "total_bytes": self.total_bytes,
                "inflight": self.inflight,
                "open_conns": self.open_connections,
                "reconnects": self.reconnects,
                "elapsed": elapsed,
                "p50": pct.get("p50", 0.0),
                "p95": pct.get("p95", 0.0),
                "p99": pct.get("p99", 0.0),
                "status": dict(self.status_counts),
                "error_kinds": dict(self.error_counts),
            }


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"


def _bar(value: float, peak: float, width: int = 34) -> str:
    if peak <= 0:
        return ""
    filled = int(round(width * min(value / peak, 1.0)))
    return "#" * filled + "." * (width - filled)


class Dashboard:
    """Curses-free ANSI renderer: repaints a fixed block each second."""

    LINES = 14

    def __init__(self, target: str, mode: str, peak_rps: float = 0.0) -> None:
        self.target = target
        self.mode = mode
        self.peak_rps = peak_rps
        self._drawn = False

    def _clear(self) -> None:
        if self._drawn:
            print(f"\x1b[{self.LINES}A", end="")

    def render(self, snap: dict) -> None:
        rps, errors = snap["rps"], snap["errors"]
        self.peak_rps = max(self.peak_rps, float(rps))
        err_ratio = (errors / rps * 100.0) if rps else 0.0
        err_color = GREEN if err_ratio < 1 else (YELLOW if err_ratio < 10 else RED)

        self._clear()
        w = 62
        print(f"{BG_DARK}{BOLD}{CYAN} loadlab {RESET}{BG_DARK}{DIM} | {self.mode} | {self.target[:38]}{RESET}{' ' * 6}")
        print(f"{BG_DARK} {'-' * w} {RESET}")
        print(f"{BG_DARK} throughput   {BOLD}{MAGENTA}{rps:>8,} rps{RESET}{BG_DARK}   avg {snap['avg_rps']:>9,.1f}   {DIM}{_bar(rps, self.peak_rps)}{RESET}{' ' * 8}")
        print(f"{BG_DARK} bandwidth    {BOLD}{CYAN}{_human_bytes(snap['bytes']):>8}/s{RESET}{BG_DARK}   total {_human_bytes(snap['total_bytes']):>9}{RESET}{' ' * 22}")
        print(f"{BG_DARK} latency      p50 {snap['p50']:>7.1f}ms  p95 {snap['p95']:>7.1f}ms  p99 {snap['p99']:>7.1f}ms{RESET}{' ' * 12}")
        print(f"{BG_DARK} errors       {err_color}{errors:>6,}{RESET}{BG_DARK} this sec ({err_ratio:5.1f}%)  total {snap['total_errors']:,}{RESET}{' ' * 10}")
        print(f"{BG_DARK} in-flight    {snap['inflight']:>6,}   conns {snap['open_conns']:>4}   reconnects {snap['reconnects']:>6,}{RESET}{' ' * 12}")
        print(f"{BG_DARK} requests     {snap['total']:>10,}   elapsed {snap['elapsed']:>7.1f}s{RESET}{' ' * 18}")

        statuses = snap["status"]
        if statuses:
            parts = []
            for code in sorted(statuses):
                color = GREEN if 200 <= code < 300 else (YELLOW if 300 <= code < 500 else RED)
                parts.append(f"{color}{code}{RESET}{BG_DARK}:{statuses[code]:,}")
            print(f"{BG_DARK} status       {'  '.join(parts)}{RESET}{' ' * (w - min(len(parts) * 12, w))}")

        kinds = snap["error_kinds"]
        if kinds:
            top = sorted(kinds.items(), key=lambda kv: -kv[1])[:4]
            body = "  ".join(f"{k}:{v:,}" for k, v in top)
            print(f"{BG_DARK} {RED}failures{RESET}{BG_DARK}     {body:<55}{RESET}")
        else:
            print(f"{BG_DARK} {DIM}failures     none{RESET}{BG_DARK}{' ' * 46}{RESET}")

        print(f"{BG_DARK} {'-' * w} {RESET}")
        print(f"{BG_DARK} {DIM}ctrl-c to stop and write report{RESET}{' ' * 35}")
        self._drawn = True

    def finish(self, snap: dict, report_paths: list[str]) -> None:
        print()
        print(f"{BOLD}{GREEN} run complete {RESET}")
        print(f"  total requests : {snap['total']:,}")
        print(f"  failures       : {snap['total_errors']:,}")
        print(f"  transferred    : {_human_bytes(snap['total_bytes'])}")
        print(f"  elapsed        : {snap['elapsed']:.1f}s")
        print(f"  avg throughput : {snap['avg_rps']:,.1f} rps")
        for p in report_paths:
            print(f"  report         : {p}")

