"""Shared fixtures.

The `store` fixture is parametrized across every shipped backend so the
protocol is tested once and honoured by all of them. SQLite always runs; Mongo
runs only when a server is reachable::

    TID_TEST_MONGO_URI=mongodb://localhost:27017 uv run pytest

Without that variable the Mongo params skip, so the default `uv run pytest`
needs no services.
"""
from __future__ import annotations

import itertools
import os

import pytest

from tid.store import SqliteStore

_counter = itertools.count()


def _mongo_uri() -> str | None:
    return os.environ.get("TID_TEST_MONGO_URI")


@pytest.fixture(params=["sqlite", "mongo"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStore(tmp_path / "state.db")
        try:
            yield s
        finally:
            await s.close()
        return

    uri = _mongo_uri()
    if not uri:
        pytest.skip("set TID_TEST_MONGO_URI to exercise the Mongo store")
    pytest.importorskip("pymongo", reason="needs tid[mongo]")
    from tid.store.mongo import MongoStore

    collection = f"article_test_{os.getpid()}_{next(_counter)}"
    s = MongoStore(uri=uri, database="tid_test", collection=collection)
    try:
        yield s
    finally:
        await s.col.drop()
        await s.close()
