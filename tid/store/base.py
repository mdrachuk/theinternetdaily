"""Storage protocol.

The pipeline talks to a `Store`, never to SQL. Rows come back as
`ArticleRow` dataclasses rather than `sqlite3.Row` so a non-SQLite backend is
a drop-in — the old `row["body"]` access pattern silently required a mapping
type and leaked the schema into `cli.py`.

SQLite is the default and needs no extra services. Mongo lives in
`tid.store.mongo` behind `pip install tid[mongo]`.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def url_hash(url: str) -> str:
    """The article id: a short, stable digest of the URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def norm_title(title: str) -> str:
    """Normalized title, used for cross-source duplicate detection.

    Keeps every letter and digit of every script, case-folded, and collapses
    everything else (punctuation, quotes, dashes, whitespace) to a single
    space. NFKC first, so a full-width digit or a ligature compares equal to
    its plain form. The previous `[^a-z0-9]` version threw away all
    non-ASCII, so a Cyrillic headline normalized to "" or to whatever Latin
    word or number it happened to contain, and one such row made every later
    headline from that source look like a duplicate.

    Can still come back empty (a title of nothing but punctuation); the
    stores never treat an empty normalized title as a match.
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in folded).split())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ArticleRow:
    """One article, in whatever state the pipeline has left it.

    `id` is `url_hash(url)`. `text` is the raw extracted body (NULL when
    extraction failed), `body` the rewritten one, `summary` the lede.

    The three `topic` fields are the edition's own filing, written by the topic
    stage (`tid.topics`) and re-written from scratch every time it runs: a
    topic set is a judgement about one edition, so an article that is still
    unpublished when the next one is named gets re-filed with it. All three are
    NULL when the stage has not run or could not place the article, and
    `tid.edition` then falls back to the `sources.toml` section.
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
    rendered_at: str | None = None    # ISO date of first edition inclusion
    image: str | None = None          # lead image URL, from the feed or og:image
    topic: str | None = None          # LLM-assigned topic for its edition
    topic_order: int | None = None    # that topic's rank among the edition's
    main_rank: int | None = None      # 0 = the topic's lead story; NULL = not main

    def __post_init__(self) -> None:
        if not self.title_norm:
            self.title_norm = norm_title(self.title)

    @property
    def sort_date(self) -> str:
        """Best available date, newest-first sortable. Mirrors the old
        COALESCE(published, surfaced, fetched_at)."""
        return self.published or self.surfaced or self.fetched_at

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
    async def exists(self, url: str, title: str | None = None) -> bool:
        """Is this URL already stored — or, given a title, is any URL whose
        normalized title matches? An empty normalized title matches nothing:
        it says the title carried no letters, not that two stories agree."""
        ...

    async def insert_raw(
        self,
        source: str,
        url: str,
        title: str,
        text: str | None,
        surfaced: str | None = None,
        published: str | None = None,
        image: str | None = None,
    ) -> None: ...

    # --- summarize ------------------------------------------------------
    async def pending_summary(self) -> list[ArticleRow]: ...
    async def set_summary(self, article_id: str, summary: str) -> None: ...

    # --- rewrite --------------------------------------------------------
    async def pending_rewrite(self) -> list[ArticleRow]: ...
    async def set_body(self, article_id: str, body: str) -> None: ...

    # --- topics ---------------------------------------------------------
    async def set_topic(
        self,
        article_id: str,
        topic: str | None,
        topic_order: int | None = None,
        main_rank: int | None = None,
    ) -> None:
        """File an article under an edition topic, or (with None) unfile it.

        Always a full overwrite, never a merge: the stage re-runs over every
        unpublished article each ingest, and an article left holding a topic
        that this edition no longer has would open a column of its own.
        """
        ...

    # --- render ---------------------------------------------------------
    async def pending_render(self) -> list[ArticleRow]: ...

    async def unpublished(
        self, source: str, floor: str | None = None
    ) -> list[ArticleRow]: ...

    async def retire_before(self, cutoff: str, date: str) -> int: ...

    async def mark_rendered(self, article_ids: list[str], date: str) -> None: ...

    # --- meta -----------------------------------------------------------
    async def max_fetched_at(self) -> str: ...
    async def counts(self) -> dict[str, int]: ...

    # --- bulk copy (used by `tid migrate`) ------------------------
    async def all_rows(self) -> list[ArticleRow]: ...
    async def upsert_rows(self, rows: list[ArticleRow]) -> int: ...

    async def close(self) -> None: ...
