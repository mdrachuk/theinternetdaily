"""Source marks: where the favicon comes from, and where it does not go.

The design puts a favicon in every byline. The privacy property that makes
that acceptable — the reader's browser only ever talks to this origin — is
worth a test, because it is invisible in the rendered page.
"""
from __future__ import annotations

import feedparser
import httpx
import pytest

from tid import icons
from tid.fetch import _entry_image


# --- what counts as a domain ----------------------------------------------

@pytest.mark.parametrize("value", [
    "lwn.net", "news.ycombinator.com", "a-b.co.uk", "xn--80ak6aa92e.com",
])
def test_real_domains_are_accepted(value):
    assert icons.is_domain(value)


@pytest.mark.parametrize("value", [
    "", "localhost", "../../etc/passwd", "a b.com", "lwn.net/x",
    "lwn.net?sz=64", "-lwn.net", "lwn.net.", "LWN.net", "a" * 300 + ".com",
])
def test_anything_else_is_not(value):
    assert not icons.is_domain(value)


@pytest.mark.parametrize("url,expected", [
    ("https://www.theguardian.com/x/y", "theguardian.com"),
    ("http://lwn.net", "lwn.net"),
    ("https://news.ycombinator.com/item?id=1", "news.ycombinator.com"),
    ("not a url", ""),
    ("file:///etc/passwd", ""),
    ("", ""),
])
def test_domain_for(url, expected):
    assert icons.domain_for(url) == expected


def test_icon_url_stays_on_this_origin():
    assert icons.icon_url("https://lwn.net/a") == "/icon/lwn.net.png"
    assert icons.icon_url("mailto:x@y") == ""


# --- fetching and caching -------------------------------------------------

async def test_fetch_icon_asks_google_about_the_domain_never_the_domain_itself(
    tmp_path,
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=icons.BLANK_PNG)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await icons.fetch_icon(client, tmp_path, "lwn.net")

    assert out is not None and out.read_bytes() == icons.BLANK_PNG
    (request,) = seen
    assert request.url.host == "www.google.com"
    assert request.url.params["domain"] == "lwn.net"


async def test_a_cached_icon_is_not_fetched_twice(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=icons.BLANK_PNG)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await icons.fetch_icon(client, tmp_path, "lwn.net")
        await icons.fetch_icon(client, tmp_path, "lwn.net")
    assert calls == 1


async def test_a_failed_lookup_caches_the_blank_rather_than_retrying_forever(
    tmp_path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await icons.fetch_icon(client, tmp_path, "nothing.invalid")
    assert out is not None and out.read_bytes() == icons.BLANK_PNG


async def test_fetch_icon_refuses_a_non_domain(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: pytest.fail("must not reach the network")
    )) as client:
        assert await icons.fetch_icon(client, tmp_path, "../etc") is None


# --- where article images come from ---------------------------------------

def _entry(xml: str):
    return feedparser.parse(
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/"><channel>'
        f"<item><title>T</title><link>http://x.test/1</link>{xml}</item>"
        "</channel></rss>"
    ).entries[0]


def test_media_thumbnail_wins():
    entry = _entry(
        '<media:thumbnail url="http://x.test/thumb.jpg"/>'
        '<description>&lt;img src="http://x.test/inline.jpg"&gt;</description>'
    )
    assert _entry_image(entry) == "http://x.test/thumb.jpg"


def test_an_image_enclosure_is_used():
    entry = _entry('<enclosure url="http://x.test/a.jpg" type="image/jpeg"/>')
    assert _entry_image(entry) == "http://x.test/a.jpg"


def test_a_podcast_enclosure_is_not_an_image():
    entry = _entry('<enclosure url="http://x.test/a.mp3" type="audio/mpeg"/>')
    assert _entry_image(entry) is None


def test_the_first_inline_img_is_the_last_resort():
    entry = _entry(
        "<description>&lt;p&gt;Hi&lt;/p&gt;"
        '&lt;img src="http://x.test/inline.jpg?a=1&amp;amp;b=2"&gt;</description>'
    )
    assert _entry_image(entry) == "http://x.test/inline.jpg?a=1&b=2"


def test_an_entry_with_no_image_yields_none():
    assert _entry_image(_entry("<description>Just words.</description>")) is None
