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
from tid.sources.rss import entry_image as _entry_image


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


# Google returns whatever the site publishes. A byte-exact JPEG header is
# enough to stand in for one; the browser never sees the fake pixels.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60


async def test_a_jpeg_mark_is_kept_not_thrown_away(tmp_path):
    """A third of the blank bylines in production were sites whose favicon
    Google hands back as JPEG. The PNG-only check turned all of them into
    the blank; any image a browser can paint is a mark."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=JPEG,
                                 headers={"content-type": "image/jpeg"}))
    async with httpx.AsyncClient(transport=transport) as client:
        out = await icons.fetch_icon(client, tmp_path, "techcrunch.com")
    assert out is not None and out.read_bytes() == JPEG
    assert icons.media_type(out.read_bytes()) == "image/jpeg"


@pytest.mark.parametrize("blob,expected", [
    (icons.BLANK_PNG, "image/png"),
    (JPEG, "image/jpeg"),
    (b"GIF89a" + b"\x00" * 10, "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"\x00\x00\x01\x00\x01\x00", "image/x-icon"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),
    (b"<!doctype html><title>404</title>", None),
    (b"", None),
])
def test_media_type_by_magic_bytes(blob, expected):
    assert icons.media_type(blob) == expected


async def test_a_stale_blank_is_looked_up_again(tmp_path):
    """A miss is cached for a day, not forever: a blank left by a network
    blip (or by an older tid that discarded JPEGs) heals at the next ingest
    rather than standing until someone deletes the file by hand."""
    import os
    import time

    out = icons.icon_path(tmp_path, "lwn.net")
    out.parent.mkdir(parents=True)
    out.write_bytes(icons.BLANK_PNG)
    assert icons.is_fresh(out), "a blank written just now still stands"

    stale = time.time() - icons.BLANK_TTL - 60
    os.utime(out, (stale, stale))
    assert not icons.is_fresh(out)

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=JPEG))
    async with httpx.AsyncClient(transport=transport) as client:
        await icons.fetch_icon(client, tmp_path, "lwn.net")
    assert out.read_bytes() == JPEG
    assert icons.is_fresh(out), "a real icon never expires"


def test_a_missing_file_is_not_fresh(tmp_path):
    assert not icons.is_fresh(tmp_path / "nope.png")


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


def test_the_widest_advertised_variant_wins():
    """The Guardian offers one photograph at three sizes, smallest first.
    Feed order would print the 140px thumbnail."""
    entry = _entry(
        '<media:content width="140" url="http://x.test/140.jpg"/>'
        '<media:content width="460" url="http://x.test/460.jpg"/>'
        '<media:content width="700" url="http://x.test/700.jpg"/>'
    )
    assert _entry_image(entry) == "http://x.test/700.jpg"


def test_a_wider_media_content_beats_a_widthless_thumbnail():
    entry = _entry(
        '<media:thumbnail url="http://x.test/thumb.jpg"/>'
        '<media:content width="700" url="http://x.test/700.jpg"/>'
    )
    assert _entry_image(entry) == "http://x.test/700.jpg"


def test_a_video_enclosed_as_media_content_is_not_an_image():
    entry = _entry(
        '<media:content medium="video" width="1920" url="http://x.test/a.mp4"/>'
        '<media:content width="700" url="http://x.test/700.jpg"/>'
    )
    assert _entry_image(entry) == "http://x.test/700.jpg"


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


# --- the route: how long a browser may keep what it was handed ------------

async def test_the_route_lets_a_browser_keep_a_blank_only_an_hour(
    tmp_path, monkeypatch
):
    """The disk cache retries a blank after a day. That heal reached nobody:
    the route sent every file with a week's max-age, so a browser that once
    saw the blank kept showing it long after the server had the real mark.
    A real mark may stay a week; a blank an hour, like a failed lookup."""
    from tid import web

    monkeypatch.setenv("TID_CACHE", str(tmp_path))
    icons.icon_path(tmp_path, "blank.example").parent.mkdir(parents=True)
    icons.icon_path(tmp_path, "blank.example").write_bytes(icons.BLANK_PNG)
    icons.icon_path(tmp_path, "mark.example").write_bytes(JPEG)

    transport = httpx.ASGITransport(app=web.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        blank = await c.get("/icon/blank.example.png")
        mark = await c.get("/icon/mark.example.png")

    assert blank.status_code == 200
    assert blank.content == icons.BLANK_PNG
    assert blank.headers["cache-control"] == "public, max-age=3600"
    assert mark.status_code == 200
    assert mark.headers["content-type"] == "image/jpeg"
    assert mark.headers["cache-control"] == "public, max-age=604800"
