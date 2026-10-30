# Web Crawler: Specification

A command-line, single-domain web crawler written in Python. It is built to be fast, polite and well-tested, and to be reviewed as a production-quality codebase.

- **Status:** Draft v1
- **Language:** Python 3.12+
- **Deliverables:** the crawler (CLI), its test suite, a benchmark script and a README that documents the design decisions

---

## 1. Problem statement

Given a base URL, crawl the site and, for every page found, print the page's URL followed by every URL found on that page.

Constraints from the brief:

- Runs from the command line and accepts a base URL.
- Only the **single domain** of the base URL is crawled. Other domains **and subdomains** are never crawled.
- It must be as fast as practical, using every sensible concurrency pattern, **without sacrificing accuracy or wasting compute resources**.
- Scrapy and Playwright (or any framework/headless browser of that kind) are **not allowed**. Libraries for HTTP, HTML parsing and similar tasks are allowed.
- The submission is judged on design, code quality, structure, testing and how the work was done (tools, AI usage, verification).

## 2. Goals and non-goals

### Goals

1. Correct, complete crawl of every reachable HTML page on the target host.
2. High throughput through bounded asynchronous concurrency.
3. Polite behaviour: robots.txt, rate controls, honest User-Agent, bounded retries.
4. Clean architecture where core logic is testable without a network.
5. A repository set up like a production project (typing, linting, benchmarks).
6. A README that explains the decisions, the alternatives considered, the trade-offs and the future extensions.

### Non-goals (documented in the README, each with a short "how I would extend it" note)

| Non-goal | Extension note for the README |
|---|---|
| JavaScript-rendered content | Needs a headless browser, which is forbidden here. In production, add a rendering tier behind the same `Fetcher` interface. |
| Authentication, cookies, sessions | Inject a configured `aiohttp` session or cookie jar into the fetcher. |
| `sitemap.xml` seeding | Extra source of seed URLs. Improves coverage of orphan pages. |
| `<meta http-equiv="refresh">` redirects | Extractor could surface them as links. |
| Multiple domains at once | A per-host scheduler with per-host politeness state and a global concurrency budget. |
| Persistence and resume | Frontier and visited set backed by SQLite or Redis. |
| Distributed crawling | Shared frontier (Redis or SQS), consistent hashing of hosts to workers. |
| An alternative to the CLI | A long-running service with an HTTP API and job queue, streaming results to storage or a message bus, plus metrics (Prometheus). Explain why a CLI is a poor fit for multi-domain, scheduled or long-lived crawls. |

## 3. Behaviour

### 3.1 Scope: what is "the same site"

- The seed URL must be an absolute `http` or `https` URL. Otherwise the program exits with code 2.
- A URL is **in scope** when its host equals the seed's host after normalisation: lowercase, IDNA/punycode, trailing dot removed, default ports (80/443) removed.
- Matching is **strict**. `example.com` and `www.example.com` are different sites, and so are `example.com` and `blog.example.com`. Out-of-scope URLs are listed as links found on a page but are never fetched.
- A non-default port is part of the identity (`example.com:8080` is not `example.com`).
- Scheme: `http` and `https` variants of the in-scope host are both in scope. For deduplication, in-scope URLs are keyed using the seed's scheme, so `http://host/a` and `https://host/a` are the same page when the seed was `https`.

### 3.2 Link extraction

- Extract the `href` of every `<a>` element. Honour `<base href>` when resolving relative URLs.
- Resolve relative references to absolute URLs (`urllib.parse.urljoin` semantics).
- Keep only `http` and `https` schemes. Drop `mailto:`, `tel:`, `javascript:`, `data:` and similar.
- Strip `#fragments`. Keep query strings, because they can change the content.
- Do **not** rewrite anything else: no trailing-slash folding, no query-parameter sorting. Lowercase the scheme and host only.
- Skip empty and whitespace-only `href` values. Skip URLs longer than 2048 characters (guards against crawler traps).
- Per page, deduplicate links and preserve **document order**.

