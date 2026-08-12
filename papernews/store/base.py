"""Storage protocol.

The pipeline talks to a `Store`, never to SQL. Rows come back as
`ArticleRow` dataclasses rather than `sqlite3.Row` so a non-SQLite backend is
a drop-in — the old `row["body"]` access pattern silently required a mapping
type and leaked the schema into `cli.py`.

SQLite is the default and needs no extra services. Mongo lives in
`papernews.store.mongo` behind `pip install papernews[mongo]`.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def url_hash(url: str) -> str:
    """The article id: a short, stable digest of the URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def norm_title(title: str) -> str:
    """Normalized title, used for cross-source duplicate detection."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ArticleRow:
    """One article, in whatever state the pipeline has left it.

    `id` is `url_hash(url)`. `text` is the raw extracted body (NULL when
    extraction failed), `body` the rewritten one, `summary` the lede.
    """
    id: str
    url: str
    title: str
    source: str
    title_norm: str = ""
    text: str | None = None
    body: str | None = None
    summary: str | None = None
    surfaced: str | None = None       # when the source surfaced it
    published: str | None = None      # the article's own publication date
    fetched_at: str = field(default_factory=now_iso)
    extracted_at: str | None = None
    summarized_at: str | None = None
    rewritten_at: str | None = None
    rendered_at: str | None = None    # ISO date of first PDF inclusion

    def __post_init__(self) -> None:
        if not self.title_norm:
            self.title_norm = norm_title(self.title)

    @property
    def sort_date(self) -> str:
        """Best available date, newest-first sortable. Mirrors the old
        COALESCE(published, surfaced, fetched_at)."""
        return self.published or self.surfaced or self.fetched_at

    @property
    def age_date(self) -> str | None:
        """Date used for `since_date` window filtering: the article's own date
        if known, else the date the source surfaced it. None means undated,
        which every backend treats as 'always keep'."""
        return self.published or self.surfaced

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# The `counts()` keys the status command and the web UI expect.
COUNT_KEYS = (
    "total",
    "unreadable",
    "pending_summary",
    "pending_rewrite",
    "pending_render",
    "rendered",
)


@runtime_checkable
class Store(Protocol):
    """Everything the pipeline needs from storage.

    Instance-scoped by design: one `Store` is one article collection. A
    downstream multi-user service gets isolation by constructing one store per
    user (its own file, database, or collection prefix) — there is no
    tenant_id anywhere in this package.
    """

    # --- gather ---------------------------------------------------------
    async def exists(self, url: str, title: str) -> bool: ...

    async def insert_raw(
        self,
        source: str,
        url: str,
        title: str,
        text: str | None,
        surfaced: str | None = None,
        published: str | None = None,
    ) -> None: ...

    # --- summarize ------------------------------------------------------
    async def pending_summary(self) -> list[ArticleRow]: ...
    async def set_summary(self, article_id: str, summary: str) -> None: ...

    # --- rewrite --------------------------------------------------------
    async def pending_rewrite(self) -> list[ArticleRow]: ...
    async def set_body(self, article_id: str, body: str) -> None: ...

    # --- render ---------------------------------------------------------
    async def pending_render(self) -> list[ArticleRow]: ...

    async def latest_per_source(
        self, source: str, limit: int, since_date: str | None = None
    ) -> list[ArticleRow]: ...

    async def mark_rendered(self, article_ids: list[str], date: str) -> None: ...

    # --- meta -----------------------------------------------------------
    async def max_fetched_at(self) -> str: ...
    async def counts(self) -> dict[str, int]: ...

    # --- bulk copy (used by `papernews migrate`) ------------------------
    async def all_rows(self) -> list[ArticleRow]: ...
    async def upsert_rows(self, rows: list[ArticleRow]) -> int: ...

    async def close(self) -> None: ...
