"""Target allowlist enforcement + URL/interface validation."""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass


class ScopeError(Exception):
    """Raised when a target is outside the operator-declared allowlist."""


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str
    original: str
    resolved: str

    @property
    def netloc(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.netloc}{self.path}"


def _resolve(host: str, port: int) -> str:
    try:
        info = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ScopeError(f"cannot resolve {host}:{port} ({exc})") from exc
    if not info:
        raise ScopeError(f"no address records for {host}")
    return info[0][4][0]


def parse_allowlist(entries: list[str]) -> list[str]:
    out = []
    for raw in entries:
        item = raw.strip().lower()
        if not item:
            continue
        if "/" in item:
            ipaddress.ip_network(item, strict=False)
        elif not item.replace(".", "").replace("-", "").replace(":", "").isalnum():
            raise ScopeError(f"malformed allowlist entry: {raw!r}")
        out.append(item)
    return out


def _host_in_scope(host: str, resolved: str, allow: list[str]) -> bool:
    host_l = host.lower()
    ip_obj = None
    try:
        ip_obj = ipaddress.ip_address(resolved)
    except ValueError:
        pass

    for entry in allow:
        if "/" in entry and ip_obj is not None:
            if ip_obj in ipaddress.ip_network(entry, strict=False):
                return True
            continue
        if entry.startswith("*."):
            suffix = entry[1:]
            if host_l.endswith(suffix):
                return True
            continue
        if host_l == entry:
            return True
        if ip_obj is not None and str(ip_obj) == entry:
            return True
    return False


def build_target(url: str, allow: list[str], default_scheme: str = "http") -> Target:
    if "://" not in url:
        url = f"{default_scheme}://{url}"

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ScopeError(f"unsupported scheme {parts.scheme!r} - http/https only")
    if not parts.hostname:
        raise ScopeError(f"no hostname in {url!r}")

    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    resolved = _resolve(host, port)
    if not _host_in_scope(host, resolved, allow):
        raise ScopeError(
            f"target {host} ({resolved}) is NOT in your allowlist.\n"
            f"  add it explicitly:  --allow {host}\n"
            f"  current allowlist:  {', '.join(allow) if allow else '<empty>'}"
        )

    return Target(
        scheme=parts.scheme,
        host=host,
        port=port,
        path=path,
        original=url,
        resolved=resolved,
    )
