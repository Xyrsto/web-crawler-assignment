from dataclasses import dataclass

from web_crawler import PROJECT_URL, __version__

DEFAULT_USER_AGENT = f"web-crawler/{__version__} (+{PROJECT_URL})"
DEFAULT_CONCURRENCY = 20
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    """Everything that tunes a crawl. Defaults match the specification."""

    seed: str
    concurrency: int = DEFAULT_CONCURRENCY
    max_pages: int | None = None
    max_depth: int | None = None
    timeout: float = DEFAULT_TIMEOUT
    ignore_robots: bool = False
    user_agent: str = DEFAULT_USER_AGENT
    max_body_bytes: int = 5 * 1024 * 1024
    max_retries: int = 2
    max_redirects: int = 5
    retry_after_cap: float = 60.0
    backoff_base: float = 0.5
