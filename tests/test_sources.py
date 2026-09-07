"""Source types: what a kind records, how it is fetched, and how it is shown.

The witness is Hacker News. A story there is a link to somewhere else plus a
discussion of it, and the old single "Hacker News" label pointed at the link
and lost the discussion. The type now keeps the item id on the row and the
byline prints two chips — [Hacker News] → [github.com] — the first opening
the comments. A feed article is unchanged: one chip, its source.
"""
from __future__ import annotations

import httpx
import pytest

from tid import edition as ed, site, sources
from tid.cli import cmd_gather, collect_current_edition
from tid.sources import Link, RawItem
from tid.sources.hn import fetch_hn, item_url
from tid.store import SqliteStore, url_hash

HITS = {"hits": [
    {"objectID": "41", "title": "A tool &amp; a story", "url": "https://github.com/x/y",
     "points": 312, "num_comments": 120, "created_at_i": 1_780_000_000},
    {"objectID": "42", "title": "Ask HN: why?", "url": None,
     "points": 90, "num_comments": 7, "created_at_i": 1_780_000_100},
    {"objectID": "", "title": "no id, dropped", "url": "https://x.test/"},
]}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _algolia(request: httpx.Request) -> httpx.Response:
    if "hn.algolia.com" in str(request.url):
        return httpx.Response(200, json=HITS)
    return httpx.Response(404)


# --- the registry ---------------------------------------------------------

def test_every_shipped_kind_is_registered():
    assert {"rss", "hn", "wikipedia_events"} <= set(sources.TYPES)
    for kind, t in sources.TYPES.items():
        assert t.kind == kind and t.label


def test_a_source_with_no_kind_is_a_feed():
    assert sources.kind_of({"name": "x"}) == "rss"
    assert sources.get(None) is sources.TYPES["rss"]
    assert sources.get("nope") is None


async def test_an_unknown_kind_is_a_lookup_error_not_a_crash():
    async with _client(_algolia) as client:
        with pytest.raises(LookupError):
            await sources.fetch(client, {"name": "x", "kind": "carrier-pigeon"})


# --- hn: the record -------------------------------------------------------

async def test_hn_fetch_records_the_type_and_its_fields():
    async with _client(_algolia) as client:
        items = await fetch_hn(client, since_hours=48, min_points=50)
    assert [i.title for i in items] == ["A tool & a story", "Ask HN: why?"]
    linked, ask = items
    assert linked.kind == "hn"
    assert linked.url == "https://github.com/x/y"
    assert linked.extra == {"hn_id": "41", "points": 312, "comments": 120}
    assert linked.surfaced == "2026-05-28"
    # A self post has no link of its own: the discussion is the article.
    assert ask.url == item_url("42")
    assert ask.extra["hn_id"] == "42"


async def test_the_registry_fetch_reads_the_config_table():
    async with _client(_algolia) as client:
        items = await sources.fetch(
            client, {"name": "HN", "kind": "hn", "min_points": 100}
        )
    assert all(i.source == "HN" and i.kind == "hn" for i in items)


# --- the links ------------------------------------------------------------

def _item(**over) -> ed.Item:
    base = dict(id="a" * 16, title="t", url="https://github.com/x/y",
                source="Hacker News", section="HN", kind="hn",
                extra={"hn_id": "41", "comments": 120})
    return ed.Item(**{**base, **over})


def test_an_hn_story_offers_the_discussion_then_the_link():
    links = sources.links_for(_item())
    assert links == [
        Link("Hacker News", item_url("41"), "discussion",
             "120 comments on Hacker News"),
        Link("github.com", "https://github.com/x/y", "article",
             "The story, at github.com"),
    ]


def test_a_self_post_offers_only_the_discussion():
    (link,) = sources.links_for(_item(url=item_url("42"), extra={"hn_id": "42"}))
    assert link.label == "Hacker News" and link.href == item_url("42")
    assert link.role == "discussion"


def test_an_hn_row_from_before_the_id_was_recorded_falls_back_to_the_link():
    (link,) = sources.links_for(_item(extra={}))
    assert link == Link("Hacker News", "https://github.com/x/y", "article")


def test_a_feed_article_has_one_chip_its_source():
    item = _item(kind="rss", source="Quanta", url="https://q.test/p", extra={})
    assert sources.links_for(item) == [Link("Quanta", "https://q.test/p")]


def test_a_kind_this_build_never_heard_of_still_gets_a_link():
    """A snapshot from a build with a type this one lacks must still render."""
    item = _item(kind="from-the-future", source="X", url="https://x.test/1")
    assert sources.links_for(item) == [Link("X", "https://x.test/1")]


# --- the page -------------------------------------------------------------

