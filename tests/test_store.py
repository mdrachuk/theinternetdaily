"""Store protocol conformance — run against every shipped backend.

The `store` fixture (tests/conftest.py) is parametrized, so each of these
tests runs once per backend. Anything asserted here is part of the contract a
downstream store implementation has to satisfy.
"""
from __future__ import annotations

import pytest

from tid.store import ArticleRow, SqliteStore, open_store, url_hash


async def _add(store, title, *, text="body text", published=None,
               surfaced=None, source="Src", summary=None, body=None,
               image=None, fetched_at=None):
    url = f"http://example.invalid/{title}"
    if fetched_at is not None:
        # insert_raw stamps fetched_at off the clock. A test about the edition
        # window has to place its rows in time itself, and the bulk-copy
        # path is the one part of the protocol that lets it.
        await store.upsert_rows([ArticleRow(
            id=url_hash(url), url=url, title=title, source=source, text=text,
            surfaced=surfaced, published=published, image=image,
            fetched_at=fetched_at,
        )])
    else:
        await store.insert_raw(
            source, url, title, text=text, surfaced=surfaced,
            published=published, image=image,
        )
    if summary is not None:
        await store.set_summary(url_hash(url), summary)
    if body is not None:
        await store.set_body(url_hash(url), body)
    return url_hash(url)


# --- gather ---------------------------------------------------------------

async def test_insert_then_exists_by_url(store):
    await _add(store, "one")
    assert await store.exists("http://example.invalid/one", "anything else")


async def test_exists_matches_a_normalized_title_from_another_url(store):
    """Cross-source duplicate detection: the same story syndicated under a
    different URL must not be ingested twice."""
    await _add(store, "Hello World")
    assert await store.exists("http://other.invalid/x", "hello, world!")


async def test_unknown_article_does_not_exist(store):
    assert not await store.exists("http://example.invalid/nope", "nope")


async def test_insert_is_idempotent(store):
    await _add(store, "dup")
    await _add(store, "dup")
    assert (await store.counts())["total"] == 1


async def test_reinsert_backfills_a_missing_date_but_keeps_the_text(store):
    await _add(store, "back", published=None, surfaced=None)
    # A re-gather passes text=None; it must not wipe the extracted body.
    await store.insert_raw(
        "Src", "http://example.invalid/back", "back",
        text=None, surfaced="2030-01-01",
    )
    rows = await store.pending_summary()
    assert len(rows) == 1
    assert rows[0].text == "body text"
    assert rows[0].surfaced == "2030-01-01"


async def test_image_is_stored_and_backfilled_on_a_regather(store):
    """A feed that only starts advertising an image later must still get it
    onto the row it already wrote — that is how an existing install picks
    images up without re-extracting everything."""
    await _add(store, "pic", summary="s", body="b")
    (row,) = await store.unpublished("Src")
    assert row.image is None

    await _add(store, "pic", image="https://cdn.invalid/a.jpg")
    (row,) = await store.unpublished("Src")
    assert row.image == "https://cdn.invalid/a.jpg"

    # ...and a later re-gather must not overwrite it with a different one.
    await _add(store, "pic", image="https://cdn.invalid/b.jpg")
    (row,) = await store.unpublished("Src")
    assert row.image == "https://cdn.invalid/a.jpg"


# --- summarize / rewrite --------------------------------------------------

async def test_pending_summary_excludes_unreadable_and_done(store):
    await _add(store, "ready")
    await _add(store, "unreadable", text=None)
    await _add(store, "already", summary="s", body="b")
    titles = {r.title for r in await store.pending_summary()}
    assert titles == {"ready"}


async def test_pending_rewrite_tracks_body_not_summary(store):
    await _add(store, "summarized", summary="s")
    await _add(store, "rewritten", summary="s", body="b")
    titles = {r.title for r in await store.pending_rewrite()}
    assert titles == {"summarized"}


async def test_set_summary_and_body_stamp_their_timestamps(store):
    aid = await _add(store, "stamped", summary="a summary", body="a body")
    rows = await store.unpublished("Src")
    row = next(r for r in rows if r.id == aid)
    assert row.summary == "a summary"
    assert row.body == "a body"
    assert row.summarized_at and row.rewritten_at


