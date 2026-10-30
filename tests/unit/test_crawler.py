import asyncio
import time
from typing import Any

import pytest

from tests.support.fakes import CollectingSink, FakeFetcher, html
from web_crawler.crawler import Crawler, FatalCrawlError
from web_crawler.models import (
    CrawlConfig,
    CrawlStats,
    FetchedPage,
    FetchSkipped,
    RobotsFetch,
)
from web_crawler.rate_gate import RateGate
from web_crawler.urls import SiteScope

SEED = "https://site.test/"


async def crawl(
    fetcher: FakeFetcher, sink: CollectingSink | None = None, **overrides: Any
) -> tuple[CrawlStats, CollectingSink]:
    sink = sink or CollectingSink()
    config = CrawlConfig(seed=SEED, **overrides)
    crawler = Crawler(config, fetcher, sink, SiteScope(SEED))
    stats = await asyncio.wait_for(crawler.run(), timeout=10)
    return stats, sink


async def test_visits_every_reachable_page_exactly_once() -> None:
    fetcher = FakeFetcher(
        {
            SEED: html("/a", "/b", "/"),  # a self link
            "https://site.test/a": html("/c", "/b"),  # a diamond: a and b both lead to c
            "https://site.test/b": html("/c", "/"),  # a cycle back to the seed
            "https://site.test/c": html("/a"),  # and one more cycle
        }
    )
    stats, sink = await crawl(fetcher)

    assert sorted(fetcher.fetched) == sorted(set(fetcher.fetched))
    assert set(sink.urls) == {SEED, *(f"https://site.test/{p}" for p in "abc")}
    assert stats.crawled == 4
    assert stats.failed == 0


async def test_page_links_are_reported_in_document_order() -> None:
    fetcher = FakeFetcher(
        {SEED: html("/z", "/a", "/m"), **{f"https://site.test/{p}": "" for p in "zam"}}
    )
    _, sink = await crawl(fetcher)
    assert sink.links_of(SEED) == (
        "https://site.test/z",
        "https://site.test/a",
        "https://site.test/m",
    )


async def test_out_of_scope_links_are_listed_but_never_fetched() -> None:
    external = ("https://www.site.test/", "https://blog.site.test/x", "https://other.test/")
    fetcher = FakeFetcher({SEED: html("/in", *external), "https://site.test/in": ""})
    _, sink = await crawl(fetcher)

    assert set(fetcher.fetched) == {SEED, "https://site.test/in"}
    assert set(external) <= set(sink.links_of(SEED))


async def test_a_single_page_site_terminates() -> None:
    stats, sink = await crawl(FakeFetcher({SEED: "<p>nothing here</p>"}))
    assert sink.urls == [SEED]
    assert sink.results[0].links == ()
    assert stats.crawled == 1


async def test_max_pages_limits_what_is_scheduled() -> None:
    pages = {SEED: html(*(f"/p{i}" for i in range(20)))}
    pages.update({f"https://site.test/p{i}": "" for i in range(20)})
    fetcher = FakeFetcher(pages)
    stats, _ = await crawl(fetcher, max_pages=5)

    assert len(fetcher.fetched) == 5
    assert stats.crawled == 5


async def test_max_depth_zero_crawls_only_the_seed() -> None:
    fetcher = FakeFetcher({SEED: html("/a"), "https://site.test/a": ""})
    _, sink = await crawl(fetcher, max_depth=0)
    assert sink.urls == [SEED]
    assert sink.links_of(SEED) == ("https://site.test/a",)  # still listed


async def test_max_depth_stops_following_links_below_the_limit() -> None:
    fetcher = FakeFetcher(
        {
            SEED: html("/a"),
            "https://site.test/a": html("/b"),
            "https://site.test/b": html("/c"),
            "https://site.test/c": "",
        }
    )
    _, sink = await crawl(fetcher, max_depth=2)
    assert set(sink.urls) == {SEED, "https://site.test/a", "https://site.test/b"}


async def test_concurrency_never_exceeds_the_configured_limit() -> None:
    pages = {SEED: html(*(f"/p{i}" for i in range(30)))}
    pages.update({f"https://site.test/p{i}": "" for i in range(30)})
    fetcher = FakeFetcher(pages, delay=0.01)
    await crawl(fetcher, concurrency=4)
    assert 1 < fetcher.max_in_flight <= 4


async def test_a_failing_page_does_not_stop_the_crawl() -> None:
    fetcher = FakeFetcher({SEED: html("/missing", "/ok"), "https://site.test/ok": ""})
    stats, sink = await crawl(fetcher)

    assert set(sink.urls) == {SEED, "https://site.test/ok"}
    assert stats.failed == 1
    assert stats.crawled == 2


async def test_an_unexpected_error_is_counted_and_does_not_hang_the_crawl() -> None:
    fetcher = FakeFetcher(
        {SEED: html("/bad", "/ok"), "https://site.test/ok": ""},
        explode_on="https://site.test/bad",
    )
    stats, sink = await crawl(fetcher)
    assert set(sink.urls) == {SEED, "https://site.test/ok"}
    assert stats.failed == 1


