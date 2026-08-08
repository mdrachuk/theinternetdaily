"""Tests for the per-source `since_hours` age filter (PR #7).

The filter is applied at two layers, and both are covered here:

    1. gather time  — fetch_rss() drops feed entries older than the window
    2. render time  — Store.latest_per_source(since_date=...) drops stored
                      articles older than the window (rows are never deleted,
                      so without this the edition would accumulate stale ones)

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
from pathlib import Path
from unittest import mock

import httpx
import pytest

from papernews.cache import edition_key
from papernews.store import Store


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
    from papernews.fetch import fetch_rss

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss/>")

    with mock.patch("papernews.fetch.feedparser.parse",
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


# --- 2: render-time filtering ---------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def _add(store: Store, title, published=None, surfaced=None, ready=True):
    url = f"http://example.invalid/{title}"
    store.insert_raw(
        "Src", url, title, text="body text",
        surfaced=surfaced, published=published,
    )
    if ready:
        from papernews.store import url_hash
        store.set_summary(url_hash(url), "a summary")


def _titles(store: Store, **kwargs):
    return [r["title"] for r in store.latest_per_source("Src", 10, **kwargs)]


def test_without_since_date_returns_all_ready_rows(store):
    _add(store, "old", published="2020-01-01")
    _add(store, "new", published="2030-01-01")
    assert sorted(_titles(store)) == ["new", "old"]


def test_since_date_excludes_older_articles(store):
    _add(store, "old", published="2020-01-01")
    _add(store, "new", published="2030-01-01")
    assert _titles(store, since_date="2025-01-01") == ["new"]


def test_since_date_is_inclusive_of_the_boundary(store):
    _add(store, "boundary", published="2025-01-01")
    assert _titles(store, since_date="2025-01-01") == ["boundary"]


def test_falls_back_to_surfaced_when_published_is_null(store):
    _add(store, "surfaced-only", surfaced="2030-01-01")
    _add(store, "stale-surfaced", surfaced="2020-01-01")
    assert _titles(store, since_date="2025-01-01") == ["surfaced-only"]


def test_undated_articles_are_kept(store):
    """Mirrors the gather-time rule so the two layers agree."""
    _add(store, "undated")
    assert _titles(store, since_date="2025-01-01") == ["undated"]


def test_unsummarized_rows_are_still_excluded(store):
    _add(store, "not-ready", published="2030-01-01", ready=False)
    assert _titles(store, since_date="2025-01-01") == []


def test_limit_still_applies_with_since_date(store):
    for i in range(5):
        _add(store, f"a{i}", published=f"2030-01-0{i + 1}")
    rows = store.latest_per_source("Src", 2, since_date="2025-01-01")
    assert len(rows) == 2
    # newest first
    assert [r["title"] for r in rows] == ["a4", "a3"]


# --- 3: the cache key has to notice since_hours ---------------------------


def test_since_hours_changes_the_edition_key():
    base = [{"name": "Quanta", "kind": "rss", "limit": 8}]
    windowed = [{"name": "Quanta", "kind": "rss", "limit": 8,
                 "since_hours": 168}]
    assert edition_key("t", base) != edition_key("t", windowed)


def test_unset_since_hours_does_not_change_existing_keys():
    """Adding the field must not invalidate every cached edition on upgrade —
    a cache miss costs a rebuild plus an LLM call for the cover
    decorations."""
    cfg = [{"name": "Quanta", "kind": "rss", "limit": 8}]
    # The key an existing install's cached PDF was built under.
    assert edition_key("t", cfg) == "5a80ef1d3afffd2850d2f905"
    # Explicit None must hash the same as absent.
    assert edition_key("t", cfg) == edition_key(
        "t", [dict(cfg[0], since_hours=None)]
    )


def test_differing_since_hours_differ():
    a = [{"name": "Q", "kind": "rss", "limit": 8, "since_hours": 24}]
    b = [{"name": "Q", "kind": "rss", "limit": 8, "since_hours": 168}]
    assert edition_key("t", a) != edition_key("t", b)


def test_key_is_stable_for_identical_config():
    cfg = [{"name": "Q", "kind": "rss", "limit": 8, "since_hours": 24}]
    assert edition_key("t", cfg) == edition_key("t", list(cfg))


# --- 4: Algolia numericFilters encoding -----------------------------------


async def test_numeric_filters_are_json_encoded_so_both_survive():
    """A bare Python list makes httpx emit repeated `numericFilters=` params
    and Algolia honours only the first, silently dropping the min_points
    gate. It has to be a JSON-encoded array."""
    from papernews.fetch import fetch_hn

    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"hits": []})

    async with _client(handler) as client:
        await fetch_hn(client, limit=10, since_hours=48, min_points=50)

    values = captured["url"].params.get_list("numericFilters")
    assert len(values) == 1, "must be one JSON string, not repeated params"
    decoded = json.loads(values[0])
    assert len(decoded) == 2
    assert any(f.startswith("created_at_i>") for f in decoded)
    assert "points>50" in decoded
