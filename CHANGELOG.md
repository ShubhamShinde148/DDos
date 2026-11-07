# Changelog

## [1.3.1] - 2026-11-07

### Fixed

- **IncompleteReadError on ~50% of requests against HTTP/1.0 origin servers.**
  The worker loop assumed every connection would survive `--per-conn` requests.
  When the origin replied `HTTP/1.0` (no keep-alive, closes after each response)
  the client wrote the next request into a socket the peer had already closed
  and `readuntil` raised `IncompleteReadError`. Connection lifetime is now
  decided from the *response* per RFC 7230 6.3: HTTP/1.0 peers are persistent
  only with an explicit `Connection: keep-alive`, HTTP/1.1 peers are persistent
  unless they send `Connection: close`.
- **Truncated response bodies counted as successful 200s.** The old read loop
  broke on short reads without verifying `Content-Length` was satisfied, so a
  body cut short by the peer was recorded as a clean response. Short reads now
  raise `TruncatedBody` and land in the failure histogram.
- **`Transfer-Encoding: chunked` responses desynchronised the stream.** Only
  `Content-Length` was parsed. Chunked framing is now decoded, including the
  trailer section, and chunk-size parse errors raise `MalformedChunk` instead
  of silently corrupting subsequent reads.

### Added

- `internal/httpresp.py` - dedicated HTTP/1.x response parser owning status
  line, headers, framing rules (RFC 7230 3.3.3 priority order), chunked
  decoding, keep-alive decision, and body limits.
- `tests/test_httpresp.py` - 19 tests against real loopback sockets covering
  200/404/204/HEAD, empty and large bodies, truncation, chunked with trailers,
  malformed framing, HTTP/1.0 keep-alive opt-in, read-until-close, timeouts,
  and three sequential responses on one connection.
- `LOADLAB_DEBUG=1` environment variable emits per-response protocol traces
  (version, status, byte counts, reuse decision) to stderr.

### Changed

- `HttpFlooder._one_request` now returns `(success, status, reusable)` and the
  worker stops writing to a connection the peer has abandoned.
- Response framing limits: 256 KiB header block, 64 MiB body.

## [1.3.0] - 2026-11-07

### Fixed

- Header fingerprints. `Sec-Fetch-Site` is now `none` on cold navigation with no
  `Referer` and no `Cookie`, matching a real browser's first contact. Chrome
  fingerprint headers are no longer mixed with a Firefox User-Agent, which bot
  management scored as an impossible combination and dropped before responding.

### Changed

- `--cookies` and `--buster` are opt-in; previously random cookies and
  cache-buster query params were sent by default.

## [1.2.0] - 2026-11-07

### Added

- `internal/h2.py` - hand-rolled RFC 7540 framing and RFC 7541 HPACK.
- `internal/socks.py` - RFC 1928 SOCKS5 client with RFC 1929 auth.
- `--http2` flag, `socks5://` proxy entries, TLS inside SOCKS tunnels.

## [1.1.0] - 2026-11-07

### Added

- Scenario engine (`--scenario`, `--preset ramp|spike`) with live worker scaling.
- AIMD adaptive controller (`--adaptive`) hunting the failure knee.
- Proxy pool with scoring and rotation (`--proxy-file`, `--proxy-strategy`).
- Payload strategies (`--payload browser|json|form|random`).
- Retries with backoff, connection statistics.

## [1.0.0] - 2026-11-07

### Added

- Initial release: HTTP/1.1 keep-alive storm, TCP/UDP packet generator,
  allowlist scope enforcement, live ANSI dashboard, JSON/CSV reports.