# --- render ---------------------------------------------------------------

async def test_unpublished_is_newest_first_and_uncapped(store):
    """No per-source cap: four stories in, four stories out."""
    for i in range(1, 5):
        await _add(store, f"a{i}", published=f"2030-01-0{i}", summary="s", body="b")
    rows = await store.unpublished("Src")
    assert [r.title for r in rows] == ["a4", "a3", "a2", "a1"]


async def test_unpublished_needs_text_and_summary(store):
    await _add(store, "no-summary", published="2030-01-01")
    await _add(store, "no-text", text=None, published="2030-01-01")
    assert await store.unpublished("Src") == []


async def test_unpublished_scopes_to_one_source(store):
    await _add(store, "mine", summary="s", body="b", source="Src")
    await _add(store, "theirs", summary="s", body="b", source="Other")
    rows = await store.unpublished("Src")
    assert [r.title for r in rows] == ["mine"]


async def test_unpublished_waits_for_the_rewrite(store):
    """Summarized is not finished. The paper's premise is the rewritten body,
    and publishing marks an article done — so going out with raw scraped text
    would be that article's only appearance, not a rough first draft."""
    await _add(store, "summarized-only", summary="s")
    await _add(store, "finished", summary="s", body="b")
    assert [r.title for r in await store.unpublished("Src")] == ["finished"]


async def test_unpublished_drops_what_an_edition_already_carried(store):
    aid = await _add(store, "published", summary="s", body="b")
    await _add(store, "still-new", summary="s", body="b")
    await store.mark_rendered([aid], "2026-01-01")
    assert [r.title for r in await store.unpublished("Src")] == ["still-new"]


async def test_a_straggler_from_a_published_gather_still_runs(store):
    """The bug this replaced a timestamp cutoff to fix. One gather stamps
    every row with the same `fetched_at`, but they finish summarizing at very
    different times. The one that was still pending when the edition went out
    must run in the next one — a "newer than the last edition" cutoff would
    drop it forever, because it shares its second with articles that made it.
    """
    same_second = "2026-01-01T06:00:00+00:00"
    carried = await _add(store, "carried", summary="s", body="b",
                          fetched_at=same_second)
    await _add(store, "still-rewriting", fetched_at=same_second)  # no summary
    await store.mark_rendered([carried], "2026-01-01")
    assert await store.unpublished("Src") == []

    # It finishes, and the next edition picks it up.
    straggler = url_hash("http://example.invalid/still-rewriting")
    await store.set_summary(straggler, "s")
    await store.set_body(straggler, "b")
    assert [r.title for r in await store.unpublished("Src")] == ["still-rewriting"]


async def test_floor_bounds_an_unpublished_query(store):
    await _add(store, "ancient", summary="s", body="b",
               fetched_at="2020-01-01T00:00:00+00:00")
    await _add(store, "recent", summary="s", body="b",
               fetched_at="2030-01-01T00:00:00+00:00")
    titles = [
        r.title for r in await store.unpublished("Src", "2025-01-01T00:00:00+00:00")
    ]
    assert titles == ["recent"]


async def test_retire_before_publishes_the_backlog_without_carrying_it(store):
    """Retiring is what stops a floored first edition from deferring the
    backlog to the second one rather than excluding it."""
    await _add(store, "ancient", summary="s", body="b",
               fetched_at="2020-01-01T00:00:00+00:00")
    await _add(store, "recent", summary="s", body="b",
               fetched_at="2030-01-01T00:00:00+00:00")
    await _add(store, "not-ready", fetched_at="2020-01-01T00:00:00+00:00")

    n = await store.retire_before("2025-01-01T00:00:00+00:00", "2026-01-01")
    assert n == 1
    # The old one is out of the running; the recent one is untouched...
    assert [r.title for r in await store.unpublished("Src")] == ["recent"]
    # ...and the unready one was left alone, so it still runs once it is
    # finished, however far outside the window it was gathered.
    unready = url_hash("http://example.invalid/not-ready")
    await store.set_summary(unready, "s")
    await store.set_body(unready, "b")
    assert sorted(r.title for r in await store.unpublished("Src")) == [
        "not-ready", "recent",
    ]


