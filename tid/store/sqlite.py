"""SQLite store — the default backend. Zero extra services.

The `sqlite3` calls are synchronous, so each public method hands its work to a
worker thread. One connection is shared under a lock rather than opened per
call: the write path is a handful of tiny statements and SQLite serializes
writers anyway, so a pool would buy nothing and cost a `PRAGMA` round trip
each time.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .base import ArticleRow, norm_title, now_iso, url_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS article (
    url_hash       TEXT PRIMARY KEY,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    title_norm     TEXT NOT NULL,
    source         TEXT NOT NULL,
    text           TEXT,              -- NULL if extraction failed (raw trafilatura output)
    body           TEXT,              -- NULL until rewritten (clean paragraphs)
    summary        TEXT,              -- NULL until summarized
    surfaced       TEXT,              -- when the source surfaced it (HN submission / RSS pub)
    published      TEXT,              -- the article's own publication date (from page metadata)
    fetched_at     TEXT NOT NULL,
    extracted_at   TEXT,
    summarized_at  TEXT,
    rewritten_at   TEXT,
    rendered_at    TEXT,              -- ISO date of first edition inclusion; NULL = pending
    image          TEXT,              -- lead image URL (feed enclosure or og:image)
    topic          TEXT,              -- NULL until the topic stage files it
    topic_order    INTEGER,           -- the topic's rank within its edition
    main_rank      INTEGER,           -- 0 = the topic's lead story; NULL = not main
    kind           TEXT,              -- source type (tid.sources); NULL = pre-dates the column
    extra          TEXT               -- the type's own fields, as JSON; NULL = none
);
CREATE INDEX IF NOT EXISTS idx_title_norm  ON article(title_norm);
CREATE INDEX IF NOT EXISTS idx_rendered_at ON article(rendered_at);
CREATE INDEX IF NOT EXISTS idx_source_date ON article(source, published, surfaced, fetched_at);
"""

_COLUMNS = (
    "url_hash", "url", "title", "title_norm", "source", "text", "body",
    "summary", "surfaced", "published", "fetched_at", "extracted_at",
    "summarized_at", "rewritten_at", "rendered_at", "image", "topic",
    "topic_order", "main_rank", "kind", "extra",
)

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM article"


# Columns added after the first release, with the type they are declared as.
# SQLite is dynamically typed, so the declaration only matters for ordering
# comparisons — which `topic_order` and `main_rank` are used for.
_ADDED_COLUMNS = {
    "body": "TEXT",
    "rewritten_at": "TEXT",
    "surfaced": "TEXT",
    "published": "TEXT",
    "image": "TEXT",
    "topic": "TEXT",
    "topic_order": "INTEGER",
    "main_rank": "INTEGER",
    "kind": "TEXT",
    "extra": "TEXT",
}


def _dump_extra(extra: dict[str, Any] | None) -> str | None:
    """Empty means NULL, so `COALESCE(excluded.extra, extra)` in the bulk
    copy keeps what the target has rather than blanking it."""
    return json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else None


