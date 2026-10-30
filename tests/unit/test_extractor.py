from web_crawler.extractor import extract_links

PAGE = "https://example.com/dir/page"


def test_resolves_relative_and_absolute_links_in_document_order() -> None:
    html = """
    <a href="/root">1</a>
    <a href="sibling">2</a>
    <a href="../up">3</a>
    <a href="https://other.test/x">4</a>
    <a href="//cdn.test/y">5</a>
    """
    assert extract_links(html, PAGE) == (
        "https://example.com/root",
        "https://example.com/dir/sibling",
        "https://example.com/up",
        "https://other.test/x",
        "https://cdn.test/y",
    )


def test_duplicates_collapse_keeping_the_first_position() -> None:
    html = '<a href="/a">1</a><a href="/b">2</a><a href="/a">3</a><a href="/a#frag">4</a>'
    assert extract_links(html, PAGE) == ("https://example.com/a", "https://example.com/b")


def test_fragments_are_stripped_but_queries_are_kept() -> None:
    html = '<a href="/a?x=2&amp;y=1#top">1</a>'
    assert extract_links(html, PAGE) == ("https://example.com/a?x=2&y=1",)


def test_non_http_schemes_are_dropped() -> None:
    html = """
    <a href="mailto:a@example.com">m</a>
    <a href="tel:+351000000">t</a>
    <a href="javascript:void(0)">j</a>
    <a href="data:text/plain,hi">d</a>
    <a href="ftp://example.com/f">f</a>
    <a href="/kept">k</a>
    """
    assert extract_links(html, PAGE) == ("https://example.com/kept",)


def test_empty_and_whitespace_hrefs_are_skipped() -> None:
    html = '<a href="">1</a><a href="   ">2</a><a href=" /a ">3</a><a>4</a>'
    assert extract_links(html, PAGE) == ("https://example.com/a",)


def test_only_anchor_elements_count() -> None:
    html = """
    <link href="/style.css" rel="stylesheet">
    <img src="/logo.png">
    <script src="/app.js"></script>
    <area href="/area">
    <a href="/real">r</a>
    """
    assert extract_links(html, PAGE) == ("https://example.com/real",)


def test_base_href_changes_how_relative_links_resolve() -> None:
    html = '<head><base href="https://example.com/other/"></head><a href="x">1</a>'
    assert extract_links(html, PAGE) == ("https://example.com/other/x",)


def test_relative_base_href_resolves_against_the_page() -> None:
    html = '<head><base href="/docs/"></head><a href="x">1</a>'
    assert extract_links(html, PAGE) == ("https://example.com/docs/x",)


def test_non_http_base_href_is_ignored() -> None:
    html = '<head><base href="javascript:void(0)"></head><a href="x">1</a>'
    assert extract_links(html, PAGE) == ("https://example.com/dir/x",)


def test_malformed_html_still_yields_links() -> None:
    html = '<div><a href="/a">unclosed <p><a href="/b">also <b>unclosed'
    assert extract_links(html, PAGE) == ("https://example.com/a", "https://example.com/b")


def test_unusable_urls_are_skipped_not_fatal() -> None:
    html = '<a href="http://[::1">bad</a><a href="/ok">ok</a>'
    assert extract_links(html, PAGE) == ("https://example.com/ok",)


def test_a_broken_base_href_falls_back_to_the_page_url() -> None:
    html = '<head><base href="http://[::1"></head><a href="x">1</a>'
    assert extract_links(html, PAGE) == ("https://example.com/dir/x",)


def test_empty_documents_have_no_links() -> None:
    assert extract_links("", PAGE) == ()
    assert extract_links("   \n", PAGE) == ()
