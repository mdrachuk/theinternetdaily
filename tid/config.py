"""Environment-derived configuration.

Every value is read through a function, never captured at import time: a test
or a downstream consumer has to be able to change it without reimporting the
package. Step 6 of the plan replaces these with a single `Config` object
constructed by the CLI; the shape here is deliberately close to that.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path


def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, default))


def state_path() -> Path:
    """SQLite file, used when no store URL is set."""
    return _env_path("TID_STATE", "state.db")


def store_url() -> str:
    """Store URL. A `mongodb://…` value here moves the whole app off SQLite."""
    return os.environ.get("TID_STORE") or str(state_path())


def queue_url() -> str | None:
    """Job queue URL. Unset (or "local") means the in-process queue."""
    return os.environ.get("TID_QUEUE") or None


def config_path() -> Path:
    return _env_path("TID_CONFIG", "sources.toml")


def cache_dir() -> Path:
    return _env_path("TID_CACHE", "archive/cache")


def workers() -> int | None:
    """Concurrent LLM batches. None means "ask the backend" — its own limit is
    a better default than a fixed number, since a 70 W local GPU and a hosted
    API want very different values."""
    raw = os.environ.get("TID_WORKERS", "").strip()
    return int(raw) if raw else None


def llm_backend() -> str | None:
    """LLM backend name; None lets tid.llm.make_backend read the env."""
    return os.environ.get("LLM_BACKEND") or None


def load_sources(path: Path | None = None) -> list[dict]:
    with open(path or config_path(), "rb") as f:
        return tomllib.load(f).get("source", [])


def load_topics(path: Path | None = None) -> list[dict]:
    """The paper's standing sections: the `[[topic]]` tables of the config.

    Raw dicts, like `load_sources`; `tid.topics.standing_topics` turns them
    into `Topic`s and validates them. A config with no `[[topic]]` at all is
    fine — the topic stage then names every section itself, as it always did.
    """
    with open(path or config_path(), "rb") as f:
        return tomllib.load(f).get("topic", [])


def post_ingest_hook() -> str:
    return os.environ.get("POST_INGEST_HOOK", "").strip()


def post_ingest_hook_timeout() -> float:
    return float(os.environ.get("POST_INGEST_HOOK_TIMEOUT", "300"))