def _load_extra(blob: str | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _migrate(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(article)")}
    for name, decl in _ADDED_COLUMNS.items():
        if name not in cols:
            con.execute(f"ALTER TABLE article ADD COLUMN {name} {decl}")
    # Re-normalize titles stored under an older `norm_title`. The ASCII-only
    # version left every non-Latin headline as "" or a stray Latin word, and
    # those stale values would keep matching (or missing) new arrivals. One
    # pass, idempotent: the second open finds nothing to change.
    stale = [
        (norm, h) for h, title, stored in
        con.execute("SELECT url_hash, title, title_norm FROM article")
        if (norm := norm_title(title)) != stored
    ]
    if stale:
        con.executemany(
            "UPDATE article SET title_norm = ? WHERE url_hash = ?", stale
        )
    con.commit()


def _row(r: sqlite3.Row) -> ArticleRow:
    return ArticleRow(
        id=r["url_hash"],
        url=r["url"],
        title=r["title"],
        title_norm=r["title_norm"],
        source=r["source"],
        text=r["text"],
        body=r["body"],
        summary=r["summary"],
        surfaced=r["surfaced"],
        published=r["published"],
        fetched_at=r["fetched_at"],
        extracted_at=r["extracted_at"],
        summarized_at=r["summarized_at"],
        rewritten_at=r["rewritten_at"],
        rendered_at=r["rendered_at"],
        image=r["image"],
        topic=r["topic"],
        topic_order=r["topic_order"],
        main_rank=r["main_rank"],
        kind=r["kind"] or "",
        extra=_load_extra(r["extra"]),
    )


# Structural conformance to store.base.Store — deliberately not a
# subclass, so a downstream store needs no import from this module.
class SqliteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because every call runs on an arbitrary
        # asyncio worker thread; _lock is what actually serializes access.
        self.con = sqlite3.connect(str(self.path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.con.executescript(_SCHEMA)
            _migrate(self.con)

    # --- plumbing -------------------------------------------------------

    def _sync(self, fn, *args):
        with self._lock:
            return fn(*args)

    async def _run(self, fn, *args):
        return await asyncio.to_thread(self._sync, fn, *args)

    # --- gather ---------------------------------------------------------

    async def exists(self, url: str, title: str | None = None) -> bool:
        norm = norm_title(title) if title else ""

        def _q() -> bool:
            if norm:
                cur = self.con.execute(
                    "SELECT 1 FROM article WHERE url_hash = ? OR title_norm = ? LIMIT 1",
                    (url_hash(url), norm),
                )
            else:
                cur = self.con.execute(
                    "SELECT 1 FROM article WHERE url_hash = ? LIMIT 1",
                    (url_hash(url),),
                )
            return cur.fetchone() is not None

        return await self._run(_q)

    async def insert_raw(
        self,
        source: str,
        url: str,
        title: str,
        text: str | None,
        surfaced: str | None = None,
        published: str | None = None,
        image: str | None = None,
        kind: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        def _w() -> None:
            now = now_iso()
            h = url_hash(url)
            self.con.execute(
                """
                INSERT OR IGNORE INTO article
                  (url_hash, url, title, title_norm, source, text,
                   surfaced, published, fetched_at, extracted_at, image,
                   kind, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    h, url, title, norm_title(title), source, text,
                    surfaced, published,
                    now,
                    now if text is not None else None,
                    image,
                    kind or None, _dump_extra(extra),
                ),
            )
            # Back-fill date fields on rows that exist but lack them (re-gather).
            if surfaced:
                self.con.execute(
                    "UPDATE article SET surfaced = ? WHERE url_hash = ? AND surfaced IS NULL",
                    (surfaced, h),
                )
            if published:
                self.con.execute(
                    "UPDATE article SET published = ? WHERE url_hash = ? AND published IS NULL",
                    (published, h),
                )
            if image:
                self.con.execute(
                    "UPDATE article SET image = ? WHERE url_hash = ? AND image IS NULL",
                    (image, h),
                )
            # The source type's fields, on a row written before they were
            # recorded. An HN story gathered last month gains its item id —
            # and so its discussion chip — the next time the feed still
            # carries it.
            if kind:
                self.con.execute(
                    "UPDATE article SET kind = ? WHERE url_hash = ? "
                    "AND (kind IS NULL OR kind = '')",
                    (kind, h),
                )
            if extra:
                self.con.execute(
                    "UPDATE article SET extra = ? WHERE url_hash = ? "
                    "AND (extra IS NULL OR extra = '' OR extra = '{}')",
                    (_dump_extra(extra), h),
                )
            self.con.commit()

        await self._run(_w)

    # --- summarize / rewrite --------------------------------------------

    async def pending_summary(self) -> list[ArticleRow]:
        return await self._pending("summary")

    async def pending_rewrite(self) -> list[ArticleRow]:
        return await self._pending("body")

    async def _pending(self, column: str) -> list[ArticleRow]:
        def _q() -> list[ArticleRow]:
            cur = self.con.execute(
                f"{_SELECT} WHERE {column} IS NULL AND text IS NOT NULL "
                "ORDER BY fetched_at ASC"
            )
            return [_row(r) for r in cur.fetchall()]

        return await self._run(_q)

    async def set_summary(self, article_id: str, summary: str) -> None:
        await self._set("summary", "summarized_at", article_id, summary)

    async def set_body(self, article_id: str, body: str) -> None:
        await self._set("body", "rewritten_at", article_id, body)

    # --- topics ---------------------------------------------------------

    async def set_topic(
        self,
        article_id: str,
        topic: str | None,
        topic_order: int | None = None,
        main_rank: int | None = None,
    ) -> None:
        """File (or, with topic=None, unfile) one article. See store.base."""
        def _w() -> None:
            self.con.execute(
                "UPDATE article SET topic = ?, topic_order = ?, main_rank = ? "
                "WHERE url_hash = ?",
                (topic, topic_order, main_rank, article_id),
            )
            self.con.commit()

        await self._run(_w)

    async def _set(self, column: str, stamp: str, article_id: str, value: str) -> None:
        def _w() -> None:
            self.con.execute(
                f"UPDATE article SET {column} = ?, {stamp} = ? WHERE url_hash = ?",
                (value, now_iso(), article_id),
            )
            self.con.commit()

        await self._run(_w)

    # --- render ---------------------------------------------------------

    async def pending_render(self) -> list[ArticleRow]:
        def _q() -> list[ArticleRow]:
            cur = self.con.execute(
                f"{_SELECT} WHERE rendered_at IS NULL "
                "AND summary IS NOT NULL AND text IS NOT NULL "
                "AND body IS NOT NULL"
            )
            return [_row(r) for r in cur.fetchall()]

        return await self._run(_q)

    async def unpublished(
        self, source: str, floor: str | None = None
    ) -> list[ArticleRow]:
        """Every finished article for `source` that no edition has carried
        yet, newest first by best available date.

        Finished means rewritten, not merely summarized. The paper's whole
        premise is the cleaned-up, translated body rather than whatever
        trafilatura scraped, and publishing marks an article done — so going
        out early with raw text is not "a bit rough this edition", it is that
        article's only appearance. A rewrite still in the queue simply waits
        for the next paper.

        Publication state is per-article — `rendered_at`, stamped when an
        edition snapshots the article — and not a timestamp comparison. That
        distinction is the whole point: one gather stamps every row it writes
        with the same `fetched_at` second, but those rows finish summarizing
        and rewriting at very different times. A cutoff of "newer than the last
        edition's high-water mark" silently drops every article that was still
        in the pipeline when that edition went out, because it shares its
        second with articles that made it.

        `floor` is a `fetched_at` bound used only when there is no published
        history to go on (see `archive.floor`); None means publication state
        alone decides.
        """
        def _q() -> list[ArticleRow]:
            cur = self.con.execute(
                f"""
                {_SELECT}
                 WHERE source = ?1
                   AND text        IS NOT NULL
                   AND summary     IS NOT NULL
                   AND body        IS NOT NULL
                   AND rendered_at IS NULL
                   AND (?2 IS NULL OR fetched_at > ?2)
                 ORDER BY COALESCE(published, surfaced, fetched_at) DESC
                """,
                (source, floor),
            )
            return [_row(r) for r in cur.fetchall()]

        return await self._run(_q)

    async def retire_before(self, cutoff: str, date: str) -> int:
        """Mark every ready, never-published article older than `cutoff` as
        published, without putting it in a paper.

        Used once, when an edition runs with a floor because there is no
        history to follow. Without it the backlog the floor just excluded would
        simply arrive in the *next* edition, which sees published history and
        so applies no floor at all — turning the bound into a one-edition
        deferral rather than a bound. Returns how many were retired.
        """
        def _w() -> int:
            cur = self.con.execute(
                """
                UPDATE article SET rendered_at = ?
                 WHERE rendered_at IS NULL
                   AND text     IS NOT NULL
                   AND summary  IS NOT NULL
                   AND body     IS NOT NULL
                   AND fetched_at <= ?
                """,
                (date, cutoff),
            )
            self.con.commit()
            return cur.rowcount

        return await self._run(_w)

    async def mark_rendered(self, article_ids: list[str], date: str) -> None:
        def _w() -> None:
            self.con.executemany(
                "UPDATE article SET rendered_at = ? WHERE url_hash = ?",
                [(date, h) for h in article_ids],
            )
            self.con.commit()

        await self._run(_w)

    # --- meta -----------------------------------------------------------

    async def max_fetched_at(self) -> str:
        """Latest fetched_at timestamp in the store (for cache keying)."""
        def _q() -> str:
            row = self.con.execute(
                "SELECT COALESCE(MAX(fetched_at), '') FROM article"
            ).fetchone()
            return row[0] or ""

        return await self._run(_q)

    async def counts(self) -> dict[str, int]:
        def _q() -> dict[str, int]:
            c = self.con.execute
            return {
                "total":            c("SELECT COUNT(*) FROM article").fetchone()[0],
                "unreadable":       c("SELECT COUNT(*) FROM article WHERE text IS NULL").fetchone()[0],
                "pending_summary":  c("SELECT COUNT(*) FROM article WHERE summary IS NULL AND text IS NOT NULL").fetchone()[0],
                "pending_rewrite":  c("SELECT COUNT(*) FROM article WHERE body    IS NULL AND text IS NOT NULL").fetchone()[0],
                "pending_render":   c("SELECT COUNT(*) FROM article WHERE rendered_at IS NULL AND summary IS NOT NULL AND text IS NOT NULL AND body IS NOT NULL").fetchone()[0],
                "rendered":         c("SELECT COUNT(*) FROM article WHERE rendered_at IS NOT NULL").fetchone()[0],
            }

        return await self._run(_q)

    # --- bulk copy ------------------------------------------------------

    async def all_rows(self) -> list[ArticleRow]:
        def _q() -> list[ArticleRow]:
            cur = self.con.execute(f"{_SELECT} ORDER BY fetched_at ASC")
            return [_row(r) for r in cur.fetchall()]

        return await self._run(_q)

    async def upsert_rows(self, rows: list[ArticleRow]) -> int:
        def _w() -> int:
            self.con.executemany(
                f"""
                INSERT INTO article ({', '.join(_COLUMNS)})
                VALUES ({', '.join('?' * len(_COLUMNS))})
                ON CONFLICT(url_hash) DO UPDATE SET
                    text          = COALESCE(excluded.text, text),
                    body          = COALESCE(excluded.body, body),
                    summary       = COALESCE(excluded.summary, summary),
                    surfaced      = COALESCE(excluded.surfaced, surfaced),
                    published     = COALESCE(excluded.published, published),
                    extracted_at  = COALESCE(excluded.extracted_at, extracted_at),
                    summarized_at = COALESCE(excluded.summarized_at, summarized_at),
                    rewritten_at  = COALESCE(excluded.rewritten_at, rewritten_at),
                    rendered_at   = COALESCE(excluded.rendered_at, rendered_at),
                    image         = COALESCE(excluded.image, image),
                    topic         = COALESCE(excluded.topic, topic),
                    topic_order   = COALESCE(excluded.topic_order, topic_order),
                    main_rank     = COALESCE(excluded.main_rank, main_rank),
                    kind          = COALESCE(excluded.kind, kind),
                    extra         = COALESCE(excluded.extra, extra)
                """,
                [
                    (
                        r.id, r.url, r.title, r.title_norm, r.source, r.text,
                        r.body, r.summary, r.surfaced, r.published,
                        r.fetched_at, r.extracted_at, r.summarized_at,
                        r.rewritten_at, r.rendered_at, r.image, r.topic,
                        r.topic_order, r.main_rank,
                        r.kind or None, _dump_extra(r.extra),
                    )
                    for r in rows
                ],
            )
            self.con.commit()
            return len(rows)

        return await self._run(_w)

    async def close(self) -> None:
        await self._run(self.con.close)
