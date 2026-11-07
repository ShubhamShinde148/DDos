"""Adaptive controller - adjusts RPS from observed error rate and latency."""
from __future__ import annotations

import time


class AdaptiveController:
    """AIMD-style congestion control against the target's actual capacity.

    If error rate stays low and p95 latency is healthy, ramp RPS up
    multiplicatively. If errors spike or latency exceeds budget, cut
    multiplicatively. This finds the failure knee instead of blindly
    hammering a fixed number.
    """

    def __init__(
        self,
        start_rps: int = 200,
        min_rps: int = 20,
        max_rps: int = 100_000,
        target_p95_ms: float = 400.0,
        err_ceiling: float = 2.0,
        probe_after: float = 3.0,
    ) -> None:
        self.rps = float(start_rps)
        self.min_rps = min_rps
        self.max_rps = max_rps
        self.target_p95 = target_p95_ms
        self.err_ceiling = err_ceiling
        self.probe_after = probe_after
        self._last_probe = time.monotonic()
        self._last_errors = 0
        self._last_requests = 0
        self.history: list[tuple[float, int, float, float]] = []

    def _window(self, snap: dict) -> tuple[float, float]:
        total = max(snap["total"], 1)
        deltas = max(snap["total"] - self._last_errors, 0)
        self._last_errors = snap["total"]
        err_pct = (deltas / total) * 100.0 if total else 0.0
        return err_pct, snap["p95"]

    def tick(self, snap: dict) -> int:
        now = time.monotonic()
        if now - self._last_probe < 1.0:
            return int(self.rps)
        self._last_probe = now

        err_pct, p95 = self._window(snap)
        healthy = err_pct < self.err_ceiling and p95 < self.target_p95

        if healthy:
            self.rps = min(self.max_rps, self.rps * 1.15)
        else:
            # back off proportionally to how badly we're failing
            severity = max(err_pct / max(self.err_ceiling, 0.1), p95 / max(self.target_p95, 1.0))
            factor = max(0.35, 1.0 / (1.0 + severity * 0.35))
            self.rps = max(self.min_rps, self.rps * factor)

        self.history.append((now, int(self.rps), round(err_pct, 2), round(p95, 1)))
        return int(self.rps)

    def knee(self) -> int:
        """Best RPS observed before error rate climbed above the ceiling."""
        for _, rps, err, _ in reversed(self.history):
            if err < self.err_ceiling:
                return rps
        return int(self.rps)
