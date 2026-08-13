"""Tests for the edition archive and the index page it feeds.

No network, no LLM, no xelatex: editions are faked by dropping files into a
cache directory, which is exactly what a real build leaves behind.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

import papernews.archive as archive
import papernews.web as web


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("PAPERNEWS_CACHE", str(d))
    return d


def _edition(cache: Path, key: str, date: str, *, articles=3, preview=False,
             mtime: float | None = None) -> None:
    (cache / f"{key}.pdf").write_bytes(b"%PDF-1.4 fake")
    if preview:
        (cache / f"{key}.png").write_bytes(b"\x89PNG fake")
    if articles is not None:
        (cache / f"{key}.json").write_text(json.dumps({
            "key": key, "date": date, "built_at": f"{date}T06:00:00+00:00",
            "articles": articles, "sources": {"Hacker News": 2, "Quanta": 1},
        }))
    if mtime is not None:
        os.utime(cache / f"{key}.pdf", (mtime, mtime))


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


def test_pdf_without_sidecar_still_listed(cache):
    """Editions built before sidecars existed must not vanish from the index."""
    _edition(cache, "d" * 24, "ignored", articles=None, mtime=1_754_000_000)
    (e,) = archive.editions(cache)
    assert e.articles is None
    assert e.date == "2025-07-31"
    assert e.size == len(b"%PDF-1.4 fake")


def test_non_key_files_ignored(cache):
    _edition(cache, "a" * 24, "2026-08-10")
    (cache / "2026-08-08.pdf").write_bytes(b"%PDF")   # a --out render, not an edition
    (cache / "notes.pdf").write_bytes(b"%PDF")
    assert [e.key for e in archive.editions(cache)] == ["a" * 24]


def test_missing_cache_dir_is_empty_not_an_error(tmp_path):
    assert archive.editions(tmp_path / "nope") == []


def test_record_writes_a_readable_sidecar(cache):
    articles = [{"source": "Hacker News"}, {"source": "Hacker News"},
                {"source": "Quanta"}]
    archive.record(cache, "e" * 24, "2026-08-12", articles)
    (cache / f"{'e' * 24}.pdf").write_bytes(b"%PDF")
    (e,) = archive.editions(cache)
    assert e.articles == 3
    assert e.sources == {"Hacker News": 2, "Quanta": 1}
    assert e.date == "2026-08-12"


@pytest.mark.parametrize("key", [
    "../../../etc/passwd", "..", "a" * 24 + "/x", "NOTHEX", "", "a/b",
])
def test_find_rejects_anything_that_is_not_a_key(cache, key):
    assert archive.find(cache, key) is None


def test_find_returns_existing_pdf(cache):
    _edition(cache, "f" * 24, "2026-08-12")
    assert archive.find(cache, "f" * 24) == cache / f"{'f' * 24}.pdf"
    assert archive.find(cache, "0" * 24) is None


# --- the routes -----------------------------------------------------------

async def test_index_lists_every_edition(client, cache):
    _edition(cache, "a" * 24, "2026-08-10", preview=True)
    _edition(cache, "b" * 24, "2026-08-12", preview=True)
    r = await client.get("/")
    assert r.status_code == 200
    assert "Previous editions" in r.text
    assert f"/digest/{'a' * 24}.pdf" in r.text
    assert f"/digest/{'b' * 24}.pdf" in r.text
    # newest is the hero, with its cover
    assert f"/digest/{'b' * 24}.png" in r.text


async def test_index_without_any_editions_offers_a_build(client, cache):
    r = await client.get("/")
    assert r.status_code == 200
    assert "No editions have been built yet" in r.text
    assert "/digest.pdf" in r.text


async def test_archived_pdf_is_served_with_a_dated_filename(client, cache):
    _edition(cache, "c" * 24, "2026-08-12")
    r = await client.get(f"/digest/{'c' * 24}.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "papernews-2026-08-12-cccccc.pdf" in r.headers["content-disposition"]


async def test_unknown_edition_is_404(client, cache):
    r = await client.get(f"/digest/{'9' * 24}.pdf")
    assert r.status_code == 404


async def test_archive_json_lists_editions(client, cache):
    _edition(cache, "a" * 24, "2026-08-10")
    r = await client.get("/archive.json")
    assert r.status_code == 200
    body = r.json()
    assert body["editions"][0]["key"] == "a" * 24
    assert body["editions"][0]["url"] == f"/digest/{'a' * 24}.pdf"
