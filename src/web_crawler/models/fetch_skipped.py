from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FetchSkipped:
    """A page deliberately not fetched (for example a redirect to another host)."""

    url: str
    reason: str
