"""Whole-crawler tests against an in-process site, plus the CLI as a subprocess."""

import asyncio
import json
import sys

import pytest
from aiohttp import web
from pytest_aiohttp import AiohttpServer

from tests.support.fakes import CollectingSink
from web_crawler.crawler import Crawler
from web_crawler.fetcher import AiohttpFetcher, make_session
from web_crawler.models import CrawlConfig, CrawlStats
from web_crawler.rate_gate import RateGate
from web_crawler.urls import SiteScope

ROBOTS = "User-agent: *\nDisallow: /private\nCrawl-delay: 0\n"


def html_response(body: str) -> web.Response:
    return web.Response(text=body, content_type="text/html")


def build_site(hits: dict[str, int]) -> web.Application:
    """index -> about, team, files, private, trap, old, missing, external links, ..."""

    async def handler(request: web.Request) -> web.StreamResponse:
        path = request.path
        hits[path] = hits.get(path, 0) + 1
        match path:
            case "/":
                return html_response(
                    """
                    <a href="/about">About</a>
                    <a href="team/">Team</a>
                    <a href="/files/report.pdf">PDF</a>
                    <a href="/private/area">Private</a>
                    <a href="/old">Old</a>
                    <a href="/missing">Missing</a>
                    <a href="/trap/0">Trap</a>
                    <a href="https://external.invalid/">External</a>
                    <a href="https://sub.external.invalid/">Sub</a>
                    <a href="mailto:hi@example.com">Mail</a>
                    <a href="/about#team">About again</a>
                    """
                )
            case "/about":
                return html_response('<a href="/">Home</a><a href="/team/">Team</a>')
            case "/team/":
                return html_response('<a href="/about">About</a><a href="/team/">Self</a>')
            case "/files/report.pdf":
                return web.Response(body=b"%PDF", content_type="application/pdf")
            case "/private/area":
                return html_response("<p>should never be fetched</p>")
            case "/old":
                raise web.HTTPMovedPermanently("/about")
            case "/robots.txt":
                return web.Response(text=ROBOTS, content_type="text/plain")
        if path.startswith("/trap/"):
            depth = int(path.rsplit("/", 1)[1])
            return html_response(f'<a href="/trap/{depth + 1}">deeper</a>')
        raise web.HTTPNotFound

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    return app


async def run_crawl(base: str, **overrides: object) -> tuple[CrawlStats, CollectingSink]:
    config = CrawlConfig(seed=base + "/", timeout=5, backoff_base=0.001, **overrides)  # type: ignore[arg-type]
    scope = SiteScope(config.seed)
    gate = RateGate()
    sink = CollectingSink()
    async with make_session(config) as session:
        fetcher = AiohttpFetcher(config, scope.contains, gate, session)
        stats = await asyncio.wait_for(Crawler(config, fetcher, sink, scope, gate).run(), 30)
    return stats, sink


@pytest.fixture
async def site(aiohttp_server: AiohttpServer) -> tuple[str, dict[str, int]]:
    hits: dict[str, int] = {}
    server = await aiohttp_server(build_site(hits))
    return f"http://{server.host}:{server.port}", hits


async def test_crawls_the_whole_site_once(site: tuple[str, dict[str, int]]) -> None:
    base, hits = site
    stats, sink = await run_crawl(base, max_pages=50)

    assert set(sink.urls) == {
        f"{base}/",
        f"{base}/about",
        f"{base}/team/",
        # the trap is cut off by max_pages, so only check it was entered
        *{url for url in sink.urls if "/trap/" in url},
    }
    # /old redirects to /about, which is already handled, so it is not reported twice
    assert sink.urls.count(f"{base}/about") == 1
    # No URL is requested twice, with one known exception: /old is only discovered to be a
    # redirect to /about by fetching it, so /about is requested once directly and once through
    # that redirect. The result is still reported once.
    assert {path: count for path, count in hits.items() if count > 1} == {"/about": 2}
    assert "/private/area" not in hits
    assert stats.failed >= 1  # /missing and the PDF


