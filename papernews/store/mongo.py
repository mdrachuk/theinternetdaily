"""MongoDB store — optional, behind `pip install papernews[mongo]`.

A single-user self-hoster should not have to run a database server to read a
newspaper, so this is not the default. It exists because a downstream
multi-user service wants per-user databases, and reimplementing this layer
there would guarantee the two drift apart.

Uses pymongo's own async client (`AsyncMongoClient`). Motor is EOL — do not
add it.

Isolation is a constructor argument: point each instance at its own database
(or its own collection name) and the documents never mix. There is no
tenant_id in the schema.
"""
from __future__ import annotations

from typing import Any

from .base import ArticleRow, norm_title, now_iso, url_hash

_FIELDS = (
    "url", "title", "title_norm", "source", "text", "body", "summary",
    "surfaced", "published", "fetched_at", "extracted_at", "summarized_at",
    "rewritten_at", "rendered_at",
)


def _to_doc(row: ArticleRow) -> dict[str, Any]:
    doc = {f: getattr(row, f) for f in _FIELDS}
    doc["_id"] = row.id
    # Denormalized at write time: the SQL ordering is
    # COALESCE(published, surfaced, fetched_at), and reproducing that in an
    # aggregation on every read would be both slower and harder to index.
    doc["sort_date"] = row.sort_date
    doc["age_date"] = row.age_date
    return doc


def _from_doc(doc: dict[str, Any]) -> ArticleRow:
    return ArticleRow(
        id=doc["_id"],
        url=doc.get("url", ""),
        title=doc.get("title", ""),
        title_norm=doc.get("title_norm", ""),
        source=doc.get("source", ""),
        text=doc.get("text"),
        body=doc.get("body"),
        summary=doc.get("summary"),
        surfaced=doc.get("surfaced"),
        published=doc.get("published"),
        fetched_at=doc.get("fetched_at", ""),
        extracted_at=doc.get("extracted_at"),
        summarized_at=doc.get("summarized_at"),
        rewritten_at=doc.get("rewritten_at"),
        rendered_at=doc.get("rendered_at"),
    )


