"""URL normalization and the "same site" rule.

Normalization is deliberately minimal: the scheme and host are lowercased, the host is
converted to punycode, default ports and fragments are dropped, and an empty path becomes
"/" (RFC 3986 says they are equivalent). Nothing else is rewritten: query strings keep
their order and trailing slashes are never folded, because both can change the content.
"""

from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

_HTTP_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str | None:
    """Return the normalized absolute http(s) URL, or None if it is unusable."""
    candidate = url.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(candidate)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in _HTTP_SCHEMES or not hostname:
        return None
    host = _normalize_host(hostname)
    if host is None:
        return None

    netloc = _build_netloc(parts.netloc, host, port, scheme)
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _normalize_host(hostname: str) -> str | None:
    host = hostname.rstrip(".").lower()
    if not host:
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _build_netloc(original: str, host: str, port: int | None, scheme: str) -> str:
    """Rebuild the authority from the normalized host, keeping credentials and a custom port."""
    userinfo, at, _ = original.rpartition("@")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    return f"{userinfo}@{netloc}" if at else netloc


def _host_and_port(netloc: str) -> str:
    return netloc.rpartition("@")[2]


class SiteScope:
    """Decides whether a URL belongs to the site being crawled.

    Matching is strict: the host and effective port must equal the seed's, so
    `www.example.com` and `blog.example.com` are different sites from `example.com`.
    """

    def __init__(self, seed: str) -> None:
        normalized = normalize_url(seed)
        if normalized is None:
            raise ValueError(f"not an absolute http(s) URL: {seed!r}")
        parts = urlsplit(normalized)
        self._scheme = parts.scheme
        self._host_and_port = _host_and_port(parts.netloc)

    @property
    def origin(self) -> str:
        """Scheme and authority of the seed, for building the robots.txt URL."""
        return f"{self._scheme}://{self._host_and_port}"

    def canonical_url(self, url: str) -> str | None:
        """The one form of an in-scope URL used to compare pages, or None if out of scope.

        The seed's scheme is used so that the http and https forms of one page are a
        single entry for deduplication (and the form that is fetched).
        """
        normalized = normalize_url(url)
        if normalized is None:
            return None
        parts = urlsplit(normalized)
        if _host_and_port(parts.netloc) != self._host_and_port:
            return None
        return urlunsplit((self._scheme, parts.netloc, parts.path, parts.query, ""))

    def contains(self, url: str) -> bool:
        return self.canonical_url(url) is not None