async def test_many_pages_linking_to_one_url_cause_one_fetch() -> None:
    pages = {SEED: html(*(f"/hub{i}" for i in range(40)))}
    pages.update({f"https://site.test/hub{i}": html("/shared") for i in range(40)})
    pages["https://site.test/shared"] = ""
    fetcher = FakeFetcher(pages, delay=0.001)
    await crawl(fetcher, concurrency=20)
    assert fetcher.fetched.count("https://site.test/shared") == 1


async def test_http_and_https_forms_of_a_page_are_one_page() -> None:
    fetcher = FakeFetcher(
        {SEED: html("http://site.test/a", "https://site.test/a"), "https://site.test/a": ""}
    )
    await crawl(fetcher)
    assert fetcher.fetched.count("https://site.test/a") == 1


async def test_a_failing_seed_is_fatal() -> None:
    with pytest.raises(FatalCrawlError, match="cannot fetch the seed"):
        await crawl(FakeFetcher({}))


async def test_a_seed_that_redirects_off_site_is_fatal_and_names_the_target() -> None:
    fetcher = FakeFetcher(
        {SEED: FetchSkipped(SEED, "redirected off-site to https://www.site.test/")}
    )
    with pytest.raises(FatalCrawlError, match=r"redirected off-site to https://www\.site\.test/"):
        await crawl(fetcher)


async def test_a_same_host_redirect_is_reported_under_the_final_url() -> None:
    fetcher = FakeFetcher(
        {
            SEED: html("/old"),
            "https://site.test/old": FetchedPage("https://site.test/new", html("/")),
        }
    )
    _, sink = await crawl(fetcher)
    assert set(sink.urls) == {SEED, "https://site.test/new"}


async def test_a_redirect_onto_an_already_handled_page_is_not_reported_twice() -> None:
    fetcher = FakeFetcher(
        {
            SEED: html("/old", "/target"),
            "https://site.test/old": FetchedPage("https://site.test/target", "dup"),
            "https://site.test/target": "",
        },
        delay=0.001,
    )
    stats, sink = await crawl(fetcher, concurrency=1)
    assert sink.urls.count("https://site.test/target") == 1
    assert stats.skipped == 1


class TestRobots:
    async def test_disallowed_pages_are_listed_but_not_fetched(self) -> None:
        fetcher = FakeFetcher(
            {SEED: html("/private/x", "/ok"), "https://site.test/ok": ""},
            robots=RobotsFetch("ok", "User-agent: *\nDisallow: /private"),
        )
        stats, sink = await crawl(fetcher)
        assert "https://site.test/private/x" not in fetcher.fetched
        assert "https://site.test/private/x" in sink.links_of(SEED)
        assert stats.skipped == 1
        assert fetcher.robots_requests == ["https://site.test/robots.txt"]

    async def test_a_disallowed_seed_is_fatal(self) -> None:
        fetcher = FakeFetcher({}, robots=RobotsFetch("ok", "User-agent: *\nDisallow: /"))
        with pytest.raises(FatalCrawlError, match=r"disallowed by robots\.txt"):
            await crawl(fetcher)
        assert fetcher.fetched == []

    async def test_an_unreachable_robots_txt_stops_the_crawl(self) -> None:
        fetcher = FakeFetcher({SEED: ""}, robots=RobotsFetch("unreachable"))
        with pytest.raises(FatalCrawlError, match="--ignore-robots"):
            await crawl(fetcher)

    async def test_ignore_robots_skips_the_lookup_and_the_rules(self) -> None:
        fetcher = FakeFetcher(
            {SEED: html("/private"), "https://site.test/private": ""},
            robots=RobotsFetch("ok", "User-agent: *\nDisallow: /"),
        )
        _, sink = await crawl(fetcher, ignore_robots=True)
        assert fetcher.robots_requests == []
        assert "https://site.test/private" in sink.urls

    async def test_crawl_delay_spaces_out_the_requests(self) -> None:
        fetcher = FakeFetcher(
            {SEED: html("/a", "/b"), "https://site.test/a": "", "https://site.test/b": ""},
            robots=RobotsFetch("ok", "User-agent: *\nCrawl-delay: 0.05"),
        )
        gate = RateGate()

        # The gate is applied by the real fetcher; here we only check it gets configured.
        config = CrawlConfig(seed=SEED)
        crawler = Crawler(config, fetcher, CollectingSink(), SiteScope(SEED), gate)
        await crawler.run()

        started = time.monotonic()
        await gate.wait()
        await gate.wait()
        assert time.monotonic() - started >= 0.04


async def test_stop_winds_the_crawl_down_and_flags_it_as_interrupted() -> None:
    pages = {SEED: html(*(f"/p{i}" for i in range(200)))}
    pages.update({f"https://site.test/p{i}": "" for i in range(200)})
    fetcher = FakeFetcher(pages, delay=0.02)
    config = CrawlConfig(seed=SEED, concurrency=5)
    crawler = Crawler(config, fetcher, CollectingSink(), SiteScope(SEED))

    run = asyncio.create_task(crawler.run())
    await asyncio.sleep(0.1)
    crawler.stop()
    stats = await asyncio.wait_for(run, timeout=5)

    assert crawler.stopping
    assert stats.interrupted
    assert len(fetcher.fetched) < 201
