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

```
python loadlab.py http http://127.0.0.1:8080/ --allow 127.0.0.1 \
    --concurrency 200 --rps 5000 --duration 60
```

```
python loadlab.py packet 10.0.0.5:5432 --allow 10.0.0.0/24 --mode tcp --pps 3000 --duration 20
```

## Scenario engine

Shape total pressure over time instead of hammering one fixed rate.

```
# built-in presets
--preset ramp     warmup -> linear ramp -> plateau -> cooldown
--preset spike    baseline -> 4x spike -> sustain -> recover

# explicit stages: name:duration:concurrency[:rps[:ramp]]
--scenario "warm:5:10:100,burst:10:200:5000:ramp,sustain:30:150:3000,cooldown:5:10:50:ramp"
```

The stage director adds and cancels workers live every 250ms. `ramp` linearly
interpolates concurrency and RPS from the previous stage.

## Adaptive control (AIMD)

```
--adaptive --adaptive-max 20000 --adaptive-p95 400
```

Instead of a fixed RPS, the controller ramps multiplicatively while error rate
stays under 2% and p95 stays under the latency budget, and cuts multiplicatively
when either blows past. It hunts the failure knee and records the highest rate
that held. Results land in the report as `adaptive_knee_rps` plus the full
per-second history.

## Proxy rotation

```
--proxy-file proxies.txt --proxy-strategy weighted
```

`proxies.txt` accepts any of:

```
1.2.3.4:8080
user:pass@host:3128
http://user:pass@1.2.3.4:8080
socks5://1.2.3.4:1080
```

Strategies: `weighted` (by success score), `round_robin`, `random`, `least_used`.
Dead proxies get a 15s cooldown after 3 failures so the pool self-heals. HTTP
proxies are tunneled with `CONNECT`; SOCKS5 entries are parsed and tracked but
need `python-socks` to actually dial.

## Payload realism

```
--payload browser|json|form|random
```

- `browser` - 10-user-agent pool, full fingerprinted header set (Sec-Fetch-*,
  sec-ch-ua, Accept-Language, Accept-Encoding), random Cookie, cache-buster query
- `json`     - JSON body + `application/json`
- `form`     - multipart/form-data with random boundary and fields
- `random`   - picks one of the above per request

Add `--no-cookies` / `--no-buster` to pin the shape. `--retries N` retries
transport failures up to N times with linear backoff.

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
| `--timeout S` | http | Per-operation socket timeout |
| `--scenario SPEC` | http | Explicit stage list |
| `--preset P` | http | `ramp` or `spike` derived from base flags |
| `--adaptive` | http | AIMD knee-hunting instead of fixed RPS |
| `--adaptive-max N` | http | RPS ceiling for the controller |
| `--adaptive-p95 MS` | http | Latency budget that triggers backoff |
| `--proxy-file PATH` | http | Proxy list |
| `--proxy-strategy S` | http | `weighted` / `round_robin` / `random` / `least_used` |
| `--payload P` | http | `browser` / `json` / `form` / `random` |
| `--no-cookies` | http | Drop random Cookie header |
| `--no-buster` | http | Drop cache-buster query params |
| `--retries N` | http | Retries per request on transport error |
| `--retry-backoff S` | http | Linear backoff multiplier |
| `--mode tcp\|udp` | packet | TCP connect vs UDP datagram |
| `--threads N` | packet | Worker threads |
| `--pps N` | packet | Hard packet-per-second cap |
| `--payload-size N` | packet | Datagram / connect payload bytes |

## Output

Live dashboard: current stage, rate cap, RPS + sparkline, bandwidth, p50/p95/p99,
error ratio, in-flight count, open connections, reconnects, status histogram,
top failure kinds.

Reports land in `reports/`:

- `loadlab-YYYYmmdd-HHMMSS.json` - summary, time series, connection stats,
  proxy health, adaptive history
- `loadlab-YYYYmmdd-HHMMSS.csv`  - per-second series for gnuplot / pandas / Excel

## Architecture

```
loadlab.py                entry point
internal/scope.py         allowlist enforcement, DNS pre-resolution
internal/httpflood.py     asyncio engine: keep-alive, proxy tunnel, retries
internal/scenario.py      stage director, ramp/spike profiles
internal/adaptive.py      AIMD congestion controller
internal/proxypool.py     proxy parsing, scoring, rotation, cooldown
internal/payloads.py      browser fingerprint / body / cookie generation
internal/packetrain.py    threaded TCP/UDP generator
internal/metrics.py       1s buckets, percentiles, ANSI dashboard
internal/report.py        JSON / CSV writers
internal/cli.py           argparse front end
```

## Notes

- HTTP/2 is negotiated via ALPN; request framing is HTTP/1.1. A strict h2-only
  origin will reject it - swap in `httpx[http2]`'s connector for true multiplexing.
- TLS verification is disabled for load runs on purpose.
- On Windows raise the ephemeral port range before large runs:
  `netsh int ipv4 set dynamicport tcp start=10000 num=55000`
