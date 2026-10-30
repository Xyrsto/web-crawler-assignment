"""AiohttpFetcher against a real in-process HTTP server (no internet)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web
from pytest_aiohttp import AiohttpServer

from web_crawler.fetcher import AiohttpFetcher, _parse_retry_after, make_session
from web_crawler.models import CrawlConfig, FetchedPage, FetchFailure, FetchSkipped
from web_crawler.rate_gate import RateGate

type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
type MakeFetcher = Callable[..., Awaitable[tuple[AiohttpFetcher, str]]]


@pytest.fixture
async def make_fetcher(aiohttp_server: AiohttpServer) -> AsyncIterator[MakeFetcher]:
    sessions: list[aiohttp.ClientSession] = []

    async def factory(
        routes: dict[str, Handler], **overrides: object
    ) -> tuple[AiohttpFetcher, str]:
        app = web.Application()
        for path, handler in routes.items():
            app.router.add_get(path, handler)
        server = await aiohttp_server(app)
        base = f"http://{server.host}:{server.port}"
        defaults = {"timeout": 2.0, "backoff_base": 0.001}
        config = CrawlConfig(seed=base + "/", **{**defaults, **overrides})  # type: ignore[arg-type]
        session = make_session(config)
        sessions.append(session)

        def in_scope(url: str) -> bool:
            return url.startswith(base + "/")

        return AiohttpFetcher(config, in_scope, RateGate(), session), base

    yield factory
    for session in sessions:
        await session.close()


def page(body: str = "<a href='/x'>x</a>", **kwargs: object) -> Handler:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(text=body, content_type="text/html", **kwargs)  # type: ignore[arg-type]

    return handler


async def test_fetches_html(make_fetcher: MakeFetcher) -> None:
    fetcher, base = await make_fetcher({"/": page("<p>hello</p>")})
    assert await fetcher.fetch(base + "/") == FetchedPage(base + "/", "<p>hello</p>")


async def test_decodes_using_the_declared_charset(make_fetcher: MakeFetcher) -> None:
    async def latin1(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body="café".encode("latin-1"), headers={"Content-Type": "text/html; charset=latin-1"}
        )

    fetcher, base = await make_fetcher({"/": latin1})
    assert await fetcher.fetch(base + "/") == FetchedPage(base + "/", "café")


async def test_an_unknown_charset_falls_back_to_utf8(make_fetcher: MakeFetcher) -> None:
    async def odd(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body="café".encode(), headers={"Content-Type": "text/html; charset=nonsense"}
        )

    fetcher, base = await make_fetcher({"/": odd})
    assert await fetcher.fetch(base + "/") == FetchedPage(base + "/", "café")


async def test_http_errors_are_failures_with_their_status(make_fetcher: MakeFetcher) -> None:
    async def gone(request: web.Request) -> web.StreamResponse:
        raise web.HTTPNotFound

    fetcher, base = await make_fetcher({"/": gone})
    assert await fetcher.fetch(base + "/") == FetchFailure(base + "/", "HTTP 404", 404)


async def test_non_html_is_rejected_from_the_headers(make_fetcher: MakeFetcher) -> None:
    async def pdf(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"%PDF-1.7", content_type="application/pdf")

    fetcher, base = await make_fetcher({"/": pdf})
    outcome = await fetcher.fetch(base + "/")
    assert isinstance(outcome, FetchFailure)
    assert "not HTML" in outcome.reason


async def test_xhtml_counts_as_html(make_fetcher: MakeFetcher) -> None:
    async def xhtml(request: web.Request) -> web.StreamResponse:
        return web.Response(text="<p/>", content_type="application/xhtml+xml")

    fetcher, base = await make_fetcher({"/": xhtml})
    assert isinstance(await fetcher.fetch(base + "/"), FetchedPage)


async def test_a_declared_oversized_body_is_refused(make_fetcher: MakeFetcher) -> None:
    fetcher, base = await make_fetcher({"/": page("x" * 5000)}, max_body_bytes=1000)
    outcome = await fetcher.fetch(base + "/")
    assert isinstance(outcome, FetchFailure)
    assert "too large" in outcome.reason


async def test_an_oversized_streamed_body_is_cut_off(make_fetcher: MakeFetcher) -> None:
    async def endless(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/html"})
        await response.prepare(request)  # chunked: no Content-Length to check up front
        try:
            for _ in range(1000):
                await response.write(b"x" * 1000)
        except ConnectionError:
            pass
        return response

    fetcher, base = await make_fetcher({"/": endless}, max_body_bytes=10_000)
    outcome = await fetcher.fetch(base + "/")
    assert isinstance(outcome, FetchFailure)
    assert "too large" in outcome.reason


async def test_server_errors_are_retried_until_they_succeed(make_fetcher: MakeFetcher) -> None:
    calls = 0

    async def flaky(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise web.HTTPInternalServerError
        return web.Response(text="ok", content_type="text/html")

    fetcher, base = await make_fetcher({"/": flaky})
    assert await fetcher.fetch(base + "/") == FetchedPage(base + "/", "ok")
    assert calls == 3


async def test_retries_are_bounded(make_fetcher: MakeFetcher) -> None:
    calls = 0

    async def broken(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        raise web.HTTPServiceUnavailable

    fetcher, base = await make_fetcher({"/": broken})
    outcome = await fetcher.fetch(base + "/")
    assert isinstance(outcome, FetchFailure)
    assert "HTTP 503" in outcome.reason
    assert calls == 3  # the first attempt plus two retries


async def test_client_errors_are_not_retried(make_fetcher: MakeFetcher) -> None:
    calls = 0

    async def forbidden(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        raise web.HTTPForbidden

    fetcher, base = await make_fetcher({"/": forbidden})
    await fetcher.fetch(base + "/")
    assert calls == 1


async def test_429_honours_retry_after(make_fetcher: MakeFetcher) -> None:
    calls = 0

    async def limited(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        return web.Response(text="ok", content_type="text/html")

    fetcher, base = await make_fetcher({"/": limited})
    assert isinstance(await fetcher.fetch(base + "/"), FetchedPage)
    assert calls == 2


async def test_retry_after_is_capped(make_fetcher: MakeFetcher) -> None:
    calls = 0

    async def limited(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.Response(status=503, headers={"Retry-After": "3600"})
        return web.Response(text="ok", content_type="text/html")

    fetcher, base = await make_fetcher({"/": limited}, retry_after_cap=0.01)
    outcome = await asyncio.wait_for(fetcher.fetch(base + "/"), timeout=5)
    assert isinstance(outcome, FetchedPage)


async def test_a_slow_response_times_out(make_fetcher: MakeFetcher) -> None:
    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(2)
        return web.Response(text="late", content_type="text/html")

    fetcher, base = await make_fetcher({"/": slow}, timeout=0.1, max_retries=0)
    outcome = await fetcher.fetch(base + "/")
    assert isinstance(outcome, FetchFailure)
    assert "timeout" in outcome.reason


async def test_a_slow_body_times_out_too(make_fetcher: MakeFetcher) -> None:
    async def drip(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/html"})
        await response.prepare(request)
        try:
            for _ in range(100):
                await response.write(b"x")
                await asyncio.sleep(0.05)
        except ConnectionError:
            pass
        return response

    fetcher, base = await make_fetcher({"/": drip}, timeout=0.2, max_retries=0)
    outcome = await asyncio.wait_for(fetcher.fetch(base + "/"), timeout=3)
    assert isinstance(outcome, FetchFailure)
    assert "timeout" in outcome.reason


async def test_a_refused_connection_is_a_failure_not_an_exception() -> None:
    config = CrawlConfig(seed="http://127.0.0.1:1/", timeout=1, max_retries=0)
    async with make_session(config) as session:
        fetcher = AiohttpFetcher(config, lambda _: True, RateGate(), session)
        outcome = await fetcher.fetch("http://127.0.0.1:1/")
    assert isinstance(outcome, FetchFailure)
    # Windows keeps retrying a refused connection for ~2 s, so a short timeout fires first.
    assert outcome.reason.startswith(("connection error", "timeout"))


class TestRedirects:
    async def test_same_host_redirects_are_followed(self, make_fetcher: MakeFetcher) -> None:
        async def old(request: web.Request) -> web.StreamResponse:
            raise web.HTTPMovedPermanently("/new")

        fetcher, base = await make_fetcher({"/old": old, "/new": page("<p>new</p>")})
        assert await fetcher.fetch(base + "/old") == FetchedPage(base + "/new", "<p>new</p>")

    async def test_off_site_redirects_are_not_followed(self, make_fetcher: MakeFetcher) -> None:
        async def away(request: web.Request) -> web.StreamResponse:
            raise web.HTTPFound("http://other.invalid/landing")

        fetcher, base = await make_fetcher({"/": away})
        outcome = await fetcher.fetch(base + "/")
        assert outcome == FetchSkipped(
            base + "/", "redirected off-site to http://other.invalid/landing"
        )

    async def test_a_redirect_loop_gives_up(self, make_fetcher: MakeFetcher) -> None:
        async def loop(request: web.Request) -> web.StreamResponse:
            raise web.HTTPFound("/")

        fetcher, base = await make_fetcher({"/": loop})
        outcome = await fetcher.fetch(base + "/")
        assert isinstance(outcome, FetchFailure)
        assert "redirects" in outcome.reason

    async def test_a_redirect_without_a_location_is_a_failure(
        self, make_fetcher: MakeFetcher
    ) -> None:
        async def nowhere(request: web.Request) -> web.StreamResponse:
            return web.Response(status=302)

        fetcher, base = await make_fetcher({"/": nowhere})
        assert isinstance(await fetcher.fetch(base + "/"), FetchFailure)


class TestRobots:
    async def test_returns_the_text(self, make_fetcher: MakeFetcher) -> None:
        async def robots(request: web.Request) -> web.StreamResponse:
            return web.Response(text="User-agent: *\nDisallow: /x", content_type="text/plain")

        fetcher, base = await make_fetcher({"/robots.txt": robots})
        result = await fetcher.fetch_robots(base + "/robots.txt")
        assert (result.state, result.text) == ("ok", "User-agent: *\nDisallow: /x")

    async def test_a_missing_file_means_missing(self, make_fetcher: MakeFetcher) -> None:
        fetcher, base = await make_fetcher({"/": page()})
        assert (await fetcher.fetch_robots(base + "/robots.txt")).state == "missing"

    async def test_a_server_error_means_unreachable(self, make_fetcher: MakeFetcher) -> None:
        async def broken(request: web.Request) -> web.StreamResponse:
            raise web.HTTPInternalServerError

        fetcher, base = await make_fetcher({"/robots.txt": broken}, max_retries=0)
        assert (await fetcher.fetch_robots(base + "/robots.txt")).state == "unreachable"

    async def test_a_huge_file_is_truncated_not_rejected(self, make_fetcher: MakeFetcher) -> None:
        async def huge(request: web.Request) -> web.StreamResponse:
            return web.Response(text="#" * 2_000_000, content_type="text/plain")

        fetcher, base = await make_fetcher({"/robots.txt": huge})
        result = await fetcher.fetch_robots(base + "/robots.txt")
        assert result.state == "ok"
        assert len(result.text) == 512 * 1024

    async def test_an_off_site_redirect_is_treated_as_missing(
        self, make_fetcher: MakeFetcher
    ) -> None:
        async def away(request: web.Request) -> web.StreamResponse:
            raise web.HTTPFound("http://other.invalid/robots.txt")

        fetcher, base = await make_fetcher({"/robots.txt": away})
        assert (await fetcher.fetch_robots(base + "/robots.txt")).state == "missing"


class TestRetryAfterParsing:
    def test_seconds(self) -> None:
        assert _parse_retry_after("7") == 7.0
        assert _parse_retry_after("0") == 0.0

    def test_http_dates_in_the_past_mean_no_wait(self) -> None:
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    @pytest.mark.parametrize("value", [None, "", "soon", "-5x"])
    def test_junk_is_ignored(self, value: str | None) -> None:
        assert _parse_retry_after(value) is None

    def test_negative_seconds_clamp_to_zero(self) -> None:
        assert _parse_retry_after("-5") == 0.0
