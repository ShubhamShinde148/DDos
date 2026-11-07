# loadlab

HTTP/1.1 + HTTP/2 request storm and TCP/UDP packet-rate harness for capacity
testing systems you own. Pure Python 3.10+, no third-party dependencies.

## Install

```
cd C:\Users\shubh\Documents\loadlab
python --version        # 3.10+ required
```

No pip install needed - standard library only.

## Usage

Every run requires an explicit `--allow` entry. Targets outside the allowlist are
rejected before a single socket opens.

### HTTP storm

```
python loadlab.py http http://127.0.0.1:8080/ --allow 127.0.0.1 \
    --concurrency 200 --rps 5000 --duration 60
```

### HTTP with custom headers / method

```
python loadlab.py http https://staging.myapp.test/api/search?q=test \
    --allow staging.myapp.test \
    --method POST --header "Content-Type: application/json" \
    --body '{"q":"x"}' --concurrency 64 --rps 1000 --duration 30
```

### TCP connect rate test

```
python loadlab.py packet 10.0.0.5:5432 --allow 10.0.0.5 --mode tcp --threads 64 --pps 3000 --duration 20
```

### Whole subnet

```
python loadlab.py packet 10.0.0.7:80 --allow 10.0.0.0/24 --mode tcp --pps 2000
```

## CLI reference

| Flag | Applies to | Meaning |
|------|-----------|---------|
| `--allow HOST` | both | Repeatable allowlist: hostname, wildcard `*.dom.tld`, IP, or CIDR |
| `--duration N` | both | Run length in seconds (default 30) |
| `--out DIR` | both | Report directory (default `reports/`) |
| `--no-tui` | both | Plain output instead of live dashboard |
| `--concurrency N` | http | Simultaneous connections (default 128) |
| `--rps N` | http | Global token-bucket request cap (0 = unlimited) |
| `--per-conn N` | http | Requests per keep-alive connection before recycle |
| `--method M` | http | HTTP verb |
| `--header 'K: V'` | http | Repeatable extra header |
| `--body STR` | http | Request body |
| `--no-random-ua` | http | Pin a single User-Agent |
| `--mode tcp\|udp` | packet | TCP connect vs UDP datagram |
| `--threads N` | packet | Worker threads |
| `--pps N` | packet | Hard packet-per-second cap |
| `--payload-size N` | packet | Datagram / connect payload bytes |

## Output

Live dashboard shows current RPS, cumulative average, bandwidth, p50/p95/p99
latency, error ratio, in-flight count, open connections, reconnect count, and
per-status / per-error histograms.

On exit two files land in `reports/`:

- `loadlab-YYYYmmdd-HHMMSS.json` - full summary plus per-second time series
- `loadlab-YYYYmmdd-HHMMSS.csv`  - same series for gnuplot / pandas / Excel

## Architecture

```
loadlab.py            entry point
internal/scope.py     allowlist enforcement, DNS pre-resolution
internal/httpflood.py asyncio keep-alive storm + token-bucket limiter
internal/packetrain.py threaded TCP/UDP generator
internal/metrics.py   lock-guarded 1s buckets + ANSI dashboard
internal/report.py    JSON / CSV writers
internal/cli.py       argparse front end
```

## Notes

- HTTP/2 is negotiated via ALPN when the server advertises `h2`; the request
  path is still HTTP/1.1 framing, which most h2-terminating proxies accept on
  the initial connection. For pure h2 workloads compile `httpx[http2]` and swap
  the connector in `httpflood.py`.
- SSL verification is disabled for load runs - cert chains break under
  concurrency on purpose to measure that path.
- On Windows raise the ephemeral port range before large TCP runs:
  `netsh int ipv4 set dynamicport tcp start=10000 num=55000`
