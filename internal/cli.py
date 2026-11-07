"""loadlab command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .httpflood import HttpFlooder, HttpProfile
from .metrics import Dashboard, Metrics
from .packetrain import PacketRain
from .report import write_reports
from .scope import ScopeError, build_target, parse_allowlist

BANNER = r"""
 _                 _ _           _
| | ___   __ _  __| | | __ _  __| |
| |/ _ \ / _` |/ _` | |/ _` |/ _` |
| | (_) | (_| | (_| | | (_| | (_| |
|_|\___/ \__,_|\__,_|_|\__,_|\__,_|
   load and packet-rate harness
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
    mode = args.cmd
    label = target.url if args.cmd == "http" else f"{target.resolved}:{target.port}/{args.mode}"
    dash = None if args.no_tui else Dashboard(label, mode)
    stop_ui = threading.Event()
    ui = threading.Thread(target=_run_dashboard, args=(metrics, stop_ui, dash), daemon=True)
    ui.start()

    try:
        if args.cmd == "http":
            profile = HttpProfile(
                method=args.method,
                headers=_parse_headers(args.header),
                body=args.body.encode(),
                randomize_ua=not args.no_random_ua,
            )
            engine = HttpFlooder(
                target, metrics,
                concurrency=args.concurrency,
                rps_limit=args.rps or None,
                duration=args.duration,
                profile=profile,
                per_conn_requests=args.per_conn,
            )
            asyncio.run(engine.run())
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

    meta = {"target": target.original, "resolved": target.resolved, "mode": mode}
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

