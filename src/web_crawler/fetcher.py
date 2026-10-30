"""HTTP fetching: redirects, retries, size cap, content-type check.

`Fetcher` is the boundary the crawler core depends on, so the core can be tested with an
in-memory fake. `AiohttpFetcher` is the real implementation.
"""

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urljoin

import aiohttp

from web_crawler.models import (
    CrawlConfig,
    FetchedPage,
    FetchFailure,
    FetchOutcome,
    FetchSkipped,
    RobotsFetch,
)
from web_crawler.rate_gate import RateGate
from web_crawler.robots import RobotsSource
from web_crawler.urls import normalize_url

_CHUNK_SIZE = 64 * 1024
_ROBOTS_MAX_BYTES = 512 * 1024  # RFC 9309: parse at least 500 KiB
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"


class Fetcher(RobotsSource, Protocol):
    """Downloads pages, and (through `RobotsSource`) robots.txt files."""

    async def fetch(self, url: str) -> FetchOutcome: ...


@dataclass(frozen=True, slots=True)
class _Body:
    """A downloaded body decoded to text: an HTML page, or a robots.txt file."""

    url: str
    text: str


@dataclass(frozen=True, slots=True)
class _Redirect:
    location: str


@dataclass(frozen=True, slots=True)
class _Transient:
    """A failure worth retrying."""

    reason: str
    retry_after: float | None = None


type _Answer = _Body | FetchFailure | _Redirect
type _Attempt = _Answer | _Transient
type _Followed = _Body | FetchFailure | FetchSkipped


def make_session(config: CrawlConfig) -> aiohttp.ClientSession:
    """One shared session: pooled keep-alive connections capped at the worker count."""
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=config.concurrency, ttl_dns_cache=300),
        timeout=aiohttp.ClientTimeout(total=config.timeout),
        headers={"User-Agent": config.user_agent, "Accept": _ACCEPT},
    )


class AiohttpFetcher:
    def __init__(
        self,
        config: CrawlConfig,
        in_scope: Callable[[str], bool],
        gate: RateGate,
        session: aiohttp.ClientSession,
    ) -> None:
        self._config = config
        self._in_scope = in_scope
        self._gate = gate
        self._session = session

    async def fetch(self, url: str) -> FetchOutcome:
        outcome = await self._follow(url, html_only=True)
        if isinstance(outcome, _Body):
            return FetchedPage(outcome.url, outcome.text)
        return outcome

    async def fetch_robots(self, url: str) -> RobotsFetch:
        outcome = await self._follow(url, html_only=False)
        match outcome:
            case _Body(text=text):
                return RobotsFetch("ok", text)
            case FetchFailure(status=status) if status is not None and 400 <= status < 500:
                return RobotsFetch("missing")
            case FetchSkipped():
                return RobotsFetch("missing")  # redirected away: nothing usable, nothing forbidden
            case _:
                return RobotsFetch("unreachable")

    async def _follow(self, url: str, *, html_only: bool) -> _Followed:
        """Follow same-host redirects by hand, so the host is checked at every hop."""
        current = url
        for _ in range(self._config.max_redirects + 1):
            result = await self._with_retries(current, html_only=html_only)
            if isinstance(result, _Redirect):
                target = _redirect_target(current, result.location)
                if target is None:
                    return FetchFailure(url, f"invalid redirect to {result.location!r}")
                if not self._in_scope(target):
                    return FetchSkipped(url, f"redirected off-site to {target}")
                current = target
            elif isinstance(result, _Body):
                return result
            else:
                return replace(result, url=url)
        return FetchFailure(url, f"more than {self._config.max_redirects} redirects")

    async def _with_retries(self, url: str, *, html_only: bool) -> _Answer:
        attempts = self._config.max_retries + 1
        transient = _Transient("no attempt made")
        for attempt in range(attempts):
            await self._gate.wait()
            result = await self._attempt(url, html_only=html_only)
            if not isinstance(result, _Transient):
                return result
            transient = result
            if attempt + 1 < attempts:
                await asyncio.sleep(self._retry_delay(attempt, transient.retry_after))
        return FetchFailure(url, f"{transient.reason} (gave up after {attempts} attempts)")

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._config.retry_after_cap)
        base = self._config.backoff_base
        return base * 2.0**attempt + random.uniform(0, base)

    async def _attempt(self, url: str, *, html_only: bool) -> _Attempt:
        try:
            async with self._session.get(url, allow_redirects=False) as response:
                return await self._read(url, response, html_only=html_only)
        except TimeoutError:
            return _Transient("timeout")
        except aiohttp.InvalidURL:
            return FetchFailure(url, "invalid URL")
        except aiohttp.ClientError as exc:
            return _Transient(f"connection error ({type(exc).__name__})")

    async def _read(
        self, url: str, response: aiohttp.ClientResponse, *, html_only: bool
    ) -> _Attempt:
        status = response.status
        if 300 <= status < 400:
            location = response.headers.get("Location")
            if location:
                return _Redirect(location)
            return FetchFailure(url, f"HTTP {status} without Location", status)
        if status == 429 or status >= 500:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            return _Transient(f"HTTP {status}", retry_after)
        if status >= 400:
            return FetchFailure(url, f"HTTP {status}", status)

        if html_only:
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip().lower() not in _HTML_TYPES:
                return FetchFailure(url, f"not HTML ({content_type or 'no content type'})")

        limit = self._config.max_body_bytes if html_only else _ROBOTS_MAX_BYTES
        if html_only and response.content_length is not None and response.content_length > limit:
            return FetchFailure(url, f"too large (over {limit} bytes)")
        body = bytearray()
        async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > limit:
                if html_only:
                    return FetchFailure(url, f"too large (over {limit} bytes)")
                del body[limit:]
                break
        return _Body(url, _decode(bytes(body), response.charset))


def _redirect_target(current: str, location: str) -> str | None:
    try:
        return normalize_url(urljoin(current, location))
    except ValueError:
        return None


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:  # the server named a charset Python does not know
        return body.decode("utf-8", errors="replace")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())
