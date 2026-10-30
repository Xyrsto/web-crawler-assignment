"""Immutable data shared between the crawler's modules, one model per file."""

from web_crawler.models.crawl_config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    CrawlConfig,
)
from web_crawler.models.crawl_stats import CrawlStats
from web_crawler.models.fetch_failure import FetchFailure
from web_crawler.models.fetch_outcome import FetchOutcome
from web_crawler.models.fetch_skipped import FetchSkipped
from web_crawler.models.fetched_page import FetchedPage
from web_crawler.models.page_result import PageResult
from web_crawler.models.robots_fetch import RobotsFetch

__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "CrawlConfig",
    "CrawlStats",
    "FetchFailure",
    "FetchOutcome",
    "FetchSkipped",
    "FetchedPage",
    "PageResult",
    "RobotsFetch",
]