async def test_the_index_lists_every_link_in_document_order(
    site: tuple[str, dict[str, int]],
) -> None:
    base, _ = site
    _, sink = await run_crawl(base, max_pages=5)
    assert sink.links_of(f"{base}/") == (
        f"{base}/about",
        f"{base}/team/",
        f"{base}/files/report.pdf",
        f"{base}/private/area",
        f"{base}/old",
        f"{base}/missing",
        f"{base}/trap/0",
        "https://external.invalid/",
        "https://sub.external.invalid/",
    )


async def test_the_crawl_is_cut_off_by_max_pages(site: tuple[str, dict[str, int]]) -> None:
    base, _ = site
    stats, _ = await run_crawl(base, max_pages=4)
    assert stats.crawled + stats.failed + stats.skipped <= 4 + 1  # +1: the /old duplicate skip


async def test_the_trap_is_cut_off_by_max_depth(site: tuple[str, dict[str, int]]) -> None:
    base, hits = site
    await run_crawl(base, max_depth=3)
    assert max(int(p.rsplit("/", 1)[1]) for p in hits if p.startswith("/trap/")) == 2


async def test_ignore_robots_fetches_the_private_area(site: tuple[str, dict[str, int]]) -> None:
    base, hits = site
    await run_crawl(base, max_pages=20, ignore_robots=True)
    assert "/private/area" in hits
    assert "/robots.txt" not in hits


async def test_concurrent_workers_agree_on_the_result(site: tuple[str, dict[str, int]]) -> None:
    base, _ = site
    _, sequential = await run_crawl(base, max_depth=2, concurrency=1)
    _, parallel = await run_crawl(base, max_depth=2, concurrency=20)
    assert set(sequential.urls) == set(parallel.urls)
    for url in sequential.urls:
        assert sequential.links_of(url) == parallel.links_of(url)


class TestCommandLine:
    async def run_cli(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "web_crawler",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode is not None
        return process.returncode, out.decode("utf-8"), err.decode("utf-8")

    async def test_prints_pages_and_a_summary(self, site: tuple[str, dict[str, int]]) -> None:
        base, _ = site
        code, out, err = await self.run_cli(base + "/", "--max-depth", "1")

        assert code == 0
        assert f"{base}/about\n" in out
        assert f"    {base}/team/\n" in out
        assert "pages crawled" in err
        assert "pages crawled" not in out  # stdout is results only

    async def test_json_output(self, site: tuple[str, dict[str, int]]) -> None:
        base, _ = site
        code, out, _ = await self.run_cli(base + "/", "--max-depth", "1", "--json")

        assert code == 0
        records = [json.loads(line) for line in out.splitlines()]
        by_url = {record["url"]: record["links"] for record in records}
        assert f"{base}/team/" in by_url[f"{base}/about"]

    async def test_a_dead_seed_exits_1(self, site: tuple[str, dict[str, int]]) -> None:
        base, _ = site
        code, out, err = await self.run_cli(base + "/missing")
        assert code == 1
        assert out == ""
        assert "cannot fetch the seed" in err

    async def test_an_off_site_seed_redirect_exits_1(self, aiohttp_server: AiohttpServer) -> None:
        async def away(request: web.Request) -> web.StreamResponse:
            raise web.HTTPFound("http://localhost.invalid/")

        app = web.Application()
        app.router.add_get("/{tail:.*}", away)
        server = await aiohttp_server(app)

        code, _, err = await self.run_cli(f"http://{server.host}:{server.port}/", "--ignore-robots")
        assert code == 1
        assert "http://localhost.invalid/" in err

    async def test_a_bad_url_is_a_usage_error(self) -> None:
        code, _, err = await self.run_cli("example.com")
        assert code == 2
        assert "usage:" in err
