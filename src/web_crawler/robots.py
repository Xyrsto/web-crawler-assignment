"""robots.txt: parsing the rules (RFC 9309) and loading them for a site.

The standard library's `urllib.robotparser` is not used: it applies the first matching rule
instead of the longest one and does not understand the `*` and `$` wildcards.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from web_crawler.models import RobotsFetch

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: re.Pattern[str]
    length: int


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


class RobotsPolicy:
    """What the crawler may fetch from one host."""

    def __init__(
        self,
        rules: tuple[_Rule, ...] = (),
        crawl_delay: float | None = None,
        *,
        deny_all: bool = False,
    ) -> None:
        self._rules = rules
        self._crawl_delay = crawl_delay
        self._deny_all = deny_all

    @classmethod
    def allow_all(cls) -> "RobotsPolicy":
        return cls()

    @classmethod
    def disallow_all(cls) -> "RobotsPolicy":
        return cls(deny_all=True)

    @classmethod
    def from_fetch(cls, fetch: RobotsFetch, user_agent: str) -> "RobotsPolicy":
        """RFC 9309: 4xx means no restrictions, an unreachable server means none allowed."""
        match fetch.state:
            case "ok":
                return cls.parse(fetch.text, user_agent)
            case "missing":
                return cls.allow_all()
            case "unreachable":
                return cls.disallow_all()

    @classmethod
    def parse(cls, text: str, user_agent: str) -> "RobotsPolicy":
        groups = _parse_groups(text)
        ua = user_agent.lower()
        specific = [g for g in groups if any(a != "*" and a in ua for a in g.agents)]
        chosen = specific or [g for g in groups if "*" in g.agents]
        rules = tuple(rule for group in chosen for rule in group.rules)
        delays = [g.crawl_delay for g in chosen if g.crawl_delay is not None]
        return cls(rules, delays[0] if delays else None)

    @property
    def denies_everything(self) -> bool:
        return self._deny_all

    @property
    def crawl_delay(self) -> float | None:
        return self._crawl_delay

    def allowed(self, url: str) -> bool:
        if self._deny_all:
            return False
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        best: _Rule | None = None
        for rule in self._rules:
            if not rule.pattern.match(target):
                continue
            # Longest match wins; on a tie, Allow beats Disallow.
            if (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def _parse_groups(text: str) -> list[_Group]:
    groups: list[_Group] = []
    current: _Group | None = None
    collecting_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        if name == "user-agent":
            if current is None or not collecting_agents:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
            collecting_agents = True
        elif current is not None and name in {"allow", "disallow"}:
            collecting_agents = False
            if value:  # an empty Disallow/Allow carries no restriction
                current.rules.append(_make_rule(allow=name == "allow", path=value))
        elif current is not None and name == "crawl-delay":
            collecting_agents = False
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                current.crawl_delay = delay
    return groups


def _make_rule(*, allow: bool, path: str) -> _Rule:
    anchored = path.endswith("$")
    body = path[:-1] if anchored else path
    regex = ".*".join(re.escape(piece) for piece in body.split("*"))
    return _Rule(allow, re.compile(regex + (r"\Z" if anchored else "")), len(path))


class RobotsSource(Protocol):
    """Anything that can download a robots.txt file (the real fetcher, or a fake in tests)."""

    async def fetch_robots(self, url: str) -> RobotsFetch: ...


async def load_policy(source: RobotsSource, origin: str, user_agent: str) -> RobotsPolicy:
    """Download `<origin>/robots.txt` and turn it into the rules that apply to `user_agent`."""
    fetch = await source.fetch_robots(f"{origin}/robots.txt")
    if fetch.state == "unreachable":
        logger.warning("robots.txt is unreachable; treating the whole site as disallowed")
    return RobotsPolicy.from_fetch(fetch, user_agent)
