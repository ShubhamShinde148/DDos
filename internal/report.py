"""Report writers: JSON summary and CSV time series."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from . import __version__
from .metrics import Metrics


def write_reports(metrics: Metrics, out_dir: Path, meta: dict) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = metrics.snapshot()

    json_path = out_dir / f"loadlab-{stamp}.json"
    payload = {
        "tool": "loadlab",
        "version": __version__,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "meta": meta,
        "summary": {
            "total_requests": snap["total"],
            "total_failures": snap["total_errors"],
            "total_bytes": snap["total_bytes"],
            "elapsed_s": round(snap["elapsed"], 3),
            "avg_rps": round(snap["avg_rps"], 2),
            "p50_ms": round(snap["p50"], 2),
            "p95_ms": round(snap["p95"], 2),
            "p99_ms": round(snap["p99"], 2),
            "status_histogram": snap["status"],
            "failure_histogram": snap["error_kinds"],
            "reconnects": snap["reconnects"],
        },
        "timeseries": [
            {
                "second": i,
                "requests": b.requests,
                "errors": b.errors,
                "bytes": b.bytes,
                "avg_latency_ms": round((sum(b.latencies) / len(b.latencies)) * 1000, 2) if b.latencies else 0.0,
            }
            for i, b in enumerate(metrics.buckets)
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    csv_path = out_dir / f"loadlab-{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["second", "requests", "errors", "bytes", "avg_latency_ms"])
        for i, b in enumerate(metrics.buckets):
            avg = (sum(b.latencies) / len(b.latencies)) * 1000 if b.latencies else 0.0
            writer.writerow([i, b.requests, b.errors, b.bytes, round(avg, 2)])

    return [str(json_path), str(csv_path)]
