"""End-to-end pipeline test with no network, no GPU and no API key.

gather → extract → summarize → rewrite → topics → edition → LaTeX, with HTTP served by
httpx.MockTransport and the LLM replaced by tid.testing.FakeBackend. This
is the test that would have caught every signature break during the async and
store migrations, and it is the one CI can always run.

The xelatex step is exercised too, but only where xelatex exists — the
LaTeX-source assertions below are what actually pin the rendering behaviour.
"""
from __future__ import annotations

import shutil

import httpx
import pytest

from tid.cli import (
    cmd_gather,
    cmd_ingest,
    cmd_rewrite,
    cmd_summarize,
    collect_current_edition,
)
from tid.render import build_pdf, render_tex
from tid.store import SqliteStore
from tid.testing import FakeBackend

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>A Story About &amp; Symbols</title>
    <link>http://articles.invalid/one</link>
    <pubDate>Mon, 03 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Second Story</title>
    <link>http://articles.invalid/two</link>
    <pubDate>Tue, 04 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Unreadable Story</title>
    <link>http://articles.invalid/thin</link>
    <pubDate>Tue, 04 Aug 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

# trafilatura needs a real-looking page and at least 200 characters of body.
def _page(paragraphs: int = 6, date: str = "2026-08-03") -> str:
    body = "\n".join(
        f"<p>Paragraph {i} of the article, with enough words in it to clear "
        f"trafilatura's minimum length threshold for extraction.</p>"
        for i in range(paragraphs)
    )
    return f"""<!doctype html>
<html><head><title>Article</title>
<meta property="article:published_time" content="{date}T09:00:00Z">
</head><body><article><h1>Article</h1>{body}</article></body></html>"""


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/feed"):
        return httpx.Response(
            200, content=FEED.encode(),
            headers={"content-type": "application/rss+xml"},
        )
    if url.endswith("/thin"):
        # Extraction fails: too little text. Must be recorded, not crash.
        return httpx.Response(
            200, content=b"<html><body><p>too short</p></body></html>",
            headers={"content-type": "text/html"},
        )
    if url.startswith("http://articles.invalid/"):
        # Distinct publication dates so the edition's newest-first ordering is
        # actually observable.
        date = "2026-08-04" if url.endswith("/two") else "2026-08-03"
        return httpx.Response(
            200, content=_page(date=date).encode(),
            headers={"content-type": "text/html; charset=utf-8"},
        )
    return httpx.Response(404)


SOURCES = [{
    "name": "Test Feed",
    "kind": "rss",
    "url": "http://feeds.invalid/feed",
    "limit": 10,
}]


@pytest.fixture
async def pipeline(tmp_path):
    store = SqliteStore(tmp_path / "state.db")
    backend = FakeBackend()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)
    ) as client:
        try:
            yield client, store, backend
        finally:
            await store.close()


async def test_full_pipeline_produces_a_render_ready_edition(pipeline, tmp_path):
    client, store, backend = pipeline

    assert await cmd_gather(client, store, SOURCES) == 0
    counts = await store.counts()
    assert counts["total"] == 3
    assert counts["unreadable"] == 1          # the thin page
    assert counts["pending_summary"] == 2

    assert await cmd_summarize(store, backend) == 0
    assert await cmd_rewrite(store, backend) == 0
    counts = await store.counts()
    assert counts["pending_summary"] == 0
    assert counts["pending_rewrite"] == 0

    articles = await collect_current_edition(store, SOURCES)
    # Newest first, and the unreadable page is absent: it never got a summary.
    assert [a["title"] for a in articles] == [
        "Second Story", "A Story About & Symbols",
    ]
    assert all(a["summary"] for a in articles)
    assert all("Rewritten paragraph" in a["text"] for a in articles)
    # The feed's HTML entity was decoded at fetch time, not left as &amp;.
    assert any("&" in a["title"] for a in articles)


async def test_edition_renders_to_valid_looking_latex(pipeline):
    client, store, backend = pipeline
    await cmd_gather(client, store, SOURCES)
    await cmd_summarize(store, backend)
    await cmd_rewrite(store, backend)
    articles = await collect_current_edition(store, SOURCES)

    tex = render_tex("2026-08-08", articles, decorations={})
    assert tex.startswith("\\documentclass")
    assert "\\begin{document}" in tex and "\\end{document}" in tex
    # The ampersand in a title has to reach LaTeX escaped or the build fails.
    assert "A Story About \\& Symbols" in tex
    assert "http://articles.invalid/two" in tex


