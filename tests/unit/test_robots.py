from web_crawler.models import RobotsFetch
from web_crawler.robots import RobotsPolicy

UA = "web-crawler/0.1.0 (+https://example.test)"


def policy(text: str, user_agent: str = UA) -> RobotsPolicy:
    return RobotsPolicy.parse(text, user_agent)


def test_everything_is_allowed_without_rules() -> None:
    assert policy("").allowed("https://example.com/anything")


def test_disallow_blocks_a_prefix() -> None:
    robots = policy("User-agent: *\nDisallow: /private")
    assert not robots.allowed("https://example.com/private")
    assert not robots.allowed("https://example.com/private/deep?x=1")
    assert robots.allowed("https://example.com/public")


def test_empty_disallow_means_no_restriction() -> None:
    assert policy("User-agent: *\nDisallow:").allowed("https://example.com/a")


def test_longest_match_wins() -> None:
    robots = policy("User-agent: *\nDisallow: /docs\nAllow: /docs/public")
    assert not robots.allowed("https://example.com/docs/secret")
    assert robots.allowed("https://example.com/docs/public/page")


def test_allow_wins_a_tie() -> None:
    robots = policy("User-agent: *\nDisallow: /page\nAllow: /page")
    assert robots.allowed("https://example.com/page")


def test_wildcards_and_end_anchor() -> None:
    robots = policy("User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp/*/cache")
    assert not robots.allowed("https://example.com/files/report.pdf")
    assert robots.allowed("https://example.com/files/report.pdf?download=1")
    assert robots.allowed("https://example.com/files/report.pdfx")
    assert not robots.allowed("https://example.com/tmp/abc/cache")
    assert robots.allowed("https://example.com/tmp/abc/other")


def test_query_strings_are_part_of_the_matched_path() -> None:
    robots = policy("User-agent: *\nDisallow: /*?session=")
    assert not robots.allowed("https://example.com/a?session=1")
    assert robots.allowed("https://example.com/a?page=1")


def test_a_group_naming_us_beats_the_wildcard_group() -> None:
    text = """
    User-agent: *
    Disallow: /

    User-agent: web-crawler
    Disallow: /private
    """
    robots = policy(text)
    assert robots.allowed("https://example.com/public")
    assert not robots.allowed("https://example.com/private")


def test_groups_for_other_agents_are_ignored() -> None:
    robots = policy("User-agent: googlebot\nDisallow: /")
    assert robots.allowed("https://example.com/a")


def test_several_agents_can_share_one_group() -> None:
    robots = policy("User-agent: googlebot\nUser-agent: web-crawler\nDisallow: /x")
    assert not robots.allowed("https://example.com/x")


def test_agent_matching_is_case_insensitive_and_comments_are_ignored() -> None:
    text = "# hello\nUSER-AGENT: Web-Crawler  # us\nDISALLOW: /x # nope"
    assert not policy(text).allowed("https://example.com/x")


def test_crawl_delay_is_read_for_our_group() -> None:
    text = "User-agent: *\nCrawl-delay: 2\n\nUser-agent: web-crawler\nCrawl-delay: 0.5"
    assert policy(text).crawl_delay == 0.5
    assert policy("User-agent: *\nCrawl-delay: 3").crawl_delay == 3


def test_junk_crawl_delay_is_ignored() -> None:
    assert policy("User-agent: *\nCrawl-delay: soon").crawl_delay is None
    assert policy("User-agent: *\nCrawl-delay: -1").crawl_delay is None


def test_lines_before_any_user_agent_are_ignored() -> None:
    assert policy("Disallow: /\nCrawl-delay: 9").allowed("https://example.com/a")


def test_4xx_means_allow_all_and_unreachable_means_disallow_all() -> None:
    assert RobotsPolicy.from_fetch(RobotsFetch("missing"), UA).allowed("https://example.com/a")
    unreachable = RobotsPolicy.from_fetch(RobotsFetch("unreachable"), UA)
    assert not unreachable.allowed("https://example.com/a")
    assert unreachable.denies_everything


def test_from_fetch_parses_the_body() -> None:
    robots = RobotsPolicy.from_fetch(RobotsFetch("ok", "User-agent: *\nDisallow: /x"), UA)
    assert not robots.allowed("https://example.com/x")