class MongoStore:
    """Structural conformance to store.base.Store, over one collection."""

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "papernews",
        collection: str = "article",
        client: Any | None = None,
    ):
        from pymongo import AsyncMongoClient

        self._owns_client = client is None
        self.client = client if client is not None else AsyncMongoClient(uri)
        self.col = self.client[database][collection]
        self._indexed = False

    async def ensure_indexes(self) -> None:
        """Idempotent; called lazily before the first query so constructing a
        store stays synchronous."""
        if self._indexed:
            return
        from pymongo import ASCENDING, DESCENDING

        await self.col.create_index([("title_norm", ASCENDING)])
        await self.col.create_index(
            [("source", ASCENDING), ("sort_date", DESCENDING)]
        )
        await self.col.create_index([("fetched_at", DESCENDING)])
        # Partial indexes: the pending-work queries only ever look at the rows
        # where the field is still missing, which is a small slice of the
        # collection once the pipeline has caught up.
        await self.col.create_index(
            [("fetched_at", ASCENDING)],
            name="pending_summary",
            partialFilterExpression={"summary": None},
        )
        await self.col.create_index(
            [("fetched_at", ASCENDING)],
            name="pending_rewrite",
            partialFilterExpression={"body": None},
        )
        self._indexed = True

    # --- gather ---------------------------------------------------------

    async def exists(self, url: str, title: str) -> bool:
        await self.ensure_indexes()
        doc = await self.col.find_one(
            {"$or": [{"_id": url_hash(url)}, {"title_norm": norm_title(title)}]},
            {"_id": 1},
        )
        return doc is not None

    async def insert_raw(
        self,
        source: str,
        url: str,
        title: str,
        text: str | None,
        surfaced: str | None = None,
        published: str | None = None,
    ) -> None:
        await self.ensure_indexes()
        now = now_iso()
        row = ArticleRow(
            id=url_hash(url), url=url, title=title, source=source,
            text=text, surfaced=surfaced, published=published,
            fetched_at=now, extracted_at=now if text is not None else None,
        )
        doc = _to_doc(row)
        _id = doc.pop("_id")
        await self.col.update_one(
            {"_id": _id}, {"$setOnInsert": doc}, upsert=True
        )
        # Back-fill date fields on rows that exist but lack them (re-gather),
        # and keep the denormalized sort/age dates consistent with them.
        backfill: dict[str, Any] = {}
        if surfaced:
            backfill["surfaced"] = surfaced
        if published:
            backfill["published"] = published
        for field, value in backfill.items():
            await self.col.update_one(
                {"_id": _id, "$or": [{field: None}, {field: {"$exists": False}}]},
                {"$set": {field: value}},
            )
        if backfill:
            await self._refresh_dates(_id)

    async def _refresh_dates(self, _id: str) -> None:
        doc = await self.col.find_one({"_id": _id})
        if doc is None:
            return
        row = _from_doc(doc)
        await self.col.update_one(
            {"_id": _id},
            {"$set": {"sort_date": row.sort_date, "age_date": row.age_date}},
        )

    # --- summarize / rewrite --------------------------------------------

    async def pending_summary(self) -> list[ArticleRow]:
        return await self._pending("summary")

    async def pending_rewrite(self) -> list[ArticleRow]:
        return await self._pending("body")

    async def _pending(self, field: str) -> list[ArticleRow]:
        await self.ensure_indexes()
        cursor = self.col.find(
            {field: None, "text": {"$ne": None}}
        ).sort("fetched_at", 1)
        return [_from_doc(d) async for d in cursor]

    async def set_summary(self, article_id: str, summary: str) -> None:
        await self.col.update_one(
            {"_id": article_id},
            {"$set": {"summary": summary, "summarized_at": now_iso()}},
        )

    async def set_body(self, article_id: str, body: str) -> None:
        await self.col.update_one(
            {"_id": article_id},
            {"$set": {"body": body, "rewritten_at": now_iso()}},
        )

    # --- render ---------------------------------------------------------

    async def pending_render(self) -> list[ArticleRow]:
        await self.ensure_indexes()
        cursor = self.col.find(
            {"rendered_at": None, "summary": {"$ne": None}, "text": {"$ne": None}}
        )
        return [_from_doc(d) async for d in cursor]

    async def latest_per_source(
        self, source: str, limit: int, since_date: str | None = None
    ) -> list[ArticleRow]:
        await self.ensure_indexes()
        query: dict[str, Any] = {
            "source": source,
            "text": {"$ne": None},
            "summary": {"$ne": None},
        }
        if since_date is not None:
            # Undated rows are always kept — same rule as the SQLite store and
            # as the gather-time filter.
            query["$or"] = [
                {"age_date": None},
                {"age_date": {"$exists": False}},
                {"age_date": {"$gte": since_date}},
            ]
        cursor = self.col.find(query).sort("sort_date", -1).limit(limit)
        return [_from_doc(d) async for d in cursor]

    async def mark_rendered(self, article_ids: list[str], date: str) -> None:
        if not article_ids:
            return
        await self.col.update_many(
            {"_id": {"$in": article_ids}}, {"$set": {"rendered_at": date}}
        )

    # --- meta -----------------------------------------------------------

    async def max_fetched_at(self) -> str:
        doc = await self.col.find_one(
            {}, {"fetched_at": 1}, sort=[("fetched_at", -1)]
        )
        return (doc or {}).get("fetched_at") or ""

    async def counts(self) -> dict[str, int]:
        await self.ensure_indexes()
        ready = {"summary": {"$ne": None}, "text": {"$ne": None}}
        return {
            "total": await self.col.count_documents({}),
            "unreadable": await self.col.count_documents({"text": None}),
            "pending_summary": await self.col.count_documents(
                {"summary": None, "text": {"$ne": None}}
            ),
            "pending_rewrite": await self.col.count_documents(
                {"body": None, "text": {"$ne": None}}
            ),
            "pending_render": await self.col.count_documents(
                {"rendered_at": None, **ready}
            ),
            "rendered": await self.col.count_documents(
                {"rendered_at": {"$ne": None}}
            ),
        }

    # --- bulk copy ------------------------------------------------------

    async def all_rows(self) -> list[ArticleRow]:
        cursor = self.col.find({}).sort("fetched_at", 1)
        return [_from_doc(d) async for d in cursor]

    async def upsert_rows(self, rows: list[ArticleRow]) -> int:
        if not rows:
            return 0
        from pymongo import UpdateOne

        ops = []
        for row in rows:
            doc = _to_doc(row)
            _id = doc.pop("_id")
            # Never overwrite a populated field with a null from the source
            # store: a migration must not lose a summary the target already has.
            setters = {k: v for k, v in doc.items() if v is not None}
            ops.append(UpdateOne({"_id": _id}, {"$set": setters}, upsert=True))
        await self.col.bulk_write(ops, ordered=False)
        return len(rows)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()
