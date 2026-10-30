"""The crawl orchestrator: a fixed pool of workers that fetch pages and feed the frontier.

The frontier (see `frontier.py`) decides which pages are crawled and when the crawl is over.
A worker adds a page's links while it still holds the page (inside `frontier.take()`), so the
crawl cannot end while a page might still add links.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any

from web_crawler.extractor import extract_links
from web_crawler.fetcher import Fetcher
from web_crawler.frontier import CrawlTask, Frontier
from web_crawler.models import (
    CrawlConfig,
    CrawlStats,
    FetchedPage,
    FetchFailure,
    FetchSkipped,
    PageResult,
)
from web_crawler.rate_gate import RateGate
from web_crawler.robots import RobotsPolicy, load_policy
from web_crawler.sink import ResultSink
from web_crawler.urls import SiteScope

logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 5.0
MAX_CRAWL_DELAY_SECONDS = 60.0


class FatalCrawlError(Exception):
    """The crawl cannot proceed (seed unreachable, off-site or forbidden)."""


class Crawler:
    def __init__(
        self,
        config: CrawlConfig,
        fetcher: Fetcher,
        sink: ResultSink,
        scope: SiteScope,
        gate: RateGate | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._sink = sink
        self._scope = scope
        self._gate = gate or RateGate()
        self._policy = RobotsPolicy.allow_all()
        self._frontier = Frontier(scope, config.max_pages, may_crawl=self._robots_allow)
        self._stats = CrawlStats()
        self._stop_event = asyncio.Event()
        self._fatal: str | None = None

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    def stop(self) -> None:
        """Stop scheduling new pages; in-flight pages get a short grace period."""
        self._frontier.close()
        self._stop_event.set()

    async def run(self) -> CrawlStats:
        started = time.monotonic()
        await self._prepare()
        workers = [asyncio.create_task(self._worker()) for _ in range(self._config.concurrency)]
        try:
            await self._wait_until_done_or_stopped()
        finally:
            await self._cancel(workers)
            self._stats.skipped += self._frontier.disallowed
            self._stats.elapsed = time.monotonic() - started

        if self._fatal is not None:
            raise FatalCrawlError(self._fatal)
        return self._stats

    async def _prepare(self) -> None:
        """Load robots.txt, make sure the seed may be crawled, and queue it."""
        seed = self._scope.canonical_url(self._config.seed)
        if seed is None:
            raise ValueError(f"seed is out of its own scope: {self._config.seed!r}")

        self._policy = await self._load_robots()
        if not self._policy.allowed(seed):
            if self._policy.denies_everything:
                raise FatalCrawlError(
                    "robots.txt could not be fetched, so nothing may be crawled "
                    "(use --ignore-robots to override)"
                )
            raise FatalCrawlError(f"{seed} is disallowed by robots.txt")
        self._frontier.add(seed, 0)

    def _robots_allow(self, url: str) -> bool:
        """The frontier's `may_crawl` rule: whatever robots.txt currently says."""
        return self._policy.allowed(url)

    async def _load_robots(self) -> RobotsPolicy:
        if self._config.ignore_robots:
            return RobotsPolicy.allow_all()
        policy = await load_policy(self._fetcher, self._scope.origin, self._config.user_agent)
        delay = policy.crawl_delay
        if delay:
            self._gate.set_interval(min(delay, MAX_CRAWL_DELAY_SECONDS))
            logger.info("robots.txt asks for a crawl delay of %ss", delay)
        return policy

    async def _wait_until_done_or_stopped(self) -> None:
        """Return when every queued page is processed, or after stop() and a short grace period."""
        join_task = asyncio.create_task(self._frontier.wait_until_finished())
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait({join_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if not join_task.done():  # stop() came first: let in-flight pages finish
                self._stats.interrupted = True
                self._frontier.clear()
                await asyncio.wait({join_task}, timeout=SHUTDOWN_GRACE_SECONDS)
        finally:
            await self._cancel([join_task, stop_task])

    @staticmethod
    async def _cancel(tasks: Sequence[asyncio.Task[Any]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            async with self._frontier.take() as task:
                try:
                    await self._process(task)
                except Exception:
                    logger.exception("unexpected error while processing %s", task.url)
                    self._stats.failed += 1

    async def _process(self, task: CrawlTask) -> None:
        outcome = await self._fetcher.fetch(task.url)
        match outcome:
            case FetchedPage():
                self._handle_page(task, outcome)
            case FetchFailure(url=url, reason=reason):
                self._stats.failed += 1
                logger.warning("failed %s: %s", url, reason)
                self._note_seed_problem(task, f"cannot fetch the seed {url}: {reason}")
            case FetchSkipped(url=url, reason=reason):
                self._stats.skipped += 1
                logger.warning("skipped %s: %s", url, reason)
                self._note_seed_problem(task, f"the seed {url} was {reason}")

    def _note_seed_problem(self, task: CrawlTask, message: str) -> None:
        if task.depth == 0:
            self._fatal = message

    def _handle_page(self, task: CrawlTask, page: FetchedPage) -> None:
        final = self._scope.canonical_url(page.url)
        if final is None:
            self._stats.failed += 1
            logger.warning("failed %s: ended up outside the site", task.url)
            return
        if final != task.url and not self._frontier.claim(final):
            # a same-host redirect landed on a page that is already handled or forbidden
            self._stats.skipped += 1
            logger.debug("skipped %s (redirect target already handled)", task.url)
            return

        links = extract_links(page.html, page.url)
        self._sink.emit(PageResult(page.url, links))
        self._stats.crawled += 1

        if self._config.max_depth is not None and task.depth >= self._config.max_depth:
            return
        for link in links:
            self._frontier.add(link, task.depth + 1)
