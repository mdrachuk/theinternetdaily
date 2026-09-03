"""Gathering a feed whose headlines are not Latin.

The witness: a Meduza run where 178 stories sat in the store with no text.
Every all-Cyrillic title normalized to "" (or to a stray Latin word), so from
the second such story on `exists` said "duplicate", and the gather loop then
ran its back-fill insert — which filed a textless row under the new URL. The
story never reached extraction and never showed up as unreadable.
"""
from __future__ import annotations

import httpx

from tid.cli import cmd_gather
from tid.store import SqliteStore, url_hash

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Медуза</title>
  <item>
    <title>Суд арестовал журналиста</title>
    <link>http://meduza.invalid/one</link>
    <pubDate>Mon, 03 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Правительство отменило льготы</title>
    <link>http://meduza.invalid/two</link>
    <pubDate>Mon, 03 Aug 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>HBO покажет сериал о войне</title>
    <link>http://meduza.invalid/three</link>
    <pubDate>Mon, 03 Aug 2026 11:00:00 GMT</pubDate>
  </item>
  <item>
    <title>«Суд арестовал журналиста»</title>
    <link>http://meduza.invalid/one-syndicated</link>
    <pubDate>Mon, 03 Aug 2026 12:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


def _page() -> str:
    body = "\n".join(
        f"<p>Абзац {i} статьи, в котором достаточно слов, чтобы пройти "
        f"минимальный порог длины текста для извлечения trafilatura.</p>"
        for i in range(6)
    )
    return f"<!doctype html><html><head><title>x</title></head><body><article>{body}</article></body></html>"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/feed"):
        return httpx.Response(
            200, content=FEED.encode(),
            headers={"content-type": "application/rss+xml"},
        )
    if url.startswith("http://meduza.invalid/"):
        return httpx.Response(
            200, content=_page().encode(),
            headers={"content-type": "text/html; charset=utf-8"},
        )
    return httpx.Response(404)


SOURCES = [{
    "name": "Медуза",
    "kind": "rss",
    "url": "http://feeds.invalid/feed",
    "limit": 10,
}]


async def test_every_distinct_cyrillic_story_is_extracted(tmp_path, capsys):
    store = SqliteStore(tmp_path / "state.db")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            assert await cmd_gather(client, store, SOURCES) == 0
        counts = await store.counts()
        # Three distinct stories, all readable. The fourth item is the first
        # story again under another URL: skipped, not filed as a ghost.
        assert counts["total"] == 3
        assert counts["unreadable"] == 0
        assert counts["pending_summary"] == 3
        rows = {r.id: r for r in await store.all_rows()}
        assert url_hash("http://meduza.invalid/one-syndicated") not in rows
        assert all(r.text for r in rows.values())
    finally:
        await store.close()
    err = capsys.readouterr().err
    assert "1 duplicate" in err
    assert "already have this story" in err


async def test_regather_backfills_but_adds_nothing(tmp_path):
    store = SqliteStore(tmp_path / "state.db")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            await cmd_gather(client, store, SOURCES)
            await cmd_gather(client, store, SOURCES)
        counts = await store.counts()
        assert counts["total"] == 3
        assert counts["unreadable"] == 0
    finally:
        await store.close()
