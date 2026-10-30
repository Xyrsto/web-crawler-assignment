# web-crawler

A fast, polite, single-domain web crawler for the command line. Give it a base URL; for every page it finds on that exact host it prints the page's URL followed by every URL found on the page.

```
$ web-crawler https://example.com/ --max-pages 3
https://example.com/
    https://example.com/about
    https://example.com/blog

https://example.com/about
    https://example.com/
    https://example.com/team
    https://twitter.com/example

https://example.com/blog
    https://example.com/
    https://example.com/blog/first-post
```

- **Single domain, strictly.** `example.com`, `www.example.com` and `blog.example.com` are three different sites. Links to other hosts are listed, never fetched.
- **Fast.** One `asyncio` event loop, a fixed pool of workers and pooled keep-alive connections (see [Benchmark](#benchmark)).
- **Polite.** Obeys robots.txt (including `Crawl-delay`), identifies itself, bounds retries, honours `Retry-After`, and caps timeouts and body sizes.
- **No crawling frameworks.** Only `aiohttp` (HTTP) and `selectolax` (HTML parsing).

## Installation

Requires Python 3.12+. From the project folder, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run web-crawler --version
```

Or with plain pip, inside a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
web-crawler --version
```

The examples below assume the `web-crawler` command is available; with uv, prefix them with `uv run`. `python -m web_crawler` works as well.

## Usage

```
web-crawler [options] URL
```

| Option | Default | Description |
|---|---|---|
| `URL` | required | Absolute http(s) base URL. |
| `--concurrency N` | 20 | Concurrent workers, also the connection limit. |
| `--max-pages N` | unbounded | Stop scheduling new pages after N. In-flight fetches finish. |
| `--max-depth D` | unbounded | Do not follow links found on pages at depth D (the seed is depth 0). |
| `--timeout SECONDS` | 10 | Total time allowed per request, body included. |
| `--ignore-robots` | off | Do not fetch or obey robots.txt. |
| `--json` | off | JSON Lines (`{"url": "...", "links": [...]}`) instead of text. |
| `--user-agent STRING` | built-in | Override the User-Agent. |
| `-v`, `--verbose` | off | Debug logging on stderr. |

### Examples

```bash
# Crawl a whole site
web-crawler https://example.com

# A small try-out: at most 50 pages, at most 2 links deep
web-crawler https://example.com --max-pages 50 --max-depth 2

# Be gentle with a small server
web-crawler https://example.com --concurrency 5 --timeout 5

# One JSON object per page, saved to a file
web-crawler https://example.com --json > pages.jsonl

# Keep results and logs apart
web-crawler https://example.com > pages.txt 2> crawl.log
```

### Output

stdout carries results only, so it can be piped. Logs, per-page problems and the final summary go to stderr. Results are streamed as pages complete, so **the order of pages is not deterministic**; the links *within* a page are in document order. The crawl ends by itself when no new pages are left, and Ctrl-C stops it early.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Crawl completed. Individual page failures do not change this. |
| 1 | Fatal: the seed is unreachable or returns an error, redirects to another host, or is disallowed by robots.txt (or robots.txt could not be fetched). |
| 2 | Usage error (bad URL or option). |
| 130 | Interrupted with Ctrl-C. The first Ctrl-C stops scheduling and gives in-flight pages 5 s to finish; a second one aborts at once. |

## How it behaves

- **Scope.** A URL is in scope when its host (lowercased, punycode, no trailing dot) and effective port equal the seed's. `http` and `https` forms of the same host are the same site and are deduplicated using the seed's scheme.
- **Links.** Every `<a href>` is extracted (honouring `<base href>`), resolved to an absolute URL and listed once per page in document order. Only `http(s)` links are kept (`mailto:`, `tel:`, `javascript:` and similar are dropped). Fragments are stripped and query strings are kept, so a fragment-only link such as `#top` shows up as a link to the page itself.
- **Pages.** Only `text/html` and `application/xhtml+xml` responses are parsed. Other in-scope URLs (PDFs, images) are listed as links but their bodies are never downloaded. Failed pages (4xx, 5xx after retries, timeout, non-HTML, over 5 MB) are not printed on stdout; they are reported on stderr and counted.
- **Redirects.** Followed by hand, up to 5 hops, checking the host at every hop. A redirect to another host is not followed. A page reached through a same-host redirect is reported under its final URL.
- **robots.txt.** Fetched once. A 4xx means no restrictions; a 5xx or timeout means everything is disallowed (RFC 9309), and the crawl stops with exit code 1 unless `--ignore-robots` is given. `Crawl-delay` (capped at 60 s) is enforced by a shared gate that spaces request starts.
- **Resilience.** Up to 2 retries (timeouts, connection errors, 5xx, 429) with exponential backoff and jitter. `Retry-After` is honoured, capped at 60 s.

## Architecture

```
src/web_crawler/
├── cli.py        argument parsing, wiring, exit codes, Ctrl-C handling
├── crawler.py    orchestrator: workers, fetching, shutdown
├── frontier.py   the queue of pages to crawl: dedupe, limits, a may-crawl rule, "all done"
├── fetcher.py    Fetcher protocol + AiohttpFetcher (redirects, retries, size cap)
├── extractor.py  HTML in, ordered unique absolute links out
├── urls.py       normalisation and the same-site rule
├── robots.py     RFC 9309 parser, RobotsPolicy, load_policy
├── rate_gate.py  RateGate: minimum interval between requests (used for Crawl-delay)
├── sink.py       ResultSink protocol + text / JSON Lines writers
└── models/       one frozen dataclass per file
    ├── crawl_config.py   CrawlConfig and its defaults
    ├── crawl_stats.py    CrawlStats (the summary counters)
    ├── page_result.py    PageResult (a page and its links)
    ├── fetched_page.py   FetchedPage  ┐
    ├── fetch_failure.py  FetchFailure ├ the three possible fetch results
    ├── fetch_skipped.py  FetchSkipped ┘
    ├── fetch_outcome.py  FetchOutcome (their union)
    └── robots_fetch.py   RobotsFetch (the result of requesting robots.txt)
```

`models/__init__.py` re-exports everything, so callers write `from web_crawler.models import CrawlConfig`.

- Pure functions where possible (`urls`, `extractor`, `RobotsPolicy.parse`): most of the logic is unit-testable without any I/O.
- The `Frontier` owns the queue and the "have we seen this page?" bookkeeping, so the crawler only runs workers and fetches pages.
- I/O sits behind two protocols, `Fetcher` and `ResultSink`. The crawler core never imports `aiohttp`; its tests run against an in-memory fake.
- Everything is injected through constructors; `cli.py` is the only place that picks concrete implementations.
- Fetch results are a closed set of small frozen dataclasses (`FetchedPage | FetchFailure | FetchSkipped`) handled with `match`, not exceptions used as control flow.

## Design decisions and trade-offs

### Concurrency: `asyncio` workers

The work is I/O-bound (waiting on the network), so a single event loop with `N` worker tasks (`--concurrency`, default 20) pulling from an `asyncio.Queue` is the cheapest way to keep many requests in flight.

| Option | Verdict |
|---|---|
| `asyncio` workers | **Chosen.** Lowest memory per connection, trivially capped, no locks. |
| Thread pool | More memory and context switching per in-flight request, plus locking around shared state. |
| Multiprocessing | Heavy IPC, and the visited set becomes hard to share. Only worth it if CPU-bound parsing dominated. |
| One task per URL | Cheap to spawn, but the concurrency bound and memory use are harder to reason about. A fixed pool makes both explicit. |

### Deduplicate at enqueue time

A URL is added to the `visited` set in the same synchronous step that checks it and puts it on the queue. There is no `await` between the check and the add, so on a single event loop the step is atomic. A page linked from many places at once is therefore fetched exactly once, without a lock. (Marking at dequeue time would let two workers both see the URL as unvisited.)

### Termination: a page is finished only after its links are added

"The queue is empty" is not "the crawl is done": a worker mid-fetch may still add links. The frontier wraps an `asyncio.Queue`, which counts unfinished pages. A worker takes a page with `frontier.take()`, and the page counts as finished only when that `async with` block ends, after the worker has added the page's links. The count therefore reaches zero only when the queue is empty and nobody is busy. At that point `run()` cancels the idle workers and returns. The same wait is raced against a stop event, which is how Ctrl-C winds down.

### The queue is unbounded

Workers both consume and produce, so a bounded queue could deadlock when full (every worker blocked on `put`). The cost is memory: the visited set and queue grow with the number of distinct URLs discovered. That is fine for typical sites. For tens of millions of URLs, use a Bloom filter or a disk-backed frontier (SQLite, Redis).

### `aiohttp` over `httpx`

`aiohttp` is measurably faster under high concurrency and provides a real in-process test server. `httpx` has a nicer API and HTTP/2, but speed was the priority. One shared `ClientSession` with a `TCPConnector(limit=concurrency, ttl_dns_cache=300)` gives keep-alive reuse and DNS caching. `aiodns` and Brotli are installed for async DNS and `br` compression; `aiodns` is skipped on Windows because it needs a selector event loop, which is not the Windows default.

### `selectolax` over `lxml` / BeautifulSoup

Extracting links is a trivial query, so parser speed dominates. `selectolax` (Lexbor) is the fastest option and has a small API. Parsing runs inline on the event loop because pages are small. For very large documents the parse would block other workers; moving it to a `ProcessPoolExecutor` is the obvious refinement.

### Strict host matching, and what is *not* normalised

Subdomains are excluded, so matching is exact on host and port. URL normalisation is intentionally minimal: lowercase scheme and host, punycode, default ports and fragments removed. Query strings and trailing slashes are never touched, because both can change the content and "tidying" them risks merging distinct pages (accuracy over cleverness). One exception: an empty path becomes `/`, which RFC 3986 defines as equivalent, so `https://example.com` and `https://example.com/` are not fetched twice.

### Redirects are followed by hand

The default behaviour would happily follow a redirect to another host. Following manually lets the crawler check scope at every hop. The trade-off: a page can be requested twice when it is first reached via a redirect and later linked directly (for example `/old` redirects to `/about`, and `/about` is also linked). The result is still reported once, but the request is made twice. Avoiding that would need the fetcher to consult the crawler's visited set before each hop, which leaks crawl state into the I/O layer. The duplicate request is rare and cheap, so it is accepted.

### Own robots.txt parser

The standard library's `urllib.robotparser` applies the *first* matching rule and ignores the `*` and `$` wildcards. RFC 9309 specifies longest-match precedence (Allow wins a tie) and those wildcards, which real sites use, so a small parser lives in `robots.py` with its own tests. Known gaps: no percent-encoding normalisation when matching, and a single `Crawl-delay` applies to the whole crawl.

### Streaming, non-deterministic output

Results are written as each page finishes, so the first output arrives immediately and memory stays flat. The cost is that the order of pages varies between runs. Consumers that need a stable order should sort, or use `--json` and key by URL.

### Failed and non-HTML pages

They are not printed on stdout (the output is pages and their links) and do not change the exit code. They are warned about on stderr and counted. A site with many PDFs therefore produces many "not HTML" warnings; a quieter log level for that case would be a reasonable refinement.

## Benchmark

`benchmarks/synthetic_site.py` serves a graph of pages with a fixed per-request latency, with cycles and diamonds in the link graph. It runs in a separate process so the crawler does not share an event loop with it. `benchmarks/bench_concurrency.py` crawls it at several concurrency levels (1 is the sequential baseline), takes the median of 3 timed runs after a warm-up, and measures peak memory in a separate untimed run (tracing slows the crawl). Every run also asserts **accuracy**: every page crawled exactly once, with no failures.

```bash
uv run python -m benchmarks.bench_concurrency --pages 300 --repeats 3
```

Example result: 300 pages, 50 ms latency per request, local server.

| Concurrency | Time (s) | Pages/s | Speed-up | Peak memory (MiB) |
|---:|---:|---:|---:|---:|
| 1 | 18.67 | 16 | 1.0x | 1.5 |
| 5 | 3.80 | 79 | 4.9x | 1.8 |
| 20 | 1.06 | 282 | 17.6x | 2.9 |
| 50 | 0.53 | 565 | 35.2x | 5.2 |

Speed-up is close to linear in the worker count, because with 50 ms of latency per request the crawler spends nearly all its time waiting on the network and the event loop has capacity to spare. Returns diminish once per-page CPU work (parsing, URL normalisation, the event loop itself) is no longer hidden behind latency, so against a faster server the curve flattens earlier. On a real site, politeness is the real ceiling: a `Crawl-delay` or rate limits make extra workers pointless, which is why the default is 20 and not 500. Peak memory grows slowly with concurrency (more in-flight responses), on top of a floor from the visited set.

These numbers come from one machine and a loopback server, so treat them as the shape of the curve, not a promise.

## Development

```bash
uv sync                                  # create .venv and install everything
uv run pytest --cov                      # tests, with coverage
uv run ruff check .                      # lint
uv run ruff format --check .             # formatting (drop --check to apply)
uv run mypy                              # strict type checking
uv run python -m benchmarks.bench_concurrency
```

### Testing

All tests run offline.

- **Unit tests** (`tests/unit`): URL normalisation and scoping, link extraction, the robots parser and loader, the rate gate, the frontier, both sinks, CLI parsing, and the crawler core against an in-memory fake fetcher. The core tests cover cycles, diamonds, self-links, out-of-scope links, `--max-pages` and `--max-depth`, the concurrency cap, failures, a worker raising unexpectedly, the one-fetch-per-URL race, redirects onto already-handled pages, robots rules and `stop()`.
- **Integration tests** (`tests/integration`): the real `AiohttpFetcher` against an in-process `aiohttp` server (charsets, errors, non-HTML, oversized and streamed bodies, retries and backoff, `Retry-After` and its cap, timeouts including slow bodies, redirects, robots.txt variants), a whole-site crawl, and the CLI run as a subprocess, asserting stdout, stderr and exit codes.

## Limitations

Not built, on purpose, with a note on how each would be handled:

| Not supported | How I would extend it |
|---|---|
| JavaScript-rendered content | Needs a headless browser, which is out of scope. In production, add a rendering tier behind the same `Fetcher` protocol. |
| Authentication, cookies | Pass a configured session or cookie jar into the fetcher. |
| `sitemap.xml` seeding | Another source of seed URLs; improves coverage of orphan pages. |
| `<meta http-equiv="refresh">` redirects | The extractor could surface them as links. |
| Multiple domains at once | A per-host scheduler with per-host politeness state and a global concurrency budget. |
| Persistence and resume | Frontier and visited set backed by SQLite or Redis. |
| Distributed crawling | Shared frontier (Redis, SQS) and consistent hashing of hosts to workers. |

Other limits:

- `Crawl-delay` applies globally and is capped at 60 s. robots.txt matching does not normalise percent-encoding.
- Unusual URLs are skipped rather than listed: anything `urllib` cannot parse, or longer than 2048 characters.

## What I would do with more time

- **Multi-domain crawling:** a scheduler keyed by host, with per-host queues, politeness state and robots caches under a global concurrency budget. This is where the single `RateGate` design would be replaced.
- **A service instead of a CLI.** A CLI is a poor fit for multi-domain, scheduled or long-running crawls: no job identity, no progress or cancellation from outside, results only on stdout, and a crash loses everything. A small HTTP API plus a job queue, with a persistent frontier and results streamed to storage or a message bus, plus metrics (pages/s, error rates, queue depth), would suit that better.
- **A persistent or Bloom-filter visited set** for very large sites.
- **A process pool for parsing** huge documents.
- **Sitemap seeding** and `<meta refresh>` handling.
- **Skip the duplicate request** when a redirect lands on an already-visited page, without leaking crawl state into the fetcher (for example via an injected "have we seen this?" callback).
- **HTTP/2 and conditional requests** for re-crawls.
- A test that sends a real Ctrl-C signal to the CLI.

## Tools and workflow

- **IDE:** PyCharm.
- **Interactive AI assistants and other AI tools:** this specification and the first implementation (code, tests, benchmark and this README) were produced with Claude Code (Claude Sonnet 5.5) in the Claude desktop app.
- **Skills used:** one, the `grilling` skill. It was run before the specification was written and produced the design decisions in it: the concurrency model, HTTP client and parser, strict host matching, output format, politeness rules, tooling and non-goals.
- **Plugins and MCP servers used:** none.
- **Other tooling used during the work, all through Claude Code's built-in tools:** file read/write/edit, PowerShell for running commands, and `uv` for dependencies and running the tests.
- **How the generated code was verified:** the test suite, `ruff`, `mypy --strict` and the benchmark were run, and the crawler was run against a public practice site (books.toscrape.com).