def _article(n: int, **over) -> dict:
    a = {"id": f"{n:016x}", "title": f"Headline {n}", "url": f"https://site{n}.test/p",
         "source": f"Source {n}", "section": "S", "summary": "lede",
         "text": "word " * 300, "iso_date": "2026-09-01", "date": "Sep 1"}
    return {**a, **over}


def _hn(n: int = 0, **over) -> dict:
    return _article(n, source="Hacker News", url="https://github.com/x/y",
                    kind="hn", extra={"hn_id": "41", "comments": 120}, **over)


def _build(*arts) -> ed.Edition:
    return ed.build(list(arts), key="k" * 24, date="2026-09-05",
                    built_at="2026-09-05T06:00:00+00:00")


def test_the_byline_prints_two_chips_with_an_arrow_between():
    html = site.render_edition(_build(_hn(), _article(1)))
    lead = html.split('<div class="byline top">')[1].split("</div>")[0]
    assert lead.count('class="src') == 2
    assert lead.index('href="' + item_url("41")) < lead.index('href="https://github.com/x/y"')
    assert 'class="arrow"' in lead
    assert "<span>Hacker News</span>" in lead and "<span>github.com</span>" in lead
    # Each chip carries the mark of where it leads, served from this origin.
    assert "/icon/news.ycombinator.com.png" in lead
    assert "/icon/github.com.png" in lead
    assert 'title="120 comments on Hacker News"' in lead


def test_a_feed_byline_is_one_chip_and_no_arrow():
    html = site.render_edition(_build(_article(1), _article(2)))
    # Every byline printed under a headline; the drawer's empty shell is not one.
    bylines = [b for b in html.split('<div class="byline')[1:]
               if 'id="pv-byline"' not in b.split(">")[0]]
    assert bylines
    for b in bylines:
        chunk = b.split("</div>")[0]
        assert chunk.count('class="src') == 1
        assert 'class="arrow"' not in chunk


def test_the_byline_is_not_nested_inside_the_headline_link():
    """Its chips are anchors, and HTML forbids an anchor inside an anchor: the
    browser would close the outer one early and the title would lose its
    link."""
    html = site.render_edition(_build(_hn(), _article(1)))
    for card in html.split('<article class="hl')[1:]:
        title_link = card.split('<a class="hla pv-open"')[1].split("</a>")[0]
        assert "<a " not in title_link


def test_the_lead_actions_offer_both_places():
    html = site.render_edition(_build(_hn(), _article(1)))
    actions = html.split('<div class="lead-actions">')[1].split("</div>")[0]
    assert "Hacker News ↗" in actions and "github.com ↗" in actions
    assert item_url("41") in actions


def test_the_article_page_offers_both_places_in_nav_and_foot():
    e = _build(_hn(), _article(1))
    html = site.render_article(e.lead, e.key, "/")
    nav = html.split('<nav')[1].split("</nav>")[0]
    foot = html.split('class="foot"')[1].split("</div>")[0]
    for chunk in (nav, foot):
        assert "Hacker News ↗" in chunk and "github.com ↗" in chunk
        assert item_url("41") in chunk and "https://github.com/x/y" in chunk


def test_the_drawer_builds_its_buttons_from_the_chips():
    """The preview reads the byline's chips out of the card rather than a
    second copy of the links, so the two can never disagree."""
    html = site.render_edition(_build(_hn(), _article(1)))
    assert 'id="pv-links"' in html
    assert 'querySelectorAll(".byline a.src")' in html
    assert 'id="pv-source"' not in html


# --- through the store and the snapshot -----------------------------------

def test_the_snapshot_carries_the_type_and_its_fields():
    before = _build(_hn(), _article(1))
    after = ed.from_snapshot(ed.to_snapshot(before))
    lead = after.find("0" * 16)
    assert lead.kind == "hn" and lead.extra == {"hn_id": "41", "comments": 120}
    assert sources.links_for(lead) == sources.links_for(before.lead)


def test_a_snapshot_from_before_the_fields_existed_still_loads():
    data = ed.to_snapshot(_build(_article(1)))
    for i in data["items"]:
        del i["kind"], i["extra"]
    (item,) = ed.from_snapshot(data).items
    assert item.kind == "rss" and item.extra == {}


async def test_the_store_keeps_the_type_and_its_fields(store):
    await store.insert_raw(
        "HN", "https://github.com/x/y", "t", text="body",
        kind="hn", extra={"hn_id": "41", "points": 312},
    )
    await store.set_summary(url_hash("https://github.com/x/y"), "s")
    await store.set_body(url_hash("https://github.com/x/y"), "b")
    (row,) = await store.unpublished("HN")
    assert row.kind == "hn"
    assert row.extra == {"hn_id": "41", "points": 312}


