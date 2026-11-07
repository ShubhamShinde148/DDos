"""Scenario/stage orchestrator - ramp, plateau, spike, cooldown phases."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Stage:
    """One segment of a load scenario."""
    name: str
    duration: float
    concurrency: int
    rps: int = 0          # 0 = unlimited
    ramp: bool = False    # linearly interpolate from previous stage

    def profile(self, t: float) -> tuple[int, int]:
        """Return (concurrency, rps) at elapsed t within this stage."""
        if not self.ramp or self._prev is None:
            return self.concurrency, self.rps
        ratio = min(t / max(self.duration, 1e-9), 1.0)
        prev_c, prev_r = self._prev
        c = int(round(prev_c + (self.concurrency - prev_c) * ratio))
        r = int(round(prev_r + (self.rps - prev_r) * ratio)) if self.rps else 0
        return max(c, 1), max(r, 0)

    _prev: tuple[int, int] | None = field(default=None, repr=False)


@dataclass
class Scenario:
    stages: list[Stage]

    @classmethod
    def from_spec(cls, spec: str) -> "Scenario":
        """Parse 'name:duration:concurrency:rps[:ramp]' comma-separated."""
        stages: list[Stage] = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) < 3:
                raise ValueError(f"bad stage {chunk!r} - need name:duration:concurrency[:rps[:ramp]]")
            name = parts[0]
            duration = float(parts[1])
            concurrency = int(parts[2])
            rps = int(parts[3]) if len(parts) > 3 and parts[3] else 0
            ramp = len(parts) > 4 and parts[4].lower() in ("ramp", "1", "true")
            stages.append(Stage(name, duration, concurrency, rps, ramp))

        prev = None
        for st in stages:
            st._prev = prev
            prev = (st.concurrency, st.rps)
        return cls(stages)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.stages)

    def timeline(self) -> list[tuple[float, float, Stage]]:
        """(start, end, stage) triples."""
        out = []
        t = 0.0
        for st in self.stages:
            out.append((t, t + st.duration, st))
            t += st.duration
        return out

    def at(self, elapsed: float) -> tuple[Stage, int, int] | None:
        for start, end, st in self.timeline():
            if start <= elapsed < end:
                return st, *st.profile(elapsed - start)
        return None

    @classmethod
    def ramp_up(cls, duration: float, concurrency: int, rps: int) -> "Scenario":
        return cls([
            Stage("warmup", duration * 0.15, max(concurrency // 10, 1), rps // 10 if rps else 0, ramp=True),
            Stage("ramp", duration * 0.35, concurrency, rps, ramp=True),
            Stage("plateau", duration * 0.35, concurrency, rps),
            Stage("cooldown", duration * 0.15, max(concurrency // 10, 1), rps // 10 if rps else 0, ramp=True),
        ])

    @classmethod
    def spike(cls, duration: float, concurrency: int, rps: int) -> "Scenario":
        return cls([
            Stage("baseline", duration * 0.3, max(concurrency // 4, 1), rps // 4 if rps else 0),
            Stage("spike", duration * 0.2, concurrency * 4, rps * 4 if rps else 0, ramp=True),
            Stage("sustain", duration * 0.3, concurrency, rps),
            Stage("recover", duration * 0.2, max(concurrency // 4, 1), rps // 4 if rps else 0, ramp=True),
        ])


class StageDirector:
    """Drives the engine through a scenario, scaling workers up and down live."""

    def __init__(self, scenario: Scenario, metrics) -> None:
        self.scenario = scenario
        self.metrics = metrics
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._target_conns = 0
        self._closing: list[asyncio.Future] = []
        self.current_stage: str = "init"

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop

    def force_stop(self) -> None:
        self._stop.set()

    async def run(self, worker_factory) -> None:
        """worker_factory(wid, stop_event) -> coroutine that loops until stopped."""
        start = time.monotonic()
        next_id = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - start
            slot = self.scenario.at(elapsed)
            if slot is None:
                break
            stage, want_conns, want_rps = slot
            self.current_stage = stage.name
            self._target_conns = want_conns
            self.metrics.rps_cap = want_rps or None

            live = [t for t in self._tasks if not t.done()]
            while len(live) < want_conns:
                task = asyncio.create_task(worker_factory(next_id, self._stop))
                self._tasks.append(task)
                next_id += 1
                live.append(task)
            while len(live) > want_conns:
                victim = live.pop()
                victim.cancel()
            await asyncio.sleep(0.25)

        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