async def test_sort_date_falls_back_through_published_surfaced_fetched(store):
    row = ArticleRow(id="x", url="u", title="t", source="s",
                     fetched_at="2020-01-01T00:00:00+00:00")
    assert row.sort_date == "2020-01-01T00:00:00+00:00"
    row.surfaced = "2021-01-01"
    assert row.sort_date == "2021-01-01"
    row.published = "2022-01-01"
    assert row.sort_date == "2022-01-01"


async def test_mark_rendered_moves_rows_out_of_pending_render(store):
    aid = await _add(store, "rendered-soon", summary="s", body="b")
    assert [r.id for r in await store.pending_render()] == [aid]
    await store.mark_rendered([aid], "2026-01-01")
    assert await store.pending_render() == []
    assert (await store.counts())["rendered"] == 1


# --- meta -----------------------------------------------------------------

async def test_counts_reports_every_stage(store):
    await _add(store, "unreadable", text=None)
    await _add(store, "raw")
    await _add(store, "summarized", summary="s")
    await _add(store, "full", summary="s", body="b")
    c = await store.counts()
    assert c["total"] == 4
    assert c["unreadable"] == 1
    assert c["pending_summary"] == 1          # "raw"
    assert c["pending_rewrite"] == 2          # "raw", "summarized"
    # Only "full": an article still awaiting its rewrite is not publishable.
    assert c["pending_render"] == 1
    assert c["rendered"] == 0


async def test_max_fetched_at_is_empty_for_an_empty_store(store):
    assert await store.max_fetched_at() == ""


async def test_max_fetched_at_moves_when_content_arrives(store):
    before = await store.max_fetched_at()
    await _add(store, "fresh")
    assert await store.max_fetched_at() > before


# --- migration ------------------------------------------------------------

async def test_all_rows_round_trips_through_upsert(store, tmp_path):
    """`tid migrate` is exactly this: all_rows() out, upsert_rows() in."""
    await _add(store, "keep", published="2030-01-01", summary="s", body="b")
    await _add(store, "bare", text=None)

    target = SqliteStore(tmp_path / "target.db")
    try:
        rows = await store.all_rows()
        assert await target.upsert_rows(rows) == 2
        copied = {r.title: r for r in await target.all_rows()}
        assert set(copied) == {"keep", "bare"}
        assert copied["keep"].summary == "s"
        assert copied["keep"].body == "b"
        assert copied["keep"].published == "2030-01-01"
        assert copied["bare"].text is None
        # Re-running must be a no-op, not a duplicate.
        await target.upsert_rows(rows)
        assert (await target.counts())["total"] == 2
    finally:
        await target.close()


async def test_upsert_never_nulls_a_field_the_target_already_has(store, tmp_path):
    """Guards the direction of a migration: copying a pre-LLM row over a
    summarized one must not lose the summary."""
    target = SqliteStore(tmp_path / "target.db")
    try:
        aid = await _add(target, "article", summary="target summary")
        await _add(store, "article")  # same url -> same id, no summary
        await target.upsert_rows(await store.all_rows())
        rows = {r.id: r for r in await target.all_rows()}
        assert rows[aid].summary == "target summary"
    finally:
        await target.close()


# --- the URL factory ------------------------------------------------------

def test_open_store_defaults_to_sqlite(tmp_path):
    s = open_store(str(tmp_path / "x.db"))
    assert isinstance(s, SqliteStore)
    assert s.path == tmp_path / "x.db"


def test_open_store_accepts_a_sqlite_url(tmp_path):
    s = open_store(f"sqlite://{tmp_path}/y.db")
    assert isinstance(s, SqliteStore)
    assert s.path == tmp_path / "y.db"


def test_open_store_rejects_an_unknown_scheme():
    """An unknown scheme must not fall through to 'a SQLite file named
    redis://…' — that would create a garbage file and look like it worked."""
    with pytest.raises(ValueError):
        open_store("redis://localhost:6379")


def test_open_store_reads_mongo_database_and_collection_from_the_url():
    pytest.importorskip("pymongo", reason="needs tid[mongo]")
    from tid.store.mongo import MongoStore

    s = open_store("mongodb://localhost:27017/mydb?collection=mycol")
    assert isinstance(s, MongoStore)
    assert s.col.database.name == "mydb"
    assert s.col.name == "mycol"