async def test_a_regather_backfills_the_type_onto_an_older_row(store):
    """Rows written before the fields existed pick them up the next time the
    feed still carries the story — which is how an existing install gets its
    discussion chips without re-extracting anything."""
    url = "https://github.com/x/y"
    await store.insert_raw("HN", url, "t", text="body")
    await store.set_summary(url_hash(url), "s")
    await store.set_body(url_hash(url), "b")
    (row,) = await store.unpublished("HN")
    assert row.kind == "" and row.extra == {}

    await store.insert_raw("HN", url, "t", text=None,
                           kind="hn", extra={"hn_id": "41"})
    (row,) = await store.unpublished("HN")
    assert row.kind == "hn" and row.extra == {"hn_id": "41"}
    assert row.text == "body", "the back-fill must not touch the article"

    # ...and a later gather does not replace what is there.
    await store.insert_raw("HN", url, "t", text=None,
                           kind="rss", extra={"hn_id": "99"})
    (row,) = await store.unpublished("HN")
    assert row.kind == "hn" and row.extra == {"hn_id": "41"}


async def test_the_bulk_copy_round_trips_the_fields(store, tmp_path):
    await store.insert_raw("HN", "https://a.test/1", "t", text="body",
                           kind="hn", extra={"hn_id": "1"})
    target = SqliteStore(tmp_path / "target.db")
    try:
        await target.upsert_rows(await store.all_rows())
        (row,) = await target.all_rows()
        assert row.kind == "hn" and row.extra == {"hn_id": "1"}
    finally:
        await target.close()


def _page() -> bytes:
    body = "\n".join(
        f"<p>Paragraph {i} of the article, with enough words in it to clear "
        f"trafilatura's minimum length threshold for extraction.</p>"
        for i in range(6)
    )
    return f"<!doctype html><html><head><title>A</title></head><body><article>{body}</article></body></html>".encode()


async def test_gather_files_hn_stories_with_their_type_and_the_edition_shows_it(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "hn.algolia.com" in url:
            return httpx.Response(200, json=HITS)
        if url.startswith("https://github.com/") or "news.ycombinator.com" in url:
            return httpx.Response(200, content=_page(),
                                  headers={"content-type": "text/html"})
        return httpx.Response(404)

    config = [{"name": "Hacker News", "kind": "hn", "min_points": 50}]
    store = SqliteStore(tmp_path / "state.db")
    try:
        async with _client(handler) as client:
            assert await cmd_gather(client, store, config) == 0
        for row in await store.pending_summary():
            await store.set_summary(row.id, "s")
            await store.set_body(row.id, "b")
        articles = await collect_current_edition(store, config)
        by_title = {a["title"]: a for a in articles}
        linked = by_title["A tool & a story"]
        assert linked["kind"] == "hn"
        assert linked["extra"] == {"hn_id": "41", "points": 312, "comments": 120}

        e = ed.build(articles, key="k" * 24, date="2026-09-05", built_at="")
        html = site.render_edition(e)
        assert item_url("41") in html and "<span>github.com</span>" in html
    finally:
        await store.close()


async def test_a_row_the_store_never_typed_takes_the_kind_from_the_config(tmp_path):
    """Until a re-gather fills it in, the config still knows what the source
    is — so the byline is right from the first render after an upgrade."""
    store = SqliteStore(tmp_path / "state.db")
    try:
        await store.insert_raw("Hacker News", "https://github.com/x/y", "t", text="body")
        await store.set_summary(url_hash("https://github.com/x/y"), "s")
        await store.set_body(url_hash("https://github.com/x/y"), "b")
        (a,) = await collect_current_edition(
            store, [{"name": "Hacker News", "kind": "hn"}]
        )
        assert a["kind"] == "hn" and a["extra"] == {}
    finally:
        await store.close()


def test_raw_item_defaults_to_a_feed():
    assert RawItem(source="s", url="u", title="t").kind == "rss"


def test_the_sources_sit_above_the_headline_and_the_title_opens_the_preview():
    """On the web the byline is printed before the title on every card, the
    title link is the way into the preview drawer, and there is no separate
    Preview button to keep in step with it."""
    html = site.render_edition(_build(_hn(), _article(1)))
    assert "Preview</button>" not in html
    for card in html.split('<article class="hl')[1:]:
        card = card.split("</article>")[0]
        assert card.index('class="byline') < card.index('class="hla pv-open"')


def test_the_preview_shows_the_summary_and_not_the_opening_lines():
    """The drawer's text is the summary alone; the article's own first
    sentences stay on its page behind "Read the full text"."""
    html = site.render_edition(_build(_hn(), _article(1)))
    assert 'id="pv-dek"' in html
    assert "excerpt" not in html
