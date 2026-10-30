import asyncio

import pytest

from web_crawler.frontier import CrawlTask, Frontier
from web_crawler.robots import RobotsPolicy
from web_crawler.urls import SiteScope

SEED = "https://site.test/"
UA = "web-crawler/0.1.0"
NO_PRIVATE = RobotsPolicy.parse("User-agent: *\nDisallow: /private", UA)


def frontier(max_pages: int | None = None, *, robots: RobotsPolicy | None = None) -> Frontier:
    if robots is None:
        return Frontier(SiteScope(SEED), max_pages)
    return Frontier(SiteScope(SEED), max_pages, may_crawl=robots.allowed)


async def take_one(queue: Frontier) -> CrawlTask:
    """Take the oldest page and finish with it straight away."""
    async with queue.take() as task:
        return task


async def is_empty(queue: Frontier) -> bool:
    """True if no page is waiting. `take()` would block forever on an empty frontier."""
    waiting = asyncio.create_task(take_one(queue))
    done, _ = await asyncio.wait({waiting}, timeout=0.05)
    waiting.cancel()
    return not done


async def test_added_pages_come_out_in_order_with_their_depth() -> None:
    queue = frontier()
    queue.add("https://site.test/a", 1)
    queue.add("https://site.test/b", 2)

    assert await take_one(queue) == CrawlTask("https://site.test/a", 1)
    assert await take_one(queue) == CrawlTask("https://site.test/b", 2)


async def test_a_page_is_queued_only_once() -> None:
    queue = frontier()
    queue.add("https://site.test/a", 1)
    queue.add("https://site.test/a", 1)
    queue.add("https://site.test/a#section", 1)  # same page, fragment dropped
    queue.add("http://site.test/a", 1)  # same page, other scheme

    assert await take_one(queue) == CrawlTask("https://site.test/a", 1)
    assert await is_empty(queue)


async def test_out_of_scope_pages_are_ignored() -> None:
    queue = frontier()
    queue.add("https://www.site.test/", 1)
    queue.add("https://other.test/", 1)
    queue.add("mailto:someone@site.test", 1)

    assert await is_empty(queue)


async def test_max_pages_stops_further_pages_being_queued() -> None:
    queue = frontier(max_pages=3)
    for name in "abcde":
        queue.add(f"https://site.test/{name}", 1)

    urls = [(await take_one(queue)).url for _ in range(3)]
    assert urls == [f"https://site.test/{name}" for name in "abc"]
    assert await is_empty(queue)


async def test_pages_refused_by_the_rule_are_counted_not_queued() -> None:
    queue = frontier(robots=NO_PRIVATE)

    queue.add("https://site.test/private/x", 1)
    queue.add("https://site.test/private/x", 1)  # seen already, not counted twice
    queue.add("https://site.test/public", 1)

    assert queue.disallowed == 1
    assert (await take_one(queue)).url == "https://site.test/public"
    assert await is_empty(queue)


async def test_refused_pages_do_not_use_up_the_page_limit() -> None:
    queue = frontier(max_pages=1, robots=NO_PRIVATE)

    queue.add("https://site.test/private", 1)
    queue.add("https://site.test/public", 1)

    assert (await take_one(queue)).url == "https://site.test/public"


async def test_the_rule_is_asked_with_the_canonical_url() -> None:
    asked: list[str] = []

    def rule(url: str) -> bool:
        asked.append(url)
        return True

    queue = Frontier(SiteScope(SEED), may_crawl=rule)
    queue.add("http://SITE.test/a#section", 1)

    assert asked == ["https://site.test/a"]


async def test_close_refuses_further_pages() -> None:
    queue = frontier()
    queue.add("https://site.test/a", 1)
    queue.close()
    queue.add("https://site.test/b", 1)

    assert (await take_one(queue)).url == "https://site.test/a"
    assert await is_empty(queue)


class TestClaim:
    def test_a_new_page_can_be_claimed_once(self) -> None:
        queue = frontier()
        assert queue.claim("https://site.test/new")
        assert not queue.claim("https://site.test/new")

    def test_a_page_that_was_already_added_cannot_be_claimed(self) -> None:
        queue = frontier()
        queue.add("https://site.test/a", 1)
        assert not queue.claim("https://site.test/a")

    async def test_a_claimed_page_is_not_queued_afterwards(self) -> None:
        queue = frontier()
        queue.claim("https://site.test/a")
        queue.add("https://site.test/a", 1)
        assert queue.disallowed == 0
        assert await is_empty(queue)

    def test_a_refused_page_cannot_be_claimed(self) -> None:
        queue = frontier(robots=NO_PRIVATE)
        assert not queue.claim("https://site.test/private")


class TestTake:
    async def test_a_page_is_unfinished_until_its_block_ends(self) -> None:
        queue = frontier()
        queue.add("https://site.test/a", 1)
        queue.add("https://site.test/b", 1)
        waiter = asyncio.create_task(queue.wait_until_finished())

        async with queue.take():
            async with queue.take():
                await asyncio.sleep(0)
                assert not waiter.done()  # both pages are in their blocks
            await asyncio.sleep(0)
            assert not waiter.done()  # one block has ended, one has not

        await asyncio.wait_for(waiter, timeout=1)

    async def test_a_page_is_finished_even_if_its_block_raises(self) -> None:
        queue = frontier()
        queue.add("https://site.test/a", 1)

        with pytest.raises(RuntimeError):
            async with queue.take():
                raise RuntimeError("boom")

        await asyncio.wait_for(queue.wait_until_finished(), timeout=1)

    async def test_links_added_inside_the_block_keep_the_wait_going(self) -> None:
        queue = frontier()
        queue.add("https://site.test/a", 1)
        waiter = asyncio.create_task(queue.wait_until_finished())

        async with queue.take():
            queue.add("https://site.test/b", 2)  # a link found on page a

        await asyncio.sleep(0)
        assert not waiter.done()  # page b is still waiting

        await take_one(queue)
        await asyncio.wait_for(waiter, timeout=1)

    async def test_taking_from_an_empty_frontier_waits(self) -> None:
        queue = frontier()
        waiting = asyncio.create_task(take_one(queue))

        await asyncio.sleep(0.01)
        assert not waiting.done()

        queue.add("https://site.test/a", 1)
        assert await asyncio.wait_for(waiting, timeout=1) == CrawlTask("https://site.test/a", 1)


async def test_clear_drops_waiting_pages_and_lets_the_wait_finish() -> None:
    queue = frontier()
    queue.add("https://site.test/a", 1)
    queue.add("https://site.test/b", 1)

    queue.clear()

    await asyncio.wait_for(queue.wait_until_finished(), timeout=1)
    assert await is_empty(queue)


async def test_a_cleared_page_is_not_queued_again() -> None:
    queue = frontier()
    queue.add("https://site.test/a", 1)
    queue.clear()
    queue.add("https://site.test/a", 1)

    assert await is_empty(queue)
