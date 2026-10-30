"""Turn an HTML document into its ordered, de-duplicated list of absolute links."""

from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from web_crawler.urls import normalize_url


def extract_links(html: str, page_url: str) -> tuple[str, ...]:
    """Return every http(s) link in `html`, absolute, fragment-free, in document order."""
    if not html.strip():
        return ()
    tree = LexborHTMLParser(html)
    base = _base_url(tree, page_url)

    links: dict[str, None] = {}  # a dict is an ordered set
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        link = _resolve(base, href)
        if link is not None:
            links.setdefault(link)
    return tuple(links)


def _resolve(base: str, href: str) -> str | None:
    try:
        return normalize_url(urljoin(base, href))
    except ValueError:  # urljoin itself rejects things like "http://[::1"
        return None


def _base_url(tree: LexborHTMLParser, page_url: str) -> str:
    node = tree.css_first("base[href]")
    if node is None:
        return page_url
    href = (node.attributes.get("href") or "").strip()
    if not href:
        return page_url
    try:
        base = urljoin(page_url, href)
        return base if urlsplit(base).scheme in {"http", "https"} else page_url
    except ValueError:
        return page_url