### 3.3 Which responses count as pages

- Only responses with `Content-Type: text/html` (or `application/xhtml+xml`) are parsed.
- Other in-scope URLs (PDFs, images, archives) are still listed as links found on pages, but they are **not downloaded in full**: if a `Content-Type` is known up front (via the response headers) the body is not read.
- Response bodies larger than **5 MB** are aborted and the page is reported as failed (`too large`).
- Pages that fail (4xx, 5xx after retries, timeout, non-HTML, too large) are **not** printed on stdout. They are reported on stderr and counted in the summary.

### 3.4 Redirects

- Follow up to **5** redirects per request, checking the host at **every hop**.
- A redirect to an out-of-scope host is not followed. It is reported on stderr as `redirected off-site` and counted as skipped.
- If the fetch succeeds after same-host redirects, the page is reported under its **final** URL. Both the requested URL and the final URL are marked visited.
- If the **seed** redirects off-site, the crawl ends with a clear error message naming the target (for example `example.com` → `www.example.com`), and exit code 1.

### 3.5 Crawl limits

- `--max-pages N`: stop scheduling new pages once N pages have been scheduled. In-flight fetches finish normally.
- `--max-depth D`: the seed is depth 0. Links found on a page at depth D are not enqueued.
- Both are unbounded by default.

## 4. Concurrency design

**Model: `asyncio` with a fixed pool of worker tasks consuming a shared queue.**

- `N` workers (default **20**, `--concurrency`) loop on `queue.get()`, fetch, extract, emit results and enqueue new in-scope URLs.
- One shared `aiohttp.ClientSession` with a `TCPConnector(limit=N, ttl_dns_cache=300)` for keep-alive connection reuse. Install `aiodns` through `aiohttp[speedups]` for async DNS.
- **Dedupe at enqueue time.** A `visited` set of normalised URLs is checked and updated in the same synchronous step as `queue.put_nowait()`. There is no `await` between check and add, so on a single-threaded event loop it is atomic and no lock is needed. This guarantees a page is never fetched twice.
- **Termination via `queue.join()`.** Each worker calls `task_done()` only after it has finished a page *and* enqueued that page's links. The unfinished counter therefore reaches zero only when the queue is empty and no worker is busy. At that point the workers are cancelled and the summary is printed.
- **Unbounded queue.** Workers both consume and produce, so a bounded queue could deadlock when full. The memory cost of the `visited` set on very large sites is a documented trade-off (Bloom filter or disk-backed frontier as the extension).
- **Parsing inline.** `selectolax` parsing runs on the event loop because pages are small and parsing is fast. A process pool for parsing very large documents is listed as a possible refinement.
- **Graceful shutdown.** On `SIGINT` the crawler stops scheduling, lets in-flight requests finish (or cancels after a short grace period), prints the summary and exits with code 130.

### Rationale to record in the README

| Option | Verdict |
|---|---|
| `asyncio` workers | Chosen: I/O-bound workload, lowest memory per connection, easy to cap. |
| Thread pool | More memory and context switching per connection, and shared-state locking. |
| Multiprocessing | Heavy IPC, and the shared visited set is hard. Useful only if CPU-bound parsing dominated. |
| `httpx` instead of `aiohttp` | Cleaner API and HTTP/2, but measurably slower at high concurrency. |
| `BeautifulSoup`/`lxml` instead of `selectolax` | Slower, or heavier API, for a simple link-extraction task. |

## 5. Politeness and resilience

