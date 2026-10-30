"""Where results go. Sinks write to stdout only; logs and errors never mix into it."""

import json
from typing import Protocol, TextIO

from web_crawler.models import PageResult


class ResultSink(Protocol):
    def emit(self, result: PageResult) -> None: ...


class _StreamSink:
    """Writes text to a stream and flushes, so each page appears as soon as it is found."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def _write(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()


class TextSink(_StreamSink):
    """The page URL on its own line, its links indented, then a blank line."""

    def emit(self, result: PageResult) -> None:
        lines = [result.url, *(f"    {link}" for link in result.links)]
        self._write("\n".join(lines) + "\n\n")


class JsonLinesSink(_StreamSink):
    """One JSON object per page: {"url": "...", "links": ["..."]}."""

    def emit(self, result: PageResult) -> None:
        record = {"url": result.url, "links": list(result.links)}
        self._write(json.dumps(record) + "\n")
