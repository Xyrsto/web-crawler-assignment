import io
import json

from web_crawler.models import PageResult
from web_crawler.sink import JsonLinesSink, TextSink


def test_text_sink_format() -> None:
    stream = io.StringIO()
    TextSink(stream).emit(
        PageResult("https://example.com/about", ("https://example.com/", "https://x.test/"))
    )
    assert stream.getvalue() == (
        "https://example.com/about\n    https://example.com/\n    https://x.test/\n\n"
    )


def test_text_sink_page_without_links_prints_only_its_url() -> None:
    stream = io.StringIO()
    TextSink(stream).emit(PageResult("https://example.com/", ()))
    assert stream.getvalue() == "https://example.com/\n\n"


def test_text_sink_separates_blocks_with_a_blank_line() -> None:
    stream = io.StringIO()
    sink = TextSink(stream)
    sink.emit(PageResult("https://example.com/a", ("https://example.com/b",)))
    sink.emit(PageResult("https://example.com/b", ()))
    assert stream.getvalue() == (
        "https://example.com/a\n    https://example.com/b\n\nhttps://example.com/b\n\n"
    )


def test_json_lines_sink_writes_one_object_per_line() -> None:
    stream = io.StringIO()
    sink = JsonLinesSink(stream)
    sink.emit(PageResult("https://example.com/a", ("https://example.com/b",)))
    sink.emit(PageResult("https://example.com/b", ()))
    lines = stream.getvalue().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"url": "https://example.com/a", "links": ["https://example.com/b"]},
        {"url": "https://example.com/b", "links": []},
    ]


def test_json_lines_sink_escapes_awkward_characters() -> None:
    stream = io.StringIO()
    JsonLinesSink(stream).emit(PageResult('https://example.com/"q"\\é', ()))
    assert json.loads(stream.getvalue())["url"] == 'https://example.com/"q"\\é'
    assert stream.getvalue().count("\n") == 1