| Concern | Behaviour |
|---|---|
| robots.txt | Fetched once for the seed host and cached. Respect `Disallow`/`Allow` for the crawler's User-Agent and `*`. Respect `Crawl-delay` using a shared rate gate that enforces a minimum interval between request starts. `--ignore-robots` disables all of it. A 4xx on robots.txt means allow all. A 5xx or timeout means disallow all, with a warning (RFC 9309). If the seed itself is disallowed, exit with code 1. |
| User-Agent | Honest and identifiable: `web-crawler/<version> (+<project URL>)`, overridable with `--user-agent`. |
| Timeouts | Total per-request timeout of 10 s (`--timeout`). |
| Retries | At most 2 retries for timeouts, connection errors and 5xx, with exponential backoff and jitter. |
| Rate limiting | `429` and `503` with `Retry-After` are honoured (capped at 60 s). |
| Response size | 5 MB cap, enforced while streaming the body. |
| Resource use | Fixed worker count and connection limit. No per-URL tasks are spawned. |

## 6. Command-line interface

```
web-crawler [options] URL
python -m web_crawler [options] URL
```

| Option | Default | Description |
|---|---|---|
| `URL` | required | Absolute http(s) base URL. |
| `--concurrency N` | 20 | Number of concurrent workers and the connection limit. |
| `--max-pages N` | unbounded | Stop scheduling after N pages. |
| `--max-depth D` | unbounded | Maximum link depth from the seed. |
| `--timeout SECONDS` | 10 | Per-request timeout. |
| `--ignore-robots` | off | Do not fetch or obey robots.txt. |
| `--json` | off | Emit JSON Lines instead of text. |
| `--user-agent STRING` | built-in | Override the User-Agent. |
| `-v`, `--verbose` | off | Debug logging on stderr. |
| `--version`, `-h` | | Standard. |

The CLI uses stdlib `argparse` (no extra dependency).

### Output

- **stdout carries results only**, so the output can be piped. Logs, per-page errors and the summary go to **stderr**.
- Results are **streamed** as each page completes. Order is therefore completion order and is not deterministic. Tests compare sets, not sequences.
- Default text format, one block per page:

  ```
  https://example.com/about
      https://example.com/
      https://example.com/team
      https://twitter.com/example

  ```

  The page URL starts at column 0, links are indented four spaces, and a blank line follows each block. A page with no links prints just its URL line.

- `--json` format, one object per line: `{"url": "...", "links": ["...", "..."]}`.
- Final summary on stderr: pages crawled, failed, skipped (robots, off-site redirect), elapsed time and pages per second.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Crawl completed (individual page failures do not change the exit code). |
| 1 | Fatal error: seed unreachable, seed off-site redirect, seed disallowed by robots.txt. |
| 2 | Usage error (invalid URL, bad option). |
| 130 | Interrupted by the user. |

## 7. Architecture

Source layout (`src/` layout, package `web_crawler`):

```
web-crawler/
├── pyproject.toml
├── README.md
├── SPEC.md
├── src/web_crawler/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py          # argument parsing and wiring, exit codes
│   ├── crawler.py      # orchestrator: workers, fetching, shutdown
│   ├── frontier.py     # queue of pages to crawl: dedupe, limits, a may-crawl rule, termination
│   ├── fetcher.py      # Fetcher protocol + AiohttpFetcher (retries, size cap, redirects)
│   ├── extractor.py    # HTML in, ordered unique absolute links out
│   ├── urls.py         # normalisation, same-host check, link filtering
│   ├── robots.py       # robots.txt parser, policy and loader
│   ├── rate_gate.py    # minimum interval between requests (crawl-delay)
│   ├── sink.py         # ResultSink protocol + TextSink, JsonLinesSink
│   └── models/         # one frozen dataclass per file, re-exported by models/__init__.py
│       ├── crawl_config.py, crawl_stats.py, page_result.py, robots_fetch.py
│       └── fetched_page.py, fetch_failure.py, fetch_skipped.py, fetch_outcome.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
└── benchmarks/
    └── bench_concurrency.py
```

Design principles:

- **Pure functions where possible** (`urls`, `extractor`). They hold most of the logic and are trivially unit-testable.
- **I/O behind protocols.** `Fetcher` and `ResultSink` are `typing.Protocol`s, so the crawler core is tested with in-memory fakes and no network.
- **Dependency injection.** `Crawler` receives its `Fetcher`, `ResultSink`, `RobotsPolicy` and `CrawlConfig`. `cli.py` is the only place that wires concrete implementations.
- **Immutable data models** (`@dataclass(frozen=True, slots=True)`).
- **Typed errors.** The fetcher returns a result type (or raises a small hierarchy of `FetchError` subclasses) so that the crawler decides what to log and count.

