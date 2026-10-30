import pytest

from web_crawler.urls import MAX_URL_LENGTH, SiteScope, normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Path#frag", "https://example.com/Path"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com?x=1", "https://example.com/?x=1"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/", "http://example.com/"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("http://example.com:443/a", "http://example.com:443/a"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),
        ("https://example.com/a/", "https://example.com/a/"),
        ("https://example.com/a", "https://example.com/a"),
        ("https://example.com./", "https://example.com/"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("https://user:pw@Example.com/", "https://user:pw@example.com/"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "mailto:someone@example.com",
        "tel:+351000000",
        "javascript:void(0)",
        "data:text/html,hi",
        "ftp://example.com/file",
        "/relative/path",
        "https://",
        "http://[::1",
        "https://exa..mple.com/",
        "https://example.com:notaport/",
        "https://example.com/" + "a" * MAX_URL_LENGTH,
    ],
)
def test_normalize_url_rejects_unusable_urls(raw: str) -> None:
    assert normalize_url(raw) is None


class TestSiteScope:
    scope = SiteScope("https://example.com/start")

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/",
            "https://EXAMPLE.com/about#team",
            "http://example.com/about",  # other scheme, same host
            "https://example.com:443/about",
        ],
    )
    def test_same_host_is_in_scope(self, url: str) -> None:
        assert self.scope.contains(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.example.com/",
            "https://blog.example.com/",
            "https://example.com.evil.test/",
            "https://notexample.com/",
            "https://example.com:8443/",
            "https://other.test/",
            "mailto:a@example.com",
            "/relative",
        ],
    )
    def test_other_hosts_are_out_of_scope(self, url: str) -> None:
        assert not self.scope.contains(url)

    def test_canonical_url_uses_the_seed_scheme(self) -> None:
        url = self.scope.canonical_url("http://example.com/a?b=1#x")
        assert url == "https://example.com/a?b=1"

    def test_http_and_https_forms_share_a_canonical_url(self) -> None:
        http = self.scope.canonical_url("http://example.com/a")
        assert http == self.scope.canonical_url("https://example.com/a")

    def test_canonical_url_is_none_out_of_scope(self) -> None:
        assert self.scope.canonical_url("https://www.example.com/") is None

    def test_origin(self) -> None:
        assert SiteScope("https://Example.com:8443/x").origin == "https://example.com:8443"

    def test_www_seed_does_not_cover_the_apex(self) -> None:
        assert not SiteScope("https://www.example.com/").contains("https://example.com/")

    def test_rejects_a_bad_seed(self) -> None:
        with pytest.raises(ValueError, match="absolute http"):
            SiteScope("example.com")
