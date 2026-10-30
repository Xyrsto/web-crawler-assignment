from dataclasses import dataclass


@dataclass(slots=True)
class CrawlStats:
    """Counters reported in the summary line."""

    crawled: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed: float = 0.0
    interrupted: bool = False

    @property
    def pages_per_second(self) -> float:
        return self.crawled / self.elapsed if self.elapsed > 0 else 0.0
