"""A synthetic site with a fixed per-request latency, for benchmarking the crawler.

Run on its own it serves the site until killed:

    python -m benchmarks.synthetic_site --port 8099 --pages 400 --latency-ms 50

Page `i` is served at /page/i and links to page i+1 (so every page is reachable from
/page/0) plus a few pseudo-random pages, so the link graph has cycles and diamonds.
"""

import argparse
import asyncio
import random

from aiohttp import web


def build_adjacency(pages: int, links_per_page: int, seed: int = 42) -> list[list[int]]:
    rng = random.Random(seed)
    return [
        [(i + 1) % pages, *(rng.randrange(pages) for _ in range(links_per_page - 1))]
        for i in range(pages)
    ]


def build_app(pages: int, links_per_page: int, latency: float) -> web.Application:
    adjacency = build_adjacency(pages, links_per_page)
    bodies = [
        "<html><body>" + "".join(f'<a href="/page/{j}">p{j}</a>' for j in links) + "</body></html>"
        for links in adjacency
    ]

    async def page(request: web.Request) -> web.Response:
        index = int(request.match_info["n"])
        if not 0 <= index < pages:
            raise web.HTTPNotFound
        await asyncio.sleep(latency)
        return web.Response(text=bodies[index], content_type="text/html")

    app = web.Application()
    app.router.add_get("/page/{n}", page)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pages", type=int, default=400)
    parser.add_argument("--links-per-page", type=int, default=8)
    parser.add_argument("--latency-ms", type=float, default=50.0)
    args = parser.parse_args()
    app = build_app(args.pages, args.links_per_page, args.latency_ms / 1000)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
