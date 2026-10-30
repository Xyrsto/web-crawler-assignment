from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """A page that could not be used (HTTP error, timeout, not HTML, too large...)."""

    url: str
    reason: str
    status: int | None = None
