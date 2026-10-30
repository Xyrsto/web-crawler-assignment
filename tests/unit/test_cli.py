import pytest

from web_crawler import __version__
from web_crawler.cli import _summary, build_parser, main
from web_crawler.models import DEFAULT_USER_AGENT, CrawlStats


def test_defaults() -> None:
    args = build_parser().parse_args(["https://example.com"])
    assert args.url == "https://example.com"
    assert args.concurrency == 20
    assert args.max_pages is None
    assert args.max_depth is None
    assert args.timeout == 10.0
    assert not args.ignore_robots
    assert not args.json
    assert args.user_agent == DEFAULT_USER_AGENT


def test_all_options() -> None:
    args = build_parser().parse_args(
        [
            "https://example.com",
            "--concurrency", "5",
            "--max-pages", "100",
            "--max-depth", "3",
            "--timeout", "2.5",
            "--ignore-robots",
            "--json",
            "--user-agent", "bot/1",
            "-v",
        ]
    )  # fmt: skip
    assert (args.concurrency, args.max_pages, args.max_depth, args.timeout) == (5, 100, 3, 2.5)
    assert args.ignore_robots
    assert args.json
    assert args.user_agent == "bot/1"
    assert args.verbose


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["not-a-url"],
        ["ftp://example.com"],
        ["https://example.com", "--concurrency", "0"],
        ["https://example.com", "--max-pages", "0"],
        ["https://example.com", "--max-depth", "-1"],
        ["https://example.com", "--timeout", "0"],
        ["https://example.com", "--nope"],
    ],
)
def test_usage_errors_exit_with_code_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_summary_line() -> None:
    stats = CrawlStats(crawled=10, failed=2, skipped=1, elapsed=2.0)
    assert _summary(stats) == ("done: 10 pages crawled, 2 failed, 1 skipped in 2.00s (5.0 pages/s)")
    stats.interrupted = True
    assert _summary(stats).startswith("interrupted:")


def test_pages_per_second_with_no_elapsed_time() -> None:
    assert CrawlStats(crawled=3).pages_per_second == 0.0
