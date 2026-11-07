"""Payload generators - evasion profiles and body/cookie variation."""
from __future__ import annotations

import random
import string

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

ACCEPT_POOL = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "application/json, text/plain, */*",
    "*/*",
]

ENCODING_POOL = ["gzip, deflate, br", "gzip, deflate", "gzip, deflate, br, zstd"]

LANG_POOL = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,de;q=0.7",
    "en-IN,en;q=0.9,hi;q=0.8",
]


def rand_token(n: int = 12, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(alphabet, k=n))


def browser_headers(host: str, method: str = "GET", accept_json: bool = False) -> dict[str, str]:
    """Full realistic browser header set with per-call randomization."""
    h = {
        "Host": host,
        "User-Agent": random.choice(UA_POOL),
        "Accept": random.choice(ACCEPT_POOL) if not accept_json else "application/json, text/plain, */*",
        "Accept-Language": random.choice(LANG_POOL),
        "Accept-Encoding": random.choice(ENCODING_POOL),
        "Cache-Control": random.choice(["no-cache", "max-age=0", "no-store"]),
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": random.choice(["document", "empty", "script"]),
        "Sec-Fetch-Mode": random.choice(["navigate", "cors", "no-cors"]),
        "Sec-Fetch-Site": random.choice(["none", "same-origin", "cross-site"]),
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
        "DNT": random.choice(["1", "0"]),
    }
    if "Chrome" in h["User-Agent"]:
        h["sec-ch-ua"] = '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"'
        h["sec-ch-ua-mobile"] = "?0"
        h["sec-ch-ua-platform"] = '"Windows"' if "Windows" in h["User-Agent"] else '"macOS"'
    if method in ("POST", "PUT", "PATCH"):
        h["Origin"] = f"https://{host}"
        h["Referer"] = f"https://{host}/"
    return h


def random_query(n: int = 4) -> str:
    """Cache-buster query string with random key/value pairs."""
    pairs = [f"{rand_token(5, string.ascii_lowercase)}{i}={rand_token(10)}" for i in range(n)]
    return "&".join(pairs)


def random_cookies(n: int = 3) -> str:
    return "; ".join(f"{rand_token(6, string.ascii_lowercase)}={rand_token(16)}" for _ in range(n))


def json_body() -> bytes:
    import json
    return json.dumps({
        "id": rand_token(8),
        "query": rand_token(12),
        "ts": random.randint(1700000000, 1900000000),
        "items": [rand_token(6) for _ in range(random.randint(1, 5))],
    }).encode()


def multipart_body(boundary: str | None = None) -> bytes:
    b = boundary or "----WebKitFormBoundary" + rand_token(16)
    parts = []
    for i in range(random.randint(1, 3)):
        parts.append(
            f"--{b}\r\nContent-Disposition: form-data; name=\"field{i}\"\r\n\r\n{rand_token(20)}\r\n"
        )
    parts.append(f"--{b}--\r\n")
    return "".join(parts).encode()


class PayloadStrategy:
    """Decides per-request header/body/cookie variation."""
    BROWSER = "browser"
    JSON = "json"
    FORM = "form"
    RANDOM = "random"

    def __init__(self, strategy: str = BROWSER, method: str = "GET",
                 cookies: bool = True, query_buster: bool = True) -> None:
        self.strategy = strategy
        self.method = method
        self.cookies = cookies
        self.query_buster = query_buster

    def headers(self, host: str) -> dict[str, str]:
        h = browser_headers(host, self.method, accept_json=self.strategy == self.JSON)
        if self.cookies:
            h["Cookie"] = random_cookies()
        return h

    def body(self) -> tuple[bytes, str | None]:
        strategy = self.strategy
        if strategy == self.RANDOM:
            strategy = random.choice([self.JSON, self.FORM, self.BROWSER])
        if strategy == self.JSON:
            return json_body(), "application/json"
        if strategy == self.FORM:
            boundary = "----WebKitFormBoundary" + rand_token(16)
            return multipart_body(boundary), f"multipart/form-data; boundary={boundary}"
        return b"", None

    def path(self, base: str, is_get: bool) -> str:
        if not (self.query_buster and is_get):
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{random_query()}"
