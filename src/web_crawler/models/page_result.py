from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageResult:
    """A crawled page and the links found on it, in document order."""

    url: str
    links: tuple[str, ...]
