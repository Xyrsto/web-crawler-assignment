"""The frontier: which pages are waiting to be crawled, and which were ever scheduled.

Design notes (the README has the long version):

* A URL is marked visited when it is *added*, in the same synchronous step as the check.
  There is no `await` between the two, so on one event loop the step is atomic and no lock is
  needed: a page can never be queued twice.
* A page counts as unfinished from the moment it is added until the `take()` block that
  received it ends. `wait_until_finished()` returns when that count reaches zero. Callers add
  the links of a page inside the block, so the count reaches zero only when the queue is empty
  and nobody is busy.
* The queue is unbounded on purpose. Workers both consume and produce, so a bounded queue
  could deadlock when full.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from web_crawler.urls import SiteScope

logger = logging.getLogger(__name__)


def _allow_everything(url: str) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class CrawlTask:
    """A page to crawl: its canonical URL and how many links away from the seed it is."""

    url: str
    depth: int


class Frontier:
    """The queue of pages to crawl, with deduplication and limits.

    `may_crawl` is an extra rule (for example robots.txt) applied to every canonical URL;
    pages it refuses are remembered, so they are counted once and never asked about again.
    """

    def __init__(
        self,
        scope: SiteScope,
        max_pages: int | None = None,
        may_crawl: Callable[[str], bool] = _allow_everything,
    ) -> None:
        self._scope = scope
        self._max_pages = max_pages
        self._may_crawl = may_crawl
        self._queue: asyncio.Queue[CrawlTask] = asyncio.Queue()
        self._visited: set[str] = set()
        self._scheduled = 0
        self._disallowed = 0
        self._closed = False

    @property
    def disallowed(self) -> int:
        """How many pages were refused by the `may_crawl` rule."""
        return self._disallowed

    def close(self) -> None:
        """Refuse any further pages. Pages already queued are left alone (see `clear`)."""
        self._closed = True

    def add(self, url: str, depth: int) -> None:
        """Queue `url` unless it is out of scope, already seen, refused or over the limit."""
        key = self._scope.canonical_url(url)
        if key is None or key in self._visited or self._closed:
            return
        if self._max_pages is not None and self._scheduled >= self._max_pages:
            return
        self._visited.add(key)
        if not self._may_crawl(key):
            self._disallowed += 1
            logger.debug("skipped %s (not allowed)", key)
            return
        self._scheduled += 1
        self._queue.put_nowait(CrawlTask(key, depth))

    def claim(self, key: str) -> bool:
        """Mark a page that was reached by a redirect as handled.

        `key` is the canonical URL the redirect ended on. Returns False if that page was
        already scheduled or may not be crawled, in which case the caller must drop it.
        """
        if key in self._visited or not self._may_crawl(key):
            return False
        self._visited.add(key)
        return True

    @asynccontextmanager
    async def take(self) -> AsyncIterator[CrawlTask]:
        """Wait for the oldest queued page and hand it out.

        The page counts as finished when the `async with` block ends, even if it raises,
        so a caller cannot forget to report it.
        """
        task = await self._queue.get()
        try:
            yield task
        finally:
            self._queue.task_done()

    async def wait_until_finished(self) -> None:
        """Wait until every added page has been taken and its `take()` block has ended."""
        await self._queue.join()

    def clear(self) -> None:
        """Throw away the pages that have not been taken yet."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()
