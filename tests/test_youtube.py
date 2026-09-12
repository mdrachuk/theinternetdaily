"""`kind = "youtube"`: a channel's uploads, from its Atom feed.

A video is not read. The type resolves the channel's public URL to its id,
takes the feed, and hands the gather a body it already has — the description
— so no watch page is fetched and trafilatura never sees a script bundle. On
the page, the video's own place is a player where an article prints its
photograph, and the drawer says "Watch it here" rather than "Read the full
text".
"""
from __future__ import annotations

import httpx
import pytest

from tid import edition as ed, site, sources
from tid.cli import cmd_gather, collect_current_edition
from tid.sources import Link
from tid.sources import youtube as yt
from tid.store import SqliteStore

CHANNEL = "UCsBjURrPoezykLs9EqgamOA"
HANDLE = "https://www.youtube.com/@Fireship"

CHANNEL_PAGE = (
    "<html><head><title>Fireship</title>"
    f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">'
    "</head><body></body></html>"
)

FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Fireship</title>
 <entry>
  <id>yt:video:aspmNhKAFMc</id>
  <yt:videoId>aspmNhKAFMc</yt:videoId>
  <yt:channelId>{CHANNEL}</yt:channelId>
  <title>OpenAI&apos;s biggest math breakthrough is getting ugly...</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=aspmNhKAFMc"/>
  <published>2026-09-11T17:24:40+00:00</published>
  <media:group>
   <media:thumbnail url="https://i2.ytimg.com/vi/aspmNhKAFMc/hqdefault.jpg" width="480" height="360"/>
   <media:description>OpenAI claims it cracked a 90 year old math problem, but one NYU professor isn't happy about it.

#coding #programming</media:description>
   <media:community><media:statistics views="1137335"/></media:community>
  </media:group>
 </entry>
 <entry>
  <id>yt:video:2Xiljy4xzbc</id>
  <yt:videoId>2Xiljy4xzbc</yt:videoId>
  <yt:channelId>{CHANNEL}</yt:channelId>
  <title>An old one</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=2Xiljy4xzbc"/>
  <published>2020-01-01T00:00:00+00:00</published>
  <media:group>
   <media:thumbnail url="https://i3.ytimg.com/vi/2Xiljy4xzbc/hqdefault.jpg" width="480" height="360"/>
   <media:description></media:description>
  </media:group>
 </entry>
