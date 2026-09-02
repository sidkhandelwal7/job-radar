"""Polite async HTTP: descriptive User-Agent, per-host concurrency limits, jittered backoff,
ETag / Last-Modified support, simple per-host circuit breaker. No proxies, no spoofing (§17)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("radar.http")

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class HostState:
    semaphore: asyncio.Semaphore
    failures: int = 0
    open_until: float = 0.0
    last_request_at: float = 0.0
    min_interval: float = 0.0  # seconds between requests to this host (politeness)


@dataclass
class Response:
    status: int
    content: bytes
    headers: dict[str, str]
    url: str
    final_url: str
    elapsed_ms: int
    not_modified: bool = False
    history: list[tuple[int, str]] = field(default_factory=list)

    def json(self) -> Any:
        import json

        return json.loads(self.content.decode("utf-8", errors="replace"))

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class RetryLater(Exception):
    """The host asked us to wait longer than one cycle can afford; the source stays due."""


class CircuitOpen(RuntimeError):
    pass


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        *,
        concurrency: int = 8,
        per_host_concurrency: int = 3,
        timeout: float = 30.0,
        max_retries: int = 3,
        circuit_threshold: int = 5,
        circuit_cooldown: float = 600.0,
        default_min_interval: float = 0.0,
    ) -> None:
        self.user_agent = user_agent
        self._global = asyncio.Semaphore(concurrency)
        self._per_host = per_host_concurrency
        self._hosts: dict[str, HostState] = {}
        self._timeout = timeout
        self._max_retries = max_retries
        self._circuit_threshold = circuit_threshold
        self._circuit_cooldown = circuit_cooldown
        self._default_min_interval = default_min_interval
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
            },
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=False,
            http2=False,
        )
        self.requests_made = 0
        self.bytes_downloaded = 0
        self.per_host_counts: dict[str, int] = defaultdict(int)

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_host_min_interval(self, host: str, seconds: float) -> None:
        self._host(host).min_interval = seconds

    def _host(self, host: str) -> HostState:
        st = self._hosts.get(host)
        if st is None:
            st = HostState(
                asyncio.Semaphore(self._per_host), min_interval=self._default_min_interval
            )
            self._hosts[host] = st
        return st

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        follow_redirects: bool = False,
        max_redirects: int = 5,
        retries: int | None = None,
    ) -> Response:
        host = urlparse(url).netloc
        st = self._host(host)
        now = time.monotonic()
        if st.open_until > now:
            raise CircuitOpen(f"circuit open for {host} for {int(st.open_until - now)}s")
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        attempts = (self._max_retries if retries is None else retries) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            async with self._global, st.semaphore:
                # politeness spacing per host
                wait = st.min_interval - (time.monotonic() - st.last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                st.last_request_at = time.monotonic()
                t0 = time.monotonic()
                try:
                    resp, history = await self._do(
                        method, url, hdrs, json, content, follow_redirects, max_redirects
                    )
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    last_exc = e
                    self._note_failure(st)
                    await self._backoff(attempt)
                    continue
            elapsed = int((time.monotonic() - t0) * 1000)
            self.requests_made += 1
            self.per_host_counts[host] += 1
            self.bytes_downloaded += len(resp.content)
            if resp.status_code in RETRYABLE and attempt < attempts - 1:
                self._note_failure(st)
                retry_after = resp.headers.get("Retry-After")
                await self._backoff(attempt, retry_after)
                continue
            if resp.status_code < 500:
                st.failures = 0
            return Response(
                status=resp.status_code,
                content=resp.content,
                headers={k.lower(): v for k, v in resp.headers.items()},
                url=url,
                final_url=str(resp.url),
                elapsed_ms=elapsed,
                not_modified=(resp.status_code == 304),
                history=history,
            )
        raise last_exc or RuntimeError(f"request failed: {method} {url}")

    async def _do(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json: Any,
        content: bytes | None,
        follow_redirects: bool,
        max_redirects: int,
    ) -> tuple[httpx.Response, list[tuple[int, str]]]:
        history: list[tuple[int, str]] = []
        current = url
        for _ in range(max_redirects + 1):
            resp = await self._client.request(
                method, current, headers=headers, json=json, content=content
            )
            if (
                follow_redirects
                and resp.status_code in (301, 302, 303, 307, 308)
                and resp.headers.get("location")
            ):
                history.append((resp.status_code, str(resp.url)))
                nxt = httpx.URL(current).join(resp.headers["location"])
                current = str(nxt)
                if resp.status_code == 303:
                    method, json, content = "GET", None, None
                continue
            return resp, history
        return resp, history

    def _note_failure(self, st: HostState) -> None:
        st.failures += 1
        if st.failures >= self._circuit_threshold:
            st.open_until = time.monotonic() + self._circuit_cooldown
            log.warning("circuit opened after %d failures", st.failures)

    MAX_RETRY_AFTER = 120.0

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(60.0, (2**attempt) + random.uniform(0, 1.0))
        if retry_after:
            with contextlib.suppress(ValueError):
                ra = float(retry_after)
                if ra > self.MAX_RETRY_AFTER:
                    # a server asking for an hour gets it — on the next cycle, not by parking this one
                    raise RetryLater(f"Retry-After {ra:.0f}s exceeds {self.MAX_RETRY_AFTER:.0f}s")
                delay = max(delay, ra)
        await asyncio.sleep(delay)

    async def get(self, url: str, **kw: Any) -> Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Response:
        return await self.request("POST", url, **kw)

    async def head(self, url: str, **kw: Any) -> Response:
        return await self.request("HEAD", url, **kw)
