"""Tests for the per-source `since_hours` age filter.

`since_hours` is a **gather-time** bound and nothing else: it says how far
back into a feed one fetch reaches. It used to be re-applied at render time
as well, back when an edition was "the latest N per source" and stale rows
would otherwise sit in it forever. The edition now starts at the previous
edition's `fetched_at` watermark, so staleness is handled by construction —
and re-filtering by publication date would be actively wrong, since a blog
post written last year that a feed surfaced to us this morning is news to the
reader. Section 2 pins that down.

Also covers the two things that surround it: the edition cache key has to
move when since_hours changes, and fetch_hn's Algolia numericFilters have to
be encoded so that *both* filters survive.

No network, no LLM SDK, no xelatex — HTTP is served by httpx.MockTransport
and feedparser is stubbed.
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

import httpx
import pytest

from tid.cache import edition_key
from tid.store import url_hash


def _struct_hours_ago(hours: float) -> time.struct_time:
    """UTC struct_time N hours in the past, as feedparser would produce."""
    return time.gmtime(time.time() - hours * 3600)


class _Entry:
    """Minimal stand-in for a feedparser entry."""

    def __init__(self, link, title, published_parsed=None):
        self.link = link
        self.title = title
        if published_parsed is not None:
            self.published_parsed = published_parsed


class _Feed:
    def __init__(self, entries):
        self.entries = entries


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- 1: gather-time filtering ---------------------------------------------


async def _fetch(entries, **kwargs):
    from tid.fetch import fetch_rss

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss/>")

    with mock.patch("tid.fetch.feedparser.parse",
                    return_value=_Feed(entries)):
        async with _client(handler) as client:
            return await fetch_rss(
                client, "Test", "http://example.invalid/feed", **kwargs
            )


async def test_no_since_hours_keeps_everything():
    entries = [
        _Entry("http://a.invalid/1", "Fresh", _struct_hours_ago(1)),
        _Entry("http://a.invalid/2", "Ancient", _struct_hours_ago(5000)),
    ]
    assert len(await _fetch(entries)) == 2


async def test_drops_entries_older_than_window():
    entries = [
        _Entry("http://a.invalid/1", "Fresh", _struct_hours_ago(1)),
        _Entry("http://a.invalid/2", "Stale", _struct_hours_ago(100)),
    ]
    got = await _fetch(entries, since_hours=24)
    assert [i.title for i in got] == ["Fresh"]


async def test_entries_without_a_date_are_kept():
    """Documented behaviour: no date == can't judge == keep."""
    entries = [
        _Entry("http://a.invalid/1", "Undated"),
        _Entry("http://a.invalid/2", "Stale", _struct_hours_ago(100)),
    ]
    got = await _fetch(entries, since_hours=24)
    assert [i.title for i in got] == ["Undated"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs POSIX tzset")
async def test_cutoff_is_utc_not_local_time():
    """feedparser's *_parsed structs are UTC, so the comparison must use
    calendar.timegm, not time.mktime (which reads a struct as *local* time
    and skews the cutoff by the host's offset).

    The host timezone is forced here rather than inherited: under TZ=UTC —
    which is what CI usually runs — timegm and mktime agree and the assertion
    would pass either way. At UTC+14 a 2h-old entry looks 16h old to mktime,
    so a 6h window wrongly drops it.
    """
    prev_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-14"  # UTC+14
    time.tzset()
    try:
        entries = [_Entry("http://a.invalid/1", "Edge", _struct_hours_ago(2))]
        # Kept: 2h old, 6h window. mktime would compute 16h and drop it.
        assert len(await _fetch(entries, since_hours=6)) == 1
        # Dropped: 2h old, 1h window.
        assert len(await _fetch(entries, since_hours=1)) == 0
    finally:
        if prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev_tz
        time.tzset()


# --- 2: no render-time age filter ------------------------------------------
#
# The `store` fixture is parametrized across backends (tests/conftest.py), so
# the rule is pinned for SQLite and Mongo alike. Broader store contract tests
# live in tests/test_store.py.


async def _add(store, title, published=None, surfaced=None, ready=True):
    url = f"http://example.invalid/{title}"
    await store.insert_raw(
        "Src", url, title, text="body text",
        surfaced=surfaced, published=published,
    )
    if ready:
        await store.set_summary(url_hash(url), "a summary")


async def _titles(store, **kwargs):
    return [r.title for r in await store.ready_since("Src", **kwargs)]


async def test_publication_date_does_not_bound_the_edition(store):
    """Both rows were gathered by this run, so both are in this edition —
    however old the articles themselves are."""
    await _add(store, "old", published="2020-01-01")
    await _add(store, "new", published="2030-01-01")
    assert sorted(await _titles(store)) == ["new", "old"]


async def test_a_backdated_article_gathered_now_still_makes_the_edition(store):
    """The case a date window got wrong: an essay published years ago that a
    feed has only just filed. It is new to the reader, so it runs."""
    await _add(store, "rediscovered", published="2011-06-01")
    assert await _titles(store) == ["rediscovered"]


async def test_undated_articles_are_kept_at_render_time(store):
    await _add(store, "undated")
    assert await _titles(store) == ["undated"]


async def test_unsummarized_rows_are_still_excluded(store):
    await _add(store, "not-ready", published="2030-01-01", ready=False)
    assert await _titles(store) == []


async def test_everything_gathered_this_run_is_carried(store):
    """No cap: five in, five out, newest first."""
    for i in range(5):
        await _add(store, f"a{i}", published=f"2030-01-0{i + 1}")
    assert await _titles(store) == ["a4", "a3", "a2", "a1", "a0"]


# --- 3: the cache key has to notice since_hours ---------------------------


def test_since_hours_changes_the_edition_key():
    base = [{"name": "Quanta", "kind": "rss"}]
    windowed = [{"name": "Quanta", "kind": "rss", "since_hours": 168}]
    assert edition_key("t", base) != edition_key("t", windowed)


def test_unset_since_hours_hashes_the_same_as_absent():
    """An optional field left out must not hash differently from one set to
    None — otherwise two configs that mean the same thing split the cache."""
    cfg = [{"name": "Quanta", "kind": "rss"}]
    assert edition_key("t", cfg) == edition_key(
        "t", [dict(cfg[0], since_hours=None)]
    )


def test_a_stale_limit_key_no_longer_binds():
    """`limit` used to be hashed into the key, because it decided how many
    articles a source contributed. It decides nothing now, so it is out of the
    hash — and a config carrying a leftover `limit` must key the same as one
    without it, rather than quietly minting a second edition."""
    cfg = [{"name": "Quanta", "kind": "rss"}]
    assert edition_key("t", cfg) == edition_key(
        "t", [dict(cfg[0], limit=8)]
    )


def test_differing_since_hours_differ():
    a = [{"name": "Q", "kind": "rss", "since_hours": 24}]
    b = [{"name": "Q", "kind": "rss", "since_hours": 168}]
    assert edition_key("t", a) != edition_key("t", b)


def test_key_is_stable_for_identical_config():
    cfg = [{"name": "Q", "kind": "rss", "since_hours": 24}]
    assert edition_key("t", cfg) == edition_key("t", list(cfg))


# --- 4: Algolia numericFilters encoding -----------------------------------


async def test_numeric_filters_are_json_encoded_so_both_survive():
    """A bare Python list makes httpx emit repeated `numericFilters=` params
    and Algolia honours only the first, silently dropping the min_points
    gate. It has to be a JSON-encoded array."""
    from tid.fetch import fetch_hn

    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"hits": []})

    async with _client(handler) as client:
        await fetch_hn(client, since_hours=48, min_points=50)

    values = captured["url"].params.get_list("numericFilters")
    assert len(values) == 1, "must be one JSON string, not repeated params"
    decoded = json.loads(values[0])
    assert len(decoded) == 2
    assert any(f.startswith("created_at_i>") for f in decoded)
    assert "points>50" in decoded
