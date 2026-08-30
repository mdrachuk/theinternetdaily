"""Tests for the edition archive and the pages it feeds.

No network, no LLM: editions are faked by dropping snapshots into a cache
directory, which is exactly what a real ingest leaves behind.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import tid.archive as archive
import tid.web as web


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("TID_CACHE", str(d))
    return d


def _article(n: int, source: str = "Quanta",
             fetched_at: str = "2026-08-12T06:00:00+00:00") -> dict:
    return {
        "id": f"{n:016x}",
        "source": source,
        "fetched_at": fetched_at,
        "section": "Science",
        "medium": "read",
        "url": f"https://quantamagazine.org/story-{n}",
        "title": f"Headline {n}",
        "summary": "Two sentences of lede. That is all it takes.",
        "text": "A paragraph of rewritten body. " * 30,
        "iso_date": "2026-08-12",
        "date": "Aug 12, 2026",
    }


def _edition(cache: Path, key: str, date: str, *, articles=3,
             items: bool = True, built_at: str | None = None) -> None:
    """Write a snapshot the way `archive.record` would."""
    payload = {
        "key": key,
        "date": date,
        "built_at": built_at or f"{date}T06:00:00+00:00",
        "articles": articles,
        "sources": {"Hacker News": 2, "Quanta": 1},
        "items": [_article(i) for i in range(articles)] if items else [],
    }
    (cache / f"{key}.json").write_text(json.dumps(payload))


@pytest.fixture
async def client():
    app = web.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


# --- the archive itself ---------------------------------------------------

def test_editions_are_newest_first(cache):
    _edition(cache, "a" * 24, "2026-08-10")
    _edition(cache, "b" * 24, "2026-08-12")
    _edition(cache, "c" * 24, "2026-08-11")
    assert [e.date for e in archive.editions(cache)] == [
        "2026-08-12", "2026-08-11", "2026-08-10",
    ]


def test_pdf_era_snapshot_lists_but_is_not_readable(cache):
    """Editions built before snapshots carried their articles must not vanish
    from the archive — but nothing can render them as a page."""
    _edition(cache, "d" * 24, "2026-08-10", items=False)
    (e,) = archive.editions(cache)
    assert e.articles == 3 and e.has_items is False
    assert archive.readable(cache) == []
    assert archive.load(cache, "d" * 24) is None


def test_non_key_files_ignored(cache):
    _edition(cache, "a" * 24, "2026-08-10")
    (cache / "2026-08-08.json").write_text("{}")   # a hand-made file, not an edition
    (cache / "notes.json").write_text("{}")
    assert [e.key for e in archive.editions(cache)] == ["a" * 24]


def test_unreadable_snapshot_is_skipped_not_fatal(cache):
    _edition(cache, "a" * 24, "2026-08-10")
    (cache / f"{'e' * 24}.json").write_text("{not json")
    assert [e.key for e in archive.editions(cache)] == ["a" * 24]


def test_missing_cache_dir_is_empty_not_an_error(tmp_path):
    assert archive.editions(tmp_path / "nope") == []


def test_record_writes_a_readable_snapshot(cache):
    articles = [_article(0, "Hacker News"), _article(1, "Hacker News"),
                _article(2, "Quanta")]
    built = archive.record(cache, "e" * 24, "2026-08-12", articles)
    assert built.total == 3

    (e,) = archive.editions(cache)
    assert e.articles == 3
    assert e.sources == {"Hacker News": 2, "Quanta": 1}
    assert e.date == "2026-08-12"
    assert e.has_items

    loaded = archive.load(cache, "e" * 24)
    assert loaded is not None
    assert [i.title for i in loaded.items] == [i.title for i in built.items]
    # The full body survives the round trip; it is what the article page shows.
    assert loaded.lead.body.startswith("A paragraph of rewritten body.")


# --- the watermark: where the next edition starts -------------------------


def test_record_stamps_the_newest_gather_time_it_carried(cache):
    """The watermark is the high tide of *this* edition, so the next one can
    pick up exactly where it left off."""
    articles = [
        _article(0, fetched_at="2026-08-12T06:00:00+00:00"),
        _article(1, fetched_at="2026-08-12T06:00:09+00:00"),
    ]
    archive.record(cache, "a" * 24, "2026-08-12", articles, content="tok-1",
                   since="2026-08-11T06:00:00+00:00")
    (e,) = archive.editions(cache)
    assert e.watermark == "2026-08-12T06:00:09+00:00"
    assert e.since == "2026-08-11T06:00:00+00:00"
    assert e.content == "tok-1"


def test_boundary_falls_back_to_a_window_with_no_previous_edition(cache):
    """A fresh install, or an archive that has been cleared. The store keeps
    every row it ever fetched, so "no predecessor" must not mean "everything":
    it means the last NO_PREVIOUS_WINDOW."""
    from datetime import datetime, timedelta, timezone

    got = archive.boundary(cache, "tok-1")
    assert got is not None
    expected = datetime.now(timezone.utc) - archive.NO_PREVIOUS_WINDOW
    # Parsed rather than string-compared: the assertion is about the instant,
    # and a second can tick over between the call and this line.
    assert abs(
        (datetime.fromisoformat(got) - expected).total_seconds()
    ) < 5
    assert archive.NO_PREVIOUS_WINDOW == timedelta(hours=30)


async def test_a_cleared_archive_publishes_the_window_not_the_whole_store(
    tmp_path, monkeypatch
):
    """The case that made this rule necessary: an operator deletes the latest
    edition and re-ingests. The next paper must be the recent news, not every
    article the store has accumulated since it was first set up."""
    from tid import jobs
    from tid.store import ArticleRow, SqliteStore, url_hash

    monkeypatch.setenv("TID_CACHE", str(tmp_path / "cache"))
    store = SqliteStore(tmp_path / "state.db")
    now = datetime.now(timezone.utc)

    def _row(title, hours_ago):
        url = f"http://old.invalid/{title}"
        return ArticleRow(
            id=url_hash(url), url=url, title=title, source="Src",
            text="body", summary="lede", body="rewritten",
            fetched_at=(now - timedelta(hours=hours_ago))
                       .isoformat(timespec="seconds"),
        )

    try:
        await store.upsert_rows([
            _row("ancient", 24 * 20),   # three weeks of backlog
            _row("old", 40),            # outside the window
            _row("recent", 6),          # inside it
        ])
        sources = [{"name": "Src", "kind": "rss", "url": "http://x.invalid"}]
        key = await jobs.current_key(store, sources)
        built = await jobs.build_edition_for_key(key, store, sources)
    finally:
        await store.close()

    assert [i.title for i in built.items] == ["recent"]


def test_boundary_follows_the_last_edition_from_other_content(cache):
    archive.record(cache, "a" * 24, "2026-08-12",
                   [_article(0, fetched_at="2026-08-12T06:00:00+00:00")],
                   content="tok-1")
    assert archive.boundary(cache, "tok-2") == "2026-08-12T06:00:00+00:00"


def test_boundary_reuses_the_window_of_an_edition_from_the_same_content(cache):
    """Re-filing a source in sources.toml moves the edition key without
    gathering a thing. The new key must re-lay-out the same articles, so it
    has to run the same window again — not start a fresh one after them, which
    would hand the reader a blank paper until the next gather."""
    archive.record(cache, "a" * 24, "2026-08-12",
                   [_article(0, fetched_at="2026-08-12T06:00:00+00:00")],
                   content="tok-1", since="2026-08-11T06:00:00+00:00")
    assert archive.boundary(cache, "tok-1") == "2026-08-11T06:00:00+00:00"


def test_boundary_falls_back_to_built_at_on_a_snapshot_from_an_older_build(cache):
    """The upgrade path, exactly as it is on disk: a snapshot with neither a
    content token nor a watermark, because the build that wrote it had
    neither. It still pins down a moment, and using it is what stops the first
    edition after an upgrade from re-publishing the whole store in one go."""
    _edition(cache, "a" * 24, "2026-08-12", built_at="2026-08-12T06:00:00+00:00")
    written = json.loads((cache / ("a" * 24 + ".json")).read_text())
    assert "content" not in written and "watermark" not in written
    assert archive.boundary(cache, "tok-1") == "2026-08-12T06:00:00+00:00"


async def test_refiling_a_source_re_lays_out_the_same_paper(
    tmp_path, monkeypatch
):
    """The full round trip for case 1: publish an edition, then edit
    sources.toml without gathering anything. The key moves, so a new edition is
    assembled — and it has to carry the same articles under the new layout,
    not an empty window."""
    from tid import jobs
    from tid.store import ArticleRow, SqliteStore, url_hash

    monkeypatch.setenv("TID_CACHE", str(tmp_path / "cache"))
    store = SqliteStore(tmp_path / "state.db")
    now = datetime.now(timezone.utc)
    url = "http://x.invalid/a"
    sources = [{"name": "Src", "kind": "rss", "url": "http://x.invalid",
                "section": "News"}]
    try:
        await store.upsert_rows([ArticleRow(
            id=url_hash(url), url=url, title="Only Story", source="Src",
            text="body", summary="lede", body="rewritten",
            fetched_at=(now - timedelta(hours=2)).isoformat(timespec="seconds"),
        )])
        first = await jobs.build_edition_for_key(
            await jobs.current_key(store, sources), store, sources
        )
        assert [i.title for i in first.items] == ["Only Story"]
        assert first.lead.section == "News"

        # Re-filed into another column. Nothing gathered in between.
        refiled = [dict(sources[0], section="Science")]
        second_key = await jobs.current_key(store, refiled)
        assert second_key != first.key
        second = await jobs.build_edition_for_key(second_key, store, refiled)
    finally:
        await store.close()

    assert [i.title for i in second.items] == ["Only Story"]
    # Same story, re-filed: the layout followed the config, the window did not
    # move out from under it.
    assert second.lead.section == "Science"


@pytest.mark.parametrize("key", [
    "../../../etc/passwd", "..", "a" * 24 + "/x", "NOTHEX", "", "a/b",
])
def test_load_rejects_anything_that_is_not_a_key(cache, key):
    assert archive.load(cache, key) is None


# --- the routes -----------------------------------------------------------

async def test_index_renders_the_current_edition(client, cache, monkeypatch):
    _edition(cache, "b" * 24, "2026-08-12", articles=5)
    r = await client.get(f"/e/{'b' * 24}")
    assert r.status_code == 200
    assert "The Internet Daily" in r.text
    assert "Headline 0" in r.text
    assert "Wednesday 12 August 2026" in r.text


async def test_edition_links_its_neighbours(client, cache):
    _edition(cache, "a" * 24, "2026-08-10")
    _edition(cache, "b" * 24, "2026-08-12")
    r = await client.get(f"/e/{'b' * 24}")
    assert f"/e/{'a' * 24}" in r.text, "the older edition must be one click back"
    assert 'id="nav-next"' not in r.text, "there is nothing newer than the newest"


async def test_article_page_carries_the_full_text(client, cache):
    _edition(cache, "c" * 24, "2026-08-12")
    key, article_id = "c" * 24, f"{0:016x}"
    r = await client.get(f"/e/{key}/a/{article_id}")
    assert r.status_code == 200
    assert "Headline 0" in r.text
    assert "A paragraph of rewritten body." in r.text
    assert f"/e/{key}" in r.text, "there must be a way back to the edition"


async def test_unknown_edition_and_article_are_404(client, cache):
    _edition(cache, "c" * 24, "2026-08-12")
    assert (await client.get(f"/e/{'9' * 24}")).status_code == 404
    assert (await client.get(f"/e/{'c' * 24}/a/nope")).status_code == 404


async def test_medium_filter_narrows_the_edition(client, cache):
    """A medium nothing was filed under empties the paper rather than 404s."""
    _edition(cache, "c" * 24, "2026-08-12")
    r = await client.get(f"/e/{'c' * 24}", params={"m": "watch"})
    assert r.status_code == 200
    assert "Nothing filed yet" in r.text
    assert "Headline 0" not in r.text


async def test_nonsense_medium_is_ignored_not_rejected(client, cache):
    _edition(cache, "c" * 24, "2026-08-12")
    r = await client.get(f"/e/{'c' * 24}", params={"m": "smell"})
    assert r.status_code == 200
    assert "Headline 0" in r.text


async def test_archive_json_lists_editions(client, cache):
    _edition(cache, "a" * 24, "2026-08-10")
    r = await client.get("/archive.json")
    assert r.status_code == 200
    body = r.json()
    assert body["editions"][0]["key"] == "a" * 24
    assert body["editions"][0]["url"] == f"/e/{'a' * 24}"
    assert body["editions"][0]["has_items"] is True


async def test_pdf_routes_redirect_to_the_paper(client, cache):
    """PDFs are not built any more; a bookmark should still land somewhere."""
    for path in ("/digest.pdf", "/preview.png", f"/digest/{'a' * 24}.pdf"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code == 301, path
        assert r.headers["location"] == "/"


async def test_icon_route_rejects_a_path_that_is_not_a_domain(client, cache):
    r = await client.get("/icon/..%2F..%2Fetc%2Fpasswd.png")
    assert r.status_code == 404


async def test_icon_route_serves_a_cached_mark(client, cache):
    import tid.icons as icons

    path = icons.icon_path(cache, "lwn.net")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(icons.BLANK_PNG)
    r = await client.get("/icon/lwn.net.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
