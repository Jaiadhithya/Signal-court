"""Polite HTTP client.

Per-host token bucket, retries with 2/4/8/16 s backoff on 429/5xx/connection errors,
one ``prime()`` re-try on 401/403, cross-host redirects, and a ``Blocked`` error for bot
walls and captchas. Captchas are never solved or bypassed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

BACKOFF_S = (2, 4, 8, 16)
RETRY_STATUS = {429, 500, 502, 503, 504}
BLOCK_MARKERS = (
    "access denied",
    "captcha",
    "are you a robot",
    "unusual traffic",
    "verify you are human",
    "request unsuccessful. incapsula",
    "pardon our interruption",
    "attention required! | cloudflare",
)


class Blocked(Exception):
    """A bot wall or captcha answered. The harvester logs it and moves on."""


class FetchLog(Protocol):
    def __call__(
        self,
        *,
        url: str,
        final_url: str,
        status: int,
        endpoint: str,
        params: Mapping[str, Any] | None,
        error: str | None,
    ) -> None: ...


class ListingCache(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes, ttl_s: int) -> None: ...


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: Mapping[str, str]
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.content.decode("utf-8-sig"))


class _Bucket:
    def __init__(self, rps: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.gap = 1.0 / rps if rps > 0 else 0.0
        self.next_at = 0.0
        self.clock, self.sleep = clock, sleep

    def wait(self) -> None:
        now = self.clock()
        if now < self.next_at:
            self.sleep(self.next_at - now)
            now = self.clock()
        self.next_at = max(now, self.next_at) + self.gap


def looks_blocked(content: bytes, content_type: str = "") -> bool:
    """Bot-wall HTML where data was expected. Skips real documents and JSON."""
    if not content or content[:5] == b"%PDF-" or content[:2] == b"PK":
        return False
    if "json" in content_type.lower():
        return False
    head = content[:6000].lower()
    if b"<html" not in head and b"<!doctype html" not in head:
        return False
    text = head.decode("utf-8", errors="ignore")
    return any(m in text for m in BLOCK_MARKERS)


def cache_key(endpoint: str, params: Mapping[str, Any] | None) -> str:
    import hashlib
    import json

    return hashlib.sha256((endpoint + "|" + json.dumps(params or {}, sort_keys=True, default=str)).encode()).hexdigest()


class PoliteClient:
    def __init__(
        self,
        ua: str,
        default_rps: float = 1.0,
        host_rps: dict[str, float] | None = None,
        timeout: float = 30.0,
        impersonate_hosts: Iterable[str] = (),
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        fetch_log: FetchLog | None = None,
        listing_cache: ListingCache | None = None,
    ) -> None:
        self.ua = ua
        self.default_rps = default_rps
        self.host_rps = dict(host_rps or {})
        self.impersonate_hosts = set(impersonate_hosts)
        self.clock, self.sleep = clock, sleep
        self.fetch_log = fetch_log
        self.listing_cache = listing_cache
        self._buckets: dict[str, _Bucket] = {}
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            transport=transport,
            http2=False,
            headers={"User-Agent": ua, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"},
        )

    def close(self) -> None:
        self._client.close()

    def _bucket(self, host: str) -> _Bucket:
        if host not in self._buckets:
            self._buckets[host] = _Bucket(self.host_rps.get(host, self.default_rps), self.clock, self.sleep)
        return self._buckets[host]

    def _log(self, url: str, final_url: str, status: int, params, error: str | None) -> None:
        if self.fetch_log:
            u = urlsplit(url)
            self.fetch_log(
                url=url, final_url=final_url, status=status, endpoint=u.netloc + u.path, params=params, error=error
            )

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        prime: Callable[[], None] | None = None,
        listing_ttl_s: int | None = None,
    ) -> Response:
        host = urlsplit(url).netloc
        ckey = None
        if listing_ttl_s and self.listing_cache is not None:
            ckey = cache_key(url, params)
            hit = self.listing_cache.get(ckey)
            if hit is not None:
                return Response(url=url, status=200, content=hit, headers={}, from_cache=True)
        primed = False
        last_err: str | None = None
        attempt = 0
        while True:
            self._bucket(host).wait()
            try:
                r = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
                self._log(url, url, 0, params, last_err)
                if attempt >= len(BACKOFF_S):
                    raise
                self.sleep(BACKOFF_S[attempt])
                attempt += 1
                continue
            status, final = r.status_code, str(r.url)
            if status in (401, 403) and prime is not None and not primed:
                primed = True
                self._log(url, final, status, params, "primed")
                prime()
                continue
            if status in RETRY_STATUS:
                self._log(url, final, status, params, "retry")
                if attempt >= len(BACKOFF_S):
                    r.raise_for_status()
                wait = BACKOFF_S[attempt]
                ra = r.headers.get("retry-after")
                if ra and ra.isdigit():
                    wait = max(wait, int(ra))
                self.sleep(wait)
                attempt += 1
                continue
            body = r.content
            if status in (401, 403) or looks_blocked(body, r.headers.get("content-type", "")):
                self._log(url, final, status, params, "blocked")
                raise Blocked(f"{host} answered {status} with a bot wall or refusal")
            self._log(url, final, status, params, None)
            r.raise_for_status()
            if ckey and self.listing_cache is not None and listing_ttl_s:
                self.listing_cache.put(ckey, body, listing_ttl_s)
            return Response(url=final, status=status, content=body, headers=dict(r.headers))
