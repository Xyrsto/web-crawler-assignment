import logging

import pytest

from tests.support.fakes import FakeFetcher
from web_crawler.models import RobotsFetch
from web_crawler.robots import load_policy

ORIGIN = "https://site.test"
UA = "web-crawler/0.1.0 (+https://example.test)"


async def test_requests_the_robots_file_of_the_origin() -> None:
    fetcher = FakeFetcher({}, robots=RobotsFetch("ok", "User-agent: *\nDisallow: /private"))

    policy = await load_policy(fetcher, ORIGIN, UA)

    assert fetcher.robots_requests == ["https://site.test/robots.txt"]
    assert not policy.allowed("https://site.test/private")
    assert policy.allowed("https://site.test/public")


async def test_picks_the_rules_for_the_given_user_agent() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: friendly-bot\nAllow: /"
    fetcher = FakeFetcher({}, robots=RobotsFetch("ok", text))

    friendly = await load_policy(fetcher, ORIGIN, "friendly-bot/1.0")
    other = await load_policy(fetcher, ORIGIN, "other-bot/1.0")

    assert friendly.allowed("https://site.test/page")
    assert not other.allowed("https://site.test/page")


async def test_a_missing_file_allows_everything(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    fetcher = FakeFetcher({}, robots=RobotsFetch("missing"))

    policy = await load_policy(fetcher, ORIGIN, UA)

    assert policy.allowed("https://site.test/anything")
    assert not policy.denies_everything
    assert caplog.text == ""


async def test_an_unreachable_file_disallows_everything_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    fetcher = FakeFetcher({}, robots=RobotsFetch("unreachable"))

    policy = await load_policy(fetcher, ORIGIN, UA)

    assert policy.denies_everything
    assert not policy.allowed("https://site.test/anything")
    assert "robots.txt is unreachable" in caplog.text


async def test_the_crawl_delay_is_available_on_the_policy() -> None:
    fetcher = FakeFetcher({}, robots=RobotsFetch("ok", "User-agent: *\nCrawl-delay: 2"))

    policy = await load_policy(fetcher, ORIGIN, UA)

    assert policy.crawl_delay == 2
