"""Payload generators - evasion profiles and body/cookie variation.

Header sets are modelled on what a *real* browser sends on a cold first
navigation, because the impossible combinations (random cookies on a never-
visited domain, Sec-Fetch-Site: same-origin with no Referer, JSON Accept on
a document request) are exactly what bot-management scores on.
"""
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

# must match the UA that was picked - Chrome UAs want Sec-Fetch + CH headers,
# Firefox and Safari send a different set entirely
ACCEPT_NAV = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
ACCEPT_NAV_FIREFOX = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_JSON = "application/json, text/plain, */*"

ENC_CHROME = "gzip, deflate, br, zstd"
ENC_FIREFOX = "gzip, deflate, br"
ENC_SAFARI = "gzip, deflate, br"

LANG_POOL = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,de;q=0.7",
    "en-IN,en;q=0.9,hi;q=0.8",
]


def rand_token(n: int = 12, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(alphabet, k=n))


def _ua_family(ua: str) -> str:
    if "Firefox" in ua:
        return "firefox"
    if "Edg/" in ua:
        return "edge"
    if "Chrome" in ua and "Safari" in ua:
        return "chrome"
    if "Safari" in ua:
        return "safari"
    return "chrome"


def browser_headers(
    host: str,
    method: str = "GET",
    accept_json: bool = False,
    navigation: bool = True,
    with_cookies: bool = False,
) -> dict[str, str]:
    """Header set for a cold first navigation.

    navigation=True models the first hit on a site: Sec-Fetch-Site is `none`,
    no Referer, no cookies unless explicitly requested. That is the shape a
    real browser produces and the one bot management expects.
    """
    ua = random.choice(UA_POOL)
    family = _ua_family(ua)
    lang = random.choice(LANG_POOL)

    h: dict[str, str] = {
        "Host": host,
        "User-Agent": ua,
        "Accept": ACCEPT_JSON if accept_json else (
            ACCEPT_NAV_FIREFOX if family == "firefox" else ACCEPT_NAV),
        "Accept-Language": lang,
        "Accept-Encoding": ENC_FIREFOX if family == "firefox" else (
            ENC_SAFARI if family == "safari" else ENC_CHROME),
        "Connection": "keep-alive",
    }

    if navigation:
        h["Upgrade-Insecure-Requests"] = "1"
        h["Sec-Fetch-Dest"] = "document"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "none"
        h["Sec-Fetch-User"] = "?1"

    if family in ("chrome", "edge"):
        brand = '"Microsoft Edge";v="131"' if family == "edge" else '"Google Chrome";v="131"'
        h["sec-ch-ua"] = f'"Chromium";v="131", "Not_A Brand";v="24", {brand}'
        h["sec-ch-ua-mobile"] = "?1" if any(x in ua for x in ("Android", "iPhone")) else "?0"
        h["sec-ch-ua-platform"] = (
            '"Windows"' if "Windows" in ua else
            '"macOS"' if "Macintosh" in ua else
            '"Android"' if "Android" in ua else
            '"iOS"' if "iPhone" in ua or "iPad" in ua else '"Linux"')

    if with_cookies:
        h["Cookie"] = random_cookies()

    if method in ("POST", "PUT", "PATCH") and not accept_json:
        h["Origin"] = f"https://{host}"
        h["Referer"] = f"https://{host}/"
        h["Sec-Fetch-Site"] = "same-origin"

    return h


def random_query(n: int = 4) -> str:
    return "&".join(
        f"{rand_token(6, string.ascii_lowercase)}{i}={rand_token(10)}" for i in range(n)
    )


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
    """Decides per-request header/body/cookie variation.

    Defaults are intentionally conservative: a cold-navigation browser shape
    with no cookies. Aggressive variation is opt-in.
    """
    BROWSER = "browser"
    JSON = "json"
    FORM = "form"
    RANDOM = "random"

    def __init__(
        self,
        strategy: str = BROWSER,
        method: str = "GET",
        cookies: bool = False,
        query_buster: bool = False,
        warm: bool = False,
    ) -> None:
        self.strategy = strategy
        self.method = method
        self.cookies = cookies
        self.query_buster = query_buster
        self.warm = warm

    def headers(self, host: str) -> dict[str, str]:
        is_json = self.strategy == self.JSON
        navigation = not is_json
        return browser_headers(
            host, self.method,
            accept_json=is_json,
            navigation=navigation,
            with_cookies=self.cookies,
        )

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
