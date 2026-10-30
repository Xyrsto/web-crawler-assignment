from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """An HTML page. `url` is the final URL after any same-host redirects."""

    url: str
    html: str
