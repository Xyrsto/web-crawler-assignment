"""Command-line interface: argument parsing, wiring and exit codes.

Exit codes: 0 success, 1 fatal error, 2 usage error, 130 interrupted.
Results go to stdout only; logs, errors and the summary go to stderr.
"""

import argparse
import asyncio
import io
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from types import FrameType

from web_crawler import __version__
from web_crawler.crawler import Crawler, FatalCrawlError
from web_crawler.fetcher import AiohttpFetcher, make_session
from web_crawler.models import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    CrawlConfig,
    CrawlStats,
)
from web_crawler.rate_gate import RateGate
from web_crawler.sink import JsonLinesSink, ResultSink, TextSink
from web_crawler.urls import SiteScope

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-crawler",
        description="Crawl one site and print every page with the URLs found on it.",
    )
    add = parser.add_argument
    add("url", help="absolute http(s) base URL; only this exact host is crawled")
    add(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help="concurrent workers and connection limit (default: %(default)s)",
    )
    add("--max-pages", type=int, metavar="N", help="stop scheduling after N pages")
    add(
        "--max-depth",
        type=int,
        metavar="D",
        help="do not follow links found deeper than D (the seed is depth 0)",
    )
    add(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="per-request timeout (default: %(default)s)",
    )
    add("--ignore-robots", action="store_true", help="do not fetch or obey robots.txt")
    add("--json", action="store_true", help="print JSON Lines instead of text")
    add("--user-agent", default=DEFAULT_USER_AGENT, metavar="STRING", help="override User-Agent")
    add("-v", "--verbose", action="store_true", help="debug logging on stderr")
    add("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject values that parse fine but make no sense (exits with code 2)."""
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth must not be negative")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    try:
        SiteScope(args.url)
    except ValueError:
        parser.error(f"not an absolute http(s) URL: {args.url!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    # Results must be UTF-8 with plain "\n" regardless of platform (matters on Windows).
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    config = CrawlConfig(
        seed=args.url,
        concurrency=args.concurrency,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        timeout=args.timeout,
        ignore_robots=args.ignore_robots,
        user_agent=args.user_agent,
    )
    sink: ResultSink = JsonLinesSink(sys.stdout) if args.json else TextSink(sys.stdout)
    try:
        return asyncio.run(_run(config, sink))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # the reader (for example `head`) went away
        sys.stderr.close()
        return EXIT_OK


async def _run(config: CrawlConfig, sink: ResultSink) -> int:
    scope = SiteScope(config.seed)
    gate = RateGate()
    async with make_session(config) as session:
        fetcher = AiohttpFetcher(config, scope.contains, gate, session)
        crawler = Crawler(config, fetcher, sink, scope, gate)
        restore = _stop_on_sigint(crawler)
        try:
            stats = await crawler.run()
        except FatalCrawlError as exc:
            logging.error("%s", exc)
            return EXIT_FATAL
        finally:
            restore()
    print(_summary(stats), file=sys.stderr)
    return EXIT_INTERRUPTED if stats.interrupted else EXIT_OK


def _stop_on_sigint(crawler: Crawler) -> Callable[[], object]:
    """First Ctrl-C asks the crawler to wind down; a second one aborts immediately."""
    loop = asyncio.get_running_loop()

    def handler(signum: int, frame: FrameType | None) -> None:
        if crawler.stopping:
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(crawler.stop)

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread, so signals cannot be installed
        return lambda: None
    return lambda: signal.signal(signal.SIGINT, previous)


def _summary(stats: CrawlStats) -> str:
    state = "interrupted" if stats.interrupted else "done"
    return (
        f"{state}: {stats.crawled} pages crawled, {stats.failed} failed, "
        f"{stats.skipped} skipped in {stats.elapsed:.2f}s ({stats.pages_per_second:.1f} pages/s)"
    )
