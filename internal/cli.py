"""loadlab command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .adaptive import AdaptiveController
from .httpflood import HttpFlooder, HttpProfile
from .metrics import Dashboard, Metrics
from .packetrain import PacketRain
from .payloads import PayloadStrategy
from .proxypool import ProxyPool
from .report import write_reports
from .scenario import Scenario
from .scope import ScopeError, build_target, parse_allowlist

BANNER = r"""
 _                 _ _           _
| | ___   __ _  __| | | __ _  __| |
| |/ _ \ / _` |/ _` | |/ _` |/ _` |
| | (_) | (_| | (_| | | (_| | (_| |
|_|\___/ \__,_|\__,_|_|\__,_|\__,_|
   load and packet-rate harness   v1.1
"""


def _parse_headers(raw: list[str]) -> tuple[tuple[str, str], ...]:
    out = []
    for item in raw:
        if ":" not in item:
            raise SystemExit(f"bad header (need 'Key: value'): {item!r}")
        k, v = item.split(":", 1)
        out.append((k.strip(), v.strip()))
    return tuple(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loadlab",
        description="HTTP/1.1 + HTTP/2 and packet-rate load harness for systems you own.",
        epilog="Every target must appear in --allow. There is no bypass flag.",
    )
    p.add_argument("--version", action="version", version=f"loadlab {__version__}")

    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--allow", action="append", default=[], required=True,
                        metavar="HOST",
                        help="repeatable allowlist entry: hostname, IP, or CIDR")
        sp.add_argument("--duration", type=float, default=30.0, help="run length in seconds")
        sp.add_argument("--out", default="reports", help="report output directory")
        sp.add_argument("--no-tui", action="store_true", help="disable live dashboard")

    h = sub.add_parser("http", help="HTTP request storm (keep-alive, rate shaped)")
    h.add_argument("target", help="scheme://host[:port]/path")
    common(h)
    h.add_argument("--concurrency", type=int, default=128, help="simultaneous connections")
    h.add_argument("--rps", type=int, default=0, help="global RPS cap (0 = unlimited)")
    h.add_argument("--per-conn", type=int, default=50, help="requests per connection before recycle")
    h.add_argument("--method", default="GET")
    h.add_argument("--header", action="append", default=[], metavar="'K: V'")
    h.add_argument("--body", default="")
    h.add_argument("--no-random-ua", action="store_true")
    h.add_argument("--timeout", type=float, default=8.0, help="per-operation socket timeout")

    h.add_argument("--scenario", metavar="SPEC",
                   help="stage spec: name:duration:concurrency[:rps[:ramp]] (comma separated)")
    h.add_argument("--preset", choices=["ramp", "spike", "flat"], default=None,
                   help="built-in scenario preset derived from --duration/--concurrency/--rps")
    h.add_argument("--adaptive", action="store_true",
                   help="AIMD controller hunts the failure knee instead of fixed RPS")
    h.add_argument("--adaptive-max", type=int, default=100_000, help="ceiling for adaptive RPS")
    h.add_argument("--adaptive-p95", type=float, default=400.0, help="latency budget in ms")

    h.add_argument("--proxy-file", metavar="PATH", help="proxy list, one per line")
    h.add_argument("--proxy-strategy", default="weighted",
                   choices=["weighted", "round_robin", "random", "least_used"])

    h.add_argument("--payload", default=PayloadStrategy.BROWSER,
                   choices=[PayloadStrategy.BROWSER, PayloadStrategy.JSON,
                            PayloadStrategy.FORM, PayloadStrategy.RANDOM],
                   help="per-request body/header strategy")
    h.add_argument("--no-cookies", action="store_true", help="omit random Cookie header")
    h.add_argument("--no-buster", action="store_true", help="omit cache-buster query params")
    h.add_argument("--retries", type=int, default=0, help="retries per request on transport error")
    h.add_argument("--retry-backoff", type=float, default=0.1)

    t = sub.add_parser("packet", help="TCP-connect / UDP datagram rate generator")
    t.add_argument("target", help="host[:port]")
    common(t)
    t.add_argument("--mode", choices=["tcp", "udp"], default="tcp")
    t.add_argument("--threads", type=int, default=64)
    t.add_argument("--pps", type=int, default=5000, help="hard packet-per-second cap")
    t.add_argument("--payload-size", type=int, default=256)
    return p


def _run_dashboard(metrics: Metrics, stop: threading.Event, dash: Dashboard | None) -> None:
    while not stop.is_set():
        metrics.roll()
        if dash:
            dash.render(metrics.snapshot())
        stop.wait(1.0)


def _build_scenario(args) -> Scenario | None:
    if args.scenario:
        try:
            return Scenario.from_spec(args.scenario)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}")
    if args.preset == "ramp":
        return Scenario.ramp_up(args.duration, args.concurrency, args.rps)
    if args.preset == "spike":
        return Scenario.spike(args.duration, args.concurrency, args.rps)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(BANNER)

    try:
        allow = parse_allowlist(args.allow)
        target = build_target(args.target, allow, default_scheme="http")
    except ScopeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    metrics = Metrics()
    label = target.url if args.cmd == "http" else f"{target.resolved}:{target.port}/{args.mode}"
    dash = None if args.no_tui else Dashboard(label, args.cmd)
    stop_ui = threading.Event()
    ui = threading.Thread(target=_run_dashboard, args=(metrics, stop_ui, dash), daemon=True)
    ui.start()

    meta = {"target": target.original, "resolved": target.resolved, "mode": args.cmd}
    try:
        if args.cmd == "http":
            proxy_pool = None
            if args.proxy_file:
                path = Path(args.proxy_file)
                if not path.exists():
                    print(f"error: proxy file not found: {path}", file=sys.stderr)
                    return 2
                proxy_pool = ProxyPool.from_file(path, args.proxy_strategy)
                print(f"  proxies loaded : {len(proxy_pool)} ({args.proxy_strategy})")
                meta["proxies"] = len(proxy_pool)

            profile = HttpProfile(
                method=args.method,
                headers=_parse_headers(args.header),
                body=args.body.encode(),
                randomize_ua=not args.no_random_ua,
                strategy=args.payload,
                cookies=not args.no_cookies,
                query_buster=not args.no_buster,
                max_retries=args.retries,
                retry_backoff=args.retry_backoff,
            )
            scenario = _build_scenario(args)
            if scenario:
                print("  scenario       : " + " -> ".join(
                    f"{s.name}({s.duration:.0f}s,c={s.concurrency})" for s in scenario.stages))
                meta["scenario"] = [
                    {"name": s.name, "duration": s.duration,
                     "concurrency": s.concurrency, "rps": s.rps} for s in scenario.stages
                ]
            adaptive = None
            if args.adaptive:
                adaptive = AdaptiveController(
                    start_rps=args.rps or max(args.concurrency * 5, 50),
                    max_rps=args.adaptive_max,
                    target_p95_ms=args.adaptive_p95,
                )
                print("  adaptive       : AIMD enabled, hunting failure knee")
                meta["adaptive"] = {"max_rps": args.adaptive_max, "p95_budget_ms": args.adaptive_p95}

            engine = HttpFlooder(
                target, metrics,
                concurrency=args.concurrency,
                rps_limit=args.rps or None,
                duration=args.duration,
                profile=profile,
                per_conn_requests=args.per_conn,
                proxy_pool=proxy_pool,
                adaptive=adaptive,
                scenario=scenario,
                timeout=args.timeout,
            )
            try:
                asyncio.run(engine.run())
            except KeyboardInterrupt:
                print("\ninterrupt - wrapping up")
            if adaptive:
                meta["adaptive_knee_rps"] = adaptive.knee()
                meta["adaptive_history"] = [
                    {"t": round(t, 2), "rps": r, "err_pct": e, "p95_ms": p}
                    for t, r, e, p in adaptive.history
                ]
            meta["connection_stats"] = engine.conn_stats.as_dict()
            if proxy_pool:
                meta["proxy_health"] = proxy_pool.health_report()
        else:
            engine = PacketRain(
                target, metrics,
                mode=args.mode,
                threads=args.threads,
                pps_limit=args.pps,
                duration=args.duration,
                payload_size=args.payload_size,
            )
            engine.run()
    except KeyboardInterrupt:
        print("\ninterrupt - wrapping up")
    finally:
        stop_ui.set()
        ui.join(timeout=2.0)
        metrics.roll()

    snap = metrics.snapshot()
    paths = write_reports(metrics, Path(args.out), meta)
    if dash:
        dash.finish(snap, paths)
    else:
        print(f"requests={snap['total']:,} failures={snap['total_errors']:,} avg_rps={snap['avg_rps']:.1f}")
        print("\n".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
