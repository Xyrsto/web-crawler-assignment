"""Crawl a synthetic site at several concurrency levels and print a Markdown table.

    uv run python -m benchmarks.bench_concurrency
    uv run python -m benchmarks.bench_concurrency --pages 1000 --latency-ms 100 --repeats 5

The site runs in a separate process so the crawler does not compete with it for the event
loop. Concurrency 1 is the sequential baseline. Every run also checks accuracy: all pages
must be crawled, none twice.
"""

import argparse
import asyncio
import platform
import socket
import statistics
import subprocess
import sys
import time
import tracemalloc

from web_crawler.crawler import Crawler
from web_crawler.fetcher import AiohttpFetcher, make_session
from web_crawler.models import CrawlConfig, CrawlStats, PageResult
from web_crawler.rate_gate import RateGate
from web_crawler.urls import SiteScope

LEVELS = (1, 5, 20, 50)


class CountingSink:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.duplicates = 0

    def emit(self, result: PageResult) -> None:
        if result.url in self.seen:
            self.duplicates += 1
        self.seen.add(result.url)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def start_site(port: int, pages: int, latency_ms: float) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "benchmarks.synthetic_site",
            f"--port={port}",
            f"--pages={pages}",
            f"--latency-ms={latency_ms}",
        ]
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise RuntimeError("the synthetic site did not start")


async def crawl_once(base: str, concurrency: int, expected_pages: int) -> CrawlStats:
    config = CrawlConfig(
        seed=f"{base}/page/0", concurrency=concurrency, ignore_robots=True, timeout=30
    )
    scope = SiteScope(config.seed)
    sink = CountingSink()
    gate = RateGate()
    async with make_session(config) as session:
        fetcher = AiohttpFetcher(config, scope.contains, gate, session)
        stats = await Crawler(config, fetcher, sink, scope, gate).run()
    if len(sink.seen) != expected_pages or sink.duplicates or stats.failed:
        raise AssertionError(
            f"inaccurate crawl: {len(sink.seen)}/{expected_pages} pages, "
            f"{sink.duplicates} duplicates, {stats.failed} failures"
        )
    return stats


async def run(base: str, pages: int, repeats: int) -> None:
    await crawl_once(base, 20, pages)  # warm-up: imports, DNS, JIT-ish caches

    print("| Concurrency | Time (s) | Pages/s | Speed-up | Peak memory (MiB) |")
    print("|---:|---:|---:|---:|---:|")
    baseline = 0.0
    for level in LEVELS:
        runs = [await crawl_once(base, level, pages) for _ in range(repeats)]
        elapsed = statistics.median(stats.elapsed for stats in runs)
        rate = pages / elapsed
        baseline = baseline or rate

        tracemalloc.start()  # separate, untimed run: tracing slows the crawl down
        await crawl_once(base, level, pages)
        peak = tracemalloc.get_traced_memory()[1] / 2**20
        tracemalloc.stop()

        print(f"| {level} | {elapsed:.2f} | {rate:.0f} | {rate / baseline:.1f}x | {peak:.1f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=400)
    parser.add_argument("--latency-ms", type=float, default=50.0)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    print(
        f"Synthetic site: {args.pages} pages, {args.latency_ms:g} ms latency per request, "
        f"median of {args.repeats} runs.\n"
        f"Machine: {platform.platform()}, Python {platform.python_version()}, "
        f"{platform.processor() or platform.machine()}\n"
    )
    port = free_port()
    site = start_site(port, args.pages, args.latency_ms)
    try:
        asyncio.run(run(f"http://127.0.0.1:{port}", args.pages, args.repeats))
    finally:
        site.kill()


if __name__ == "__main__":
    main()