### Dependencies

| Package | Use |
|---|---|
| `aiohttp[speedups]` | Async HTTP client (includes `aiodns` and Brotli support) |
| `selectolax` | Fast HTML parsing |

Dev: `pytest`, `pytest-asyncio`, `pytest-aiohttp`, `pytest-cov`, `ruff`, `mypy`.

## 8. Project tooling and quality bar

- Python **3.12+**. Dependency and environment management with **`uv`**, configured in `pyproject.toml`, with a committed lockfile.
- **`ruff`** for linting and formatting.
- **`mypy --strict`** passes with no ignores unless justified in a comment.
- **`pytest`** with `pytest-asyncio` and `pytest-aiohttp`. Coverage reported, with a target of at least 90% on `src/`.
- **No CI workflows and no Makefile.** The repository is private and every task is a one-line `uv run` command, which the README's Development section lists.
- Commits are small and leave the test suite green. **No co-author trailers** are added to commit messages.

## 9. Testing strategy

All tests run **offline**. No test touches the real internet.

### Unit tests

- `urls`: normalisation, IDNA hosts, default ports, strict host matching (`www`, subdomains, ports), scheme canonicalisation, fragment stripping, query preservation, scheme filtering, over-length URLs.
- `extractor`: relative and absolute links, `<base href>`, duplicate links, document order, non-`<a>` elements ignored, malformed HTML, empty or whitespace `href`.
- `robots`: allow/disallow rules, wildcard and `$` patterns, crawl-delay, 4xx versus 5xx behaviour.
- `sink`: exact text format and JSON Lines format.
- `fetcher`: retry and backoff behaviour, `Retry-After`, size cap, redirect host checks, non-HTML short-circuit. Tested against a local `aiohttp` test server.

### Crawler core tests (with a fake `Fetcher`)

- Visits every reachable page exactly once on a graph with cycles, diamonds and self-links.
- Never fetches out-of-scope URLs, but lists them as links.
- Respects `--max-pages` and `--max-depth`.
- Terminates on an empty frontier and on a single-page site.
- Concurrency never exceeds the configured limit (tracked by the fake).
- A failing page does not stop the crawl, and it is counted.
- Race check: two pages linking to the same URL cause one fetch.

### Integration tests

- A local `aiohttp` test site served in-process, covering: redirects (same-host and off-site), 404s, 500s with retry, `429` with `Retry-After`, slow responses and timeouts, huge bodies, non-HTML assets, robots.txt rules and crawl-delay, a trap-like infinite path (bounded by `--max-pages`).
- CLI end-to-end through `subprocess` or `cli.main()` against the local site, asserting stdout, stderr summary and exit codes.

## 10. Benchmark

`benchmarks/bench_concurrency.py` provides evidence for the speed claim:

- Starts a local synthetic site (configurable page count, links per page, artificial per-request latency, for example 50 ms).
- Crawls it with concurrency **1, 5, 20 and 50** (concurrency 1 is the sequential baseline), repeating each a few times.
- Reports pages per second, total time, peak memory and speed-up versus the baseline.
- Results and the shape of the curve (where returns diminish and why) go into the README with the machine specification.
- Runs with `uv run python -m benchmarks.bench_concurrency`. It is not part of the normal test run.

## 11. README requirements

The README must contain:

1. **Overview and quick start:** install, run, example output.
2. **Usage and options**, including exit codes.
3. **Architecture:** module diagram and responsibilities.
4. **Design decisions and trade-offs**, with the options considered and the reasoning:
   - `asyncio` versus threads versus processes
   - `aiohttp` versus `httpx`
   - `selectolax` versus `lxml`/`BeautifulSoup`
   - dedupe at enqueue time and the `queue.join()` termination pattern
   - unbounded queue and visited-set memory
   - strict host matching and the handling of redirects
   - what counts as a page, and the normalisation rules (and what was deliberately not normalised)
   - streaming, non-deterministic output order
   - politeness choices