async def test_rerunning_gather_adds_nothing_and_keeps_the_llm_output(pipeline):
    """Ingest is scheduled every few hours; a re-run must be a no-op, not a
    re-summarize (which would cost money) or a wipe."""
    client, store, backend = pipeline
    await cmd_gather(client, store, SOURCES)
    await cmd_summarize(store, backend)
    await cmd_rewrite(store, backend)
    calls_after_first = len(backend.calls)

    await cmd_gather(client, store, SOURCES)
    assert (await store.counts())["total"] == 3
    await cmd_summarize(store, backend)
    await cmd_rewrite(store, backend)
    assert len(backend.calls) == calls_after_first


async def test_batches_are_sized_by_the_backend(pipeline):
    """Each stage must chunk to the backend's own limit — FakeBackend allows 4
    summaries and 2 rewrites per call, so 2 articles is one call each."""
    client, store, backend = pipeline
    await cmd_gather(client, store, SOURCES)
    await cmd_summarize(store, backend)
    assert len(backend.calls) == 1
    await cmd_rewrite(store, backend)
    assert len(backend.calls) == 2


async def test_a_refusing_model_leaves_the_articles_pending(pipeline):
    """A model that answers with prose instead of the protocol must not store
    junk: the article stays pending and the next run tries again."""
    client, store, _ = pipeline
    await cmd_gather(client, store, SOURCES)
    refuser = FakeBackend(respond=lambda system, user: "I cannot help with that.")
    assert await cmd_summarize(store, refuser) == 0
    assert (await store.counts())["pending_summary"] == 2


async def test_json_backend_takes_the_same_path(pipeline):
    """The JSON protocol (what vLLM's guided decoding produces) has to land the
    same edition as the marker protocol."""
    client, store, _ = pipeline
    await cmd_gather(client, store, SOURCES)
    json_backend = FakeBackend(supports_json=True)
    await cmd_summarize(store, json_backend)
    await cmd_rewrite(store, json_backend)
    articles = await collect_current_edition(store, SOURCES)
    assert len(articles) == 2
    assert all(a["summary"] and "Rewritten paragraph" in a["text"]
               for a in articles)
    assert json_backend.calls[0]["json_schema"] is not None


async def test_ingest_ends_with_the_edition_filed_into_topics(pipeline):
    """The pipeline is gather → summarize → rewrite → topics, and the last one
    is what decides the paper's sections: by the time ingest returns, every
    article that will be in the next edition carries a topic and knows whether
    it is one of that topic's main stories."""
    from tid import edition as ed
    from tid.testing import FAKE_TOPICS

    client, store, backend = pipeline
    assert await cmd_ingest(client, store, backend, SOURCES) == 0

    articles = await collect_current_edition(store, SOURCES)
    assert articles and all(a["topic"] in FAKE_TOPICS for a in articles)
    assert any(a["main_rank"] == 0 for a in articles)

    paper = ed.build(articles, key="k" * 24, date="2026-08-08", built_at="")
    # The columns are the topics the model named, not the source's own name.
    named = {s.name for s in paper.sections} | {s.name for s in paper.below}
    assert named | {paper.lead.group} <= set(FAKE_TOPICS)
    assert "Test Feed" not in named


@pytest.mark.skipif(shutil.which("xelatex") is None, reason="needs xelatex")
async def test_pdf_actually_builds(pipeline, tmp_path):
    client, store, backend = pipeline
    await cmd_gather(client, store, SOURCES)
    await cmd_summarize(store, backend)
    await cmd_rewrite(store, backend)
    articles = await collect_current_edition(store, SOURCES)

    pdf = await build_pdf("2026-08-08", articles, tmp_path / "out")
    assert pdf.exists()
    assert pdf.read_bytes().startswith(b"%PDF")


async def test_batches_never_mix_sources(pipeline):
    """A batch is one LLM call and the model treats it as one piece of work:
    mixing a Russian article in with English ones made it summarize all of them
    in English, overriding the per-article language rule."""
    from tid.cli import _chunks_by_source
    from tid.store import ArticleRow

    rows = [
        ArticleRow(id=f"{src}{i}", url=f"u{src}{i}", title="t", source=src)
        for src in ("Meduza", "The Guardian")
        for i in range(3)
    ]
    # Interleave, so a naive chunker would straddle the boundary.
    rows = [rows[0], rows[3], rows[1], rows[4], rows[2], rows[5]]
    for batch in _chunks_by_source(rows, 4):
        assert len({r.source for r in batch}) == 1, batch
    assert sum(len(b) for b in _chunks_by_source(rows, 4)) == len(rows)


