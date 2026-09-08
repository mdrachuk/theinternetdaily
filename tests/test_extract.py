"""The parser pool: a native crash in one page costs that page, not the server."""
import os

import httpx
import pytest

from tid import extract


def _abort(*_args) -> None:
    os.abort()  # SIGABRT, as glibc does on a double free


def _length(html: str) -> int:
    return len(html)


async def test_crash_in_parser_names_the_page_and_the_pool_recovers():
    with pytest.raises(extract.ParserCrashed):
        await extract.in_parser_process(_abort, "<html/>")
    # The pool was rebuilt: the next parse runs as if nothing happened.
    assert await extract.in_parser_process(_length, "abcd") == 4


async def test_extract_reports_the_url_on_a_crash(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="<html><body>x</body></html>",
                              headers={"content-type": "text/html"})
    monkeypatch.setattr(extract, "_parse", _abort)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(extract.ParserCrashed, match="https://x.test/p"):
            await extract.extract(c, "https://x.test/p", "t", "s")


async def test_extract_still_parses_a_real_page():
    body = "<p>" + "Real prose about something. " * 20 + "</p>"
    def handler(request):
        return httpx.Response(200, text=f"<html><body><article>{body}</article></body></html>",
                              headers={"content-type": "text/html"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        art = await extract.extract(c, "https://x.test/p", "t", "s")
    assert art is not None and "Real prose" in art.text