5. **Benchmark results** and interpretation.
6. **Testing:** how to run, what is covered, how the offline test site works.
7. **Limitations and known issues** (the non-goals table).
8. **What I would do with more time**, including:
   - multi-domain crawling and per-host scheduling
   - persistent or distributed frontier
   - sitemap seeding
   - a process pool for parsing
   - Bloom filter for the visited set
   - metrics and tracing
   - a service/API (plus a job queue) as a better home than a CLI for multi-domain, scheduled or long-running crawls
9. **Tools and workflow (to be completed by the author).** This section is deliberately left as a placeholder in the spec because it must be written truthfully by the author. It should cover:
   - IDE and extensions
   - interactive AI assistants (for example Copilot) and other AI tools used, and for which tasks
   - **plugins, skills, MCP servers and other extensions** used with those assistants: name each one and say what it was used for (for example a skill used to clarify requirements before the spec was written, a code-review skill, a test-running or browser tool)
   - how the subject matter was researched
   - how the generated code was reviewed and verified (tests, type checks, benchmarks, manual runs)
   - A note that this specification was drafted with Claude Code, with its requirements clarified through the `grilling` skill. Add every plugin and skill used later during implementation.

## 12. Milestones

Each milestone is a small commit (or a few) that leaves lint, types and tests green.

| # | Milestone | Done when |
|---|---|---|
| M0 | Scaffold: `pyproject.toml`, uv lockfile, `src/` layout, ruff, mypy, pytest | `ruff`, `mypy` and `pytest` pass on an empty package |
| M1 | `models` and `urls` with unit tests | URL normalisation and scoping tests pass |
| M2 | `extractor` with unit tests | Link-extraction tests pass |
| M3 | `sink` (text and JSON Lines) | Output-format tests pass |
| M4 | `fetcher` (`aiohttp`): timeouts, retries, size cap, redirect host checks | Fetcher tests against the local test server pass |
| M5 | `crawler` core with a fake fetcher: workers, dedupe, termination, limits | Crawler core tests pass, including the race test |
| M6 | `robots` policy and crawl-delay gate | Robots tests pass and the crawler honours the policy |
| M7 | `cli`, exit codes, stderr summary, signal handling | CLI end-to-end tests pass |
| M8 | Integration test site and scenarios | All integration tests pass |
| M9 | Benchmark script and measurements | The benchmark script produces the table for the README |
| M10 | README | A fresh clone can install, run and read the full documentation |

## 13. Acceptance criteria

1. `web-crawler https://<site>` prints, for each crawled page, the page URL and the full list of URLs found on it, and finishes by itself.
2. No request is ever made to a different host or subdomain, and no page is fetched twice.
3. Every reachable in-scope HTML page is reported exactly once (verified on the integration test site, including cycles).
4. Crawling the benchmark site at concurrency 20 is substantially faster than at concurrency 1, and the README shows the numbers.
5. robots.txt, timeouts, retries, size cap and redirect rules behave as specified in sections 3 and 5.
6. `ruff`, `mypy --strict` and the full test suite pass locally, with at least 90% coverage.
7. The README covers all the points in section 11, including the author-completed tools and workflow section (IDE, AI assistants, plugins and skills).

## 14. Assumptions and defaults to confirm

These were chosen by the spec author as sensible defaults and can be changed without affecting the rest of the design:

- Default concurrency 20, timeout 10 s, 5 MB body cap, 2 retries, 5 redirects, 2048-character URL cap, `Retry-After` cap 60 s.
- Scheme canonicalisation for dedupe (section 3.1).
- Robots.txt 5xx handling follows RFC 9309 (assume disallow).
- Coverage target of 90%.