# --- no cap, and every article gets a page --------------------------------

BIG_FEED_SIZE = 25


def _big_feed() -> str:
    items = "\n".join(
        f"  <item>"
        f"<title>Story {i:02d}</title>"
        f"<link>http://bulk.invalid/{i}</link>"
        f"<pubDate>Mon, 03 Aug 2026 {i % 24:02d}:00:00 GMT</pubDate>"
        f"</item>"
        for i in range(BIG_FEED_SIZE)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<rss version=\"2.0\"><channel><title>Bulk</title>\n{items}\n"
        "</channel></rss>\n"
    )


def _bulk_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/feed"):
        return httpx.Response(
            200, content=_big_feed().encode(),
            headers={"content-type": "application/rss+xml"},
        )
    return httpx.Response(
        200, content=_page().encode(),
        headers={"content-type": "text/html; charset=utf-8"},
    )


BULK_SOURCES = [{
    "name": "Bulk Feed",
    "kind": "rss",
    "section": "Bulk",
    "url": "http://bulk.invalid/feed",
}]


async def test_a_feed_is_never_capped_and_every_story_gets_a_page(tmp_path):
    """The whole point of dropping `limit`: a feed that files 25 stories
    between syncs puts 25 stories in the paper — the front page skims the top
    and carries the rest below the fold, and each one is reachable as its own
    page."""
    from tid import edition as ed

    store = SqliteStore(tmp_path / "state.db")
    backend = FakeBackend()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_bulk_handler)
    ) as client:
        try:
            await cmd_gather(client, store, BULK_SOURCES)
            await cmd_summarize(store, backend)
            await cmd_rewrite(store, backend)
            articles = await collect_current_edition(store, BULK_SOURCES)
        finally:
            await store.close()

    assert len(articles) == BIG_FEED_SIZE

    paper = ed.build(articles, key="k" * 24, date="2026-08-12", built_at="")
    # Nothing is dropped by the layout: the lead, the stories beside it, the
    # front-page columns and everything below the fold add up to the whole feed.
    assert paper.total == BIG_FEED_SIZE
    assert {i.title for i in paper.items} == {
        f"Story {i:02d}" for i in range(BIG_FEED_SIZE)
    }
    # And each one has a page of its own to link to.
    assert all(paper.find(i.id) is not None for i in paper.items)


# --- consecutive editions: each one starts where the last stopped ---------

async def test_the_next_edition_carries_what_the_last_one_did_not(
    tmp_path, monkeypatch
):
    """Two syncs, two editions. The second carries only what the first did
    not, the first keeps what it published, and no article falls between
    them."""
    from tid import jobs
    from tid.store import ArticleRow, url_hash

    monkeypatch.setenv("TID_CACHE", str(tmp_path / "cache"))
    store = SqliteStore(tmp_path / "state.db")
    backend = FakeBackend()

    async def _sync(client):
        await cmd_gather(client, store, SOURCES)
        await cmd_summarize(store, backend)
        await cmd_rewrite(store, backend)
        key = await jobs.current_key(store, SOURCES)
        return await jobs.build_edition_for_key(
            key, store, SOURCES, snapshot=True
        )

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_handler)
        ) as client:
            first = await _sync(client)
            assert {i.title for i in first.items} == {
                "A Story About & Symbols", "Second Story",
            }

            # A later sync brings in one more story, written straight to the
            # store so the test does not depend on the mock feed changing.
            url = "http://articles.invalid/three"
            await store.upsert_rows([ArticleRow(
                id=url_hash(url), url=url, title="Third Story",
                source="Test Feed", text="body", summary="lede",
                body="Rewritten paragraph three.",
                published="2026-08-05",
                fetched_at="2099-01-01T00:00:00+00:00",
            )])

            second = await _sync(client)
    finally:
        await store.close()

    assert [i.title for i in second.items] == ["Third Story"]
    assert second.key != first.key

    # Both papers are still on the shelf, and between them they carry
    # everything the store ever had ready — each story exactly once.
    import tid.archive as archive
    published = [
        title
        for row in archive.readable(tmp_path / "cache")
        for title in (i.title for i in archive.load(tmp_path / "cache", row.key).items)
    ]
    assert sorted(published) == [
        "A Story About & Symbols", "Second Story", "Third Story",
    ]
