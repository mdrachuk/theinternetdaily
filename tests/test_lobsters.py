"""`kind = "lobsters"`: the JSON listing walked page by page, the score gate,
and an HN-shaped byline — discussion first, then the site."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from tid import sources
from tid.sources import Link
from tid.sources.lobsters import fetch_lobsters, story_url


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _story(short_id, title, url, score, hours, comments=3, tags=("ai",)):
    return {
        "short_id": short_id, "title": title, "url": url, "score": score,
        "comment_count": comments, "created_at": _ago(hours),
        "tags": list(tags),
        "comments_url": f"https://lobste.rs/s/{short_id}/slug",
    }


PAGES = {
    1: [
        _story("aaaaaa", "A tool &amp; a story", "https://github.com/x/y", 31, 2),
        _story("bbbbbb", "Too quiet", "https://quiet.test/", 4, 3),
        _story("cccccc", "What are you working on?", "", 22, 5, tags=("ask",)),
    ],
    2: [
        _story("dddddd", "Yesterday's hit", "https://blog.test/p", 40, 30),
        _story("eeeeee", "Out of the window", "https://old.test/", 99, 60),
    ],
    3: [
        _story("ffffff", "Never fetched", "https://never.test/", 500, 90),
    ],
}


def _client(pages=PAGES):
    seen: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.host == "lobste.rs" and path.endswith(".json")
        page = int(path.rsplit("/", 1)[-1].removesuffix(".json"))
        seen.append(page)
        return httpx.Response(200, json=pages.get(page, []))

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


async def test_stories_clearing_the_window_and_the_score_are_taken_by_score():
    client, seen = _client()
    async with client:
        items = await fetch_lobsters(client, since_hours=48, min_points=10)
    assert [i.title for i in items] == [
        "Yesterday's hit", "A tool & a story", "What are you working on?",
    ]
    linked = items[1]
    assert linked.kind == "lobsters" and linked.url == "https://github.com/x/y"
    assert linked.extra == {"short_id": "aaaaaa", "points": 31, "comments": 3,
                            "tags": ["ai"]}
    assert linked.surfaced == datetime.now(timezone.utc).date().isoformat()


async def test_a_text_post_is_its_own_discussion():
    client, _ = _client()
    async with client:
        items = await fetch_lobsters(client)
    ask = next(i for i in items if i.extra["short_id"] == "cccccc")
    assert ask.url == story_url("cccccc")


async def test_paging_stops_once_a_page_reaches_past_the_window():
    client, seen = _client()
    async with client:
        await fetch_lobsters(client, since_hours=48)
    assert seen == [1, 2]


async def test_an_empty_page_ends_the_walk():
    client, seen = _client({1: PAGES[1]})
    async with client:
        items = await fetch_lobsters(client, since_hours=24 * 30)
    assert seen == [1, 2] and len(items) == 2


async def test_the_registry_reads_the_config_table():
    client, _ = _client()
    async with client:
        items = await sources.fetch(
            client, {"name": "Lobsters", "kind": "lobsters", "min_points": 30},
        )
    assert [i.title for i in items] == ["Yesterday's hit", "A tool & a story"]
    assert all(i.source == "Lobsters" for i in items)


def _item(url, extra, source="Lobsters"):
    return sources.RawItem(source=source, url=url, title="t", kind="lobsters",
                           extra=extra)


def test_a_story_offers_the_discussion_then_the_link():
    links = sources.links_for(_item(
        "https://github.com/x/y", {"short_id": "aaaaaa", "comments": 1}))
    assert links == [
        Link("Lobsters", story_url("aaaaaa"), "discussion",
             "1 comment on Lobsters"),
        Link("github.com", "https://github.com/x/y", "article",
             "The story, at github.com"),
    ]


def test_a_text_post_offers_only_the_discussion():
    (link,) = sources.links_for(_item(story_url("cccccc"), {"short_id": "cccccc"}))
    assert link.href == story_url("cccccc") and link.role == "discussion"
    assert link.title == "The discussion on Lobsters"


def test_a_row_with_no_id_falls_back_to_the_link():
    (link,) = sources.links_for(_item("https://github.com/x/y", {}))
    assert link == Link("Lobsters", "https://github.com/x/y", "article")