</feed>"""


def _handler(wide: bool = True):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(f"{request.method} {url}")
        if url.startswith(HANDLE):
            return httpx.Response(200, text=CHANNEL_PAGE)
        if url.startswith("https://www.youtube.com/feeds/videos.xml"):
            assert request.url.params["channel_id"] == CHANNEL
            return httpx.Response(200, content=FEED.encode())
        if request.method == "HEAD" and "maxresdefault" in url:
            return httpx.Response(200 if wide else 404)
        # The watch page: a fetch here means the gather tried to read the
        # video as an article, which is exactly what must not happen.
        return httpx.Response(500, text="<html>a script bundle</html>")

    handle.seen = seen  # type: ignore[attr-defined]
    return handle


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _forget_channels():
    yt._resolved.clear()
    yield
    yt._resolved.clear()


# --- resolving the channel ------------------------------------------------

async def test_a_handle_resolves_through_the_channel_page_once():
    handle = _handler()
    async with _client(handle) as client:
        assert await yt.resolve_channel_id(client, HANDLE) == CHANNEL
        assert await yt.resolve_channel_id(client, HANDLE + "/") == CHANNEL
    assert sum(u.startswith("GET " + HANDLE) for u in handle.seen) == 1


async def test_a_channel_link_or_a_bare_id_needs_no_fetch():
    handle = _handler()
    async with _client(handle) as client:
        assert await yt.resolve_channel_id(
            client, f"https://www.youtube.com/channel/{CHANNEL}") == CHANNEL
        assert await yt.resolve_channel_id(client, CHANNEL) == CHANNEL
    assert handle.seen == []


async def test_a_page_with_no_channel_id_is_an_error_not_a_feed_of_nothing():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>consent wall</html>")
    async with _client(handle) as client:
        with pytest.raises(ValueError):
            await yt.resolve_channel_id(client, HANDLE)


# --- the record -----------------------------------------------------------

async def test_the_feed_becomes_items_with_the_description_as_the_body():
    async with _client(_handler()) as client:
        items = await sources.fetch(
            client, {"name": "Fireship", "kind": "youtube", "url": HANDLE}
        )
    new, old = items
    assert new.kind == "youtube" and new.source == "Fireship"
    assert new.title == "OpenAI's biggest math breakthrough is getting ugly..."
    assert new.url == "https://www.youtube.com/watch?v=aspmNhKAFMc"
    assert new.surfaced == "2026-09-11"
    # The still is the extract step's to choose, so the feed's 4:3 is not
    # taken here — the gather would let it win over the wide one.
    assert new.image is None
    assert new.text.startswith("OpenAI claims it cracked")
    assert new.extra == {
        "video_id": "aspmNhKAFMc", "channel_id": CHANNEL, "views": 1137335,
    }
    # No description, no view count: the row still knows which video it is.
    assert old.text == "" and old.extra == {
        "video_id": "2Xiljy4xzbc", "channel_id": CHANNEL,
    }


async def test_since_hours_bounds_the_feed():
    async with _client(_handler()) as client:
        items = await yt.fetch_youtube(client, "F", HANDLE, since_hours=24 * 365)
    assert [i.extra["video_id"] for i in items] == ["aspmNhKAFMc"]


# --- the gather -----------------------------------------------------------

async def test_the_gather_never_fetches_the_watch_page(tmp_path):
    handle = _handler()
    store = SqliteStore(tmp_path / "s.db")
    try:
        async with _client(handle) as client:
            await cmd_gather(client, store, [
                {"name": "Fireship", "kind": "youtube", "url": HANDLE,
                 "medium": "watch", "section": "Video"},
            ])
        rows = {r.extra["video_id"]: r for r in await store.all_rows()}
        assert rows["aspmNhKAFMc"].text.startswith("OpenAI claims")
        assert rows["aspmNhKAFMc"].published == "2026-09-11"
        # The wide still was probed for, found, and kept over the feed's 4:3.
        assert rows["aspmNhKAFMc"].image == \
            "https://i.ytimg.com/vi/aspmNhKAFMc/maxresdefault.jpg"
        assert rows["aspmNhKAFMc"].kind == "youtube"
        # A gathered video is readable, and waits for its summary like an
        # article: the description was the body.
        counts = await store.counts()
        assert counts["unreadable"] == 0 and counts["pending_summary"] == 2
    finally:
        await store.close()
    assert not any("watch?v=" in u for u in handle.seen), handle.seen


async def test_without_a_wide_still_the_feed_thumbnail_stands(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    try:
        async with _client(_handler(wide=False)) as client:
            await cmd_gather(client, store, [
                {"name": "Fireship", "kind": "youtube", "url": HANDLE},
            ])
        (row,) = [r for r in await store.all_rows()
                  if r.extra["video_id"] == "aspmNhKAFMc"]
        assert row.image == "https://i.ytimg.com/vi/aspmNhKAFMc/hqdefault.jpg"
    finally:
        await store.close()


async def test_a_gathered_video_reaches_the_edition_as_a_watch_item(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    src = [{"name": "Fireship", "kind": "youtube", "url": HANDLE,
            "medium": "watch", "section": "Video"}]
    try:
        async with _client(_handler()) as client:
            await cmd_gather(client, store, src)
        for r in await store.all_rows():
            await store.set_summary(r.id, "A summary.")
        articles = await collect_current_edition(store, src)
    finally:
        await store.close()
    built = ed.build(articles, key="k" * 24, date="2026-09-12", built_at="")
    lead = built.lead
    assert lead.medium == "watch" and lead.kind == "youtube"
    assert lead.extra["video_id"] == "aspmNhKAFMc"
    # No reading time on a video — its body is a description, not the piece.
    assert lead.meta == lead.date and "min" not in lead.meta


# --- the page -------------------------------------------------------------

def _item(**over) -> ed.Item:
    base = dict(id="a" * 16, title="A video", url=yt.video_url("aspmNhKAFMc"),
                source="Fireship", section="Video", kind="youtube",
                medium="watch", body="What it is about.",
                image="https://i.ytimg.com/vi/aspmNhKAFMc/maxresdefault.jpg",
                extra={"video_id": "aspmNhKAFMc", "views": 1137335})
    return ed.Item(**{**base, **over})


def test_the_byline_is_one_chip_to_the_video_with_its_views():
    assert sources.links_for(_item()) == [
        Link("Fireship", yt.video_url("aspmNhKAFMc"), "article",
             "1,137,335 views on YouTube"),
    ]


def test_the_player_is_the_type_s_to_offer():
    assert sources.embed_for(_item()) == \
        "https://www.youtube-nocookie.com/embed/aspmNhKAFMc"
    assert sources.embed_for(_item(kind="rss", extra={})) == ""
    assert sources.embed_for(_item(extra={})) == ""


def test_the_article_page_holds_the_player_not_a_photograph():
    html = site.render_article(_item(), "k" * 24, "/")
    assert 'class="player"' in html
    assert 'data-embed="https://www.youtube-nocookie.com/embed/aspmNhKAFMc"' in html
    # The still is the poster, the link is the video, and the frame only
    # arrives on click: no iframe in the markup as served.
    assert 'href="https://www.youtube.com/watch?v=aspmNhKAFMc"' in html
    assert "maxresdefault.jpg" in html
    assert "<iframe" not in html
    assert '.player[data-embed]' in html
    assert "<p>What it is about.</p>" in html


def test_an_article_page_is_unchanged():
    html = site.render_article(
        _item(kind="rss", medium="read", extra={}), "k" * 24, "/")
    assert 'class="player"' not in html and 'class="photo"' in html
    assert ".player[data-embed]" not in html


def test_the_drawer_is_told_to_say_watch():
    art = {"id": "a" * 16, "title": "A video", "url": yt.video_url("x"),
           "source": "Fireship", "section": "Video", "medium": "watch",
           "kind": "youtube", "extra": {"video_id": "x"}, "summary": "s",
           "text": "d", "iso_date": "2026-09-11", "date": "Sep 11"}
    built = ed.build([art], key="k" * 24, date="2026-09-12", built_at="")
    html = site.render_edition(built)
    assert 'data-cta="Watch it here"' in html
    assert 'data-cta="Read the full text"' not in html
