"""SQLite store — the default backend. Zero extra services.

The `sqlite3` calls are synchronous, so each public method hands its work to a
worker thread. One connection is shared under a lock rather than opened per
call: the write path is a handful of tiny statements and SQLite serializes
writers anyway, so a pool would buy nothing and cost a `PRAGMA` round trip
each time.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

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
    image          TEXT               -- lead image URL (feed enclosure or og:image)
);
CREATE INDEX IF NOT EXISTS idx_title_norm  ON article(title_norm);
CREATE INDEX IF NOT EXISTS idx_rendered_at ON article(rendered_at);
CREATE INDEX IF NOT EXISTS idx_source_date ON article(source, published, surfaced, fetched_at);
"""

_COLUMNS = (
    "url_hash", "url", "title", "title_norm", "source", "text", "body",
    "summary", "surfaced", "published", "fetched_at", "extracted_at",
    "summarized_at", "rewritten_at", "rendered_at", "image",
)

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM article"


def _migrate(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(article)")}
    for name in ("body", "rewritten_at", "surfaced", "published", "image"):
        if name not in cols:
            con.execute(f"ALTER TABLE article ADD COLUMN {name} TEXT")
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

    async def exists(self, url: str, title: str) -> bool:
        def _q() -> bool:
            cur = self.con.execute(
                "SELECT 1 FROM article WHERE url_hash = ? OR title_norm = ? LIMIT 1",
                (url_hash(url), norm_title(title)),
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
    ) -> None:
        def _w() -> None:
            now = now_iso()
            h = url_hash(url)
            self.con.execute(
                """
                INSERT OR IGNORE INTO article
                  (url_hash, url, title, title_norm, source, text,
                   surfaced, published, fetched_at, extracted_at, image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    h, url, title, norm_title(title), source, text,
                    surfaced, published,
                    now,
                    now if text is not None else None,
                    image,
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
                "AND summary IS NOT NULL AND text IS NOT NULL"
            )
            return [_row(r) for r in cur.fetchall()]

        return await self._run(_q)

    async def ready_since(
        self, source: str, since: str | None = None
    ) -> list[ArticleRow]:
        """Every ready (text + summary) article for `source` gathered after
        `since`, newest first by best available date.

        `since` is a `fetched_at` timestamp — when we last published, not when
        the article was written. That is the difference that makes an edition
        "everything new since the last sync": a blog post from last week that
        a feed only surfaced to us this morning is new to the reader, and a
        date-based window would have thrown it away. None means no boundary,
        i.e. everything ready in the store.

        Safe as a string comparison: `fetched_at` is always
        `datetime.now(timezone.utc).isoformat(timespec="seconds")`, so
        lexicographic order is chronological order.
        """
        def _q() -> list[ArticleRow]:
            cur = self.con.execute(
                f"""
                {_SELECT}
                 WHERE source = ?1
                   AND text     IS NOT NULL
                   AND summary  IS NOT NULL
                   AND (?2 IS NULL OR fetched_at > ?2)
                 ORDER BY COALESCE(published, surfaced, fetched_at) DESC
                """,
                (source, since),
            )
            return [_row(r) for r in cur.fetchall()]

        return await self._run(_q)

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
                "pending_render":   c("SELECT COUNT(*) FROM article WHERE rendered_at IS NULL AND summary IS NOT NULL AND text IS NOT NULL").fetchone()[0],
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
                    image         = COALESCE(excluded.image, image)
                """,
                [
                    (
                        r.id, r.url, r.title, r.title_norm, r.source, r.text,
                        r.body, r.summary, r.surfaced, r.published,
                        r.fetched_at, r.extracted_at, r.summarized_at,
                        r.rewritten_at, r.rendered_at, r.image,
                    )
                    for r in rows
                ],
            )
            self.con.commit()
            return len(rows)

        return await self._run(_w)

    async def close(self) -> None:
        await self._run(self.con.close)
