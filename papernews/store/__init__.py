"""Article storage: a protocol, a SQLite default, and an optional Mongo backend.

    from papernews.store import open_store
    store = open_store("state.db")                       # SQLite (default)
    store = open_store("mongodb://localhost/papernews")  # needs [mongo]
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .base import (
    COUNT_KEYS,
    ArticleRow,
    Store,
    norm_title,
    now_iso,
    url_hash,
)
from .sqlite import SqliteStore

__all__ = [
    "COUNT_KEYS",
    "ArticleRow",
    "Store",
    "SqliteStore",
    "norm_title",
    "now_iso",
    "open_store",
    "url_hash",
]


def open_store(url: str | Any) -> Store:
    """Build a store from a URL or a filesystem path.

    Accepted forms::

        state.db                                  SQLite file
        sqlite:///var/lib/papernews/state.db      SQLite file (absolute)
        mongodb://host:27017/papernews            Mongo, db from the path
        mongodb://host/papernews?collection=feed  Mongo, explicit collection

    Constructing a store never does I/O beyond opening the SQLite file, so
    this is safe to call from synchronous setup code.
    """
    text = str(url)
    scheme = urlsplit(text).scheme

    if scheme in ("mongodb", "mongodb+srv"):
        from .mongo import MongoStore

        parts = urlsplit(text)
        database = parts.path.lstrip("/") or "papernews"
        query = parse_qs(parts.query)
        collection = query.pop("collection", ["article"])[0]
        # `collection` is ours, not a driver option — pymongo warns about
        # anything it does not recognise, so hand it a cleaned URI.
        uri = urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urlencode(query, doseq=True), parts.fragment)
        )
        return MongoStore(uri=uri, database=database, collection=collection)

    if scheme == "sqlite":
        # sqlite:///abs/path, sqlite://relative/path, sqlite://:memory:
        path = text[len("sqlite://"):]
        return SqliteStore(path or ":memory:")

    if len(scheme) > 1:
        # A one-character scheme is a Windows drive letter, not a scheme.
        raise ValueError(
            f"unsupported store url scheme {scheme!r}: expected a path, "
            "sqlite://, or mongodb://"
        )

    return SqliteStore(text)
