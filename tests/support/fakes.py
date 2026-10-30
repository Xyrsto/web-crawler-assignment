"""In-memory stand-ins for the crawler's I/O edges."""

import asyncio
from collections.abc import Mapping

from web_crawler.models import (
    FetchedPage,
    FetchFailure,
    FetchOutcome,
    PageResult,
    RobotsFetch,
)


def html(*hrefs: str) -> str:
    """A minimal page linking to each href."""
    links = "".join(f'<a href="{href}">link</a>' for href in hrefs)
    return f"<html><body>{links}</body></html>"


class FakeFetcher:
    """Serves pages from a dict and records how it was used."""

    def __init__(
        self,
        pages: Mapping[str, str | FetchOutcome],
        *,
        robots: RobotsFetch | None = None,
        delay: float = 0.0,
        explode_on: str | None = None,
    ) -> None:
        self.pages = pages
        self.robots = robots or RobotsFetch("missing")
        self.delay = delay
        self.explode_on = explode_on
        self.fetched: list[str] = []
        self.robots_requests: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch(self, url: str) -> FetchOutcome:
        self.fetched.append(url)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        if url == self.explode_on:
            raise RuntimeError("boom")
        page = self.pages.get(url)
        if page is None:
            return FetchFailure(url, "HTTP 404", 404)
        if isinstance(page, str):
            return FetchedPage(url, page)
        return page

    async def fetch_robots(self, url: str) -> RobotsFetch:
        self.robots_requests.append(url)
        return self.robots


class CollectingSink:
    def __init__(self) -> None:
        self.results: list[PageResult] = []

    def emit(self, result: PageResult) -> None:
        self.results.append(result)

    @property
    def urls(self) -> list[str]:
        return [result.url for result in self.results]

    def links_of(self, url: str) -> tuple[str, ...]:
        return next(result.links for result in self.results if result.url == url)
