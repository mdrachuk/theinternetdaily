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
    return _env_path("PAPERNEWS_STATE", "state.db")


def store_url() -> str:
    """Store URL. A `mongodb://…` value here moves the whole app off SQLite."""
    return os.environ.get("PAPERNEWS_STORE") or str(state_path())


def queue_url() -> str | None:
    """Job queue URL. Unset (or "local") means the in-process queue."""
    return os.environ.get("PAPERNEWS_QUEUE") or None


def config_path() -> Path:
    return _env_path("PAPERNEWS_CONFIG", "sources.toml")


def cache_dir() -> Path:
    return _env_path("PAPERNEWS_CACHE", "archive/cache")


def workers() -> int:
    """Concurrent LLM batches."""
    return int(os.environ.get("PAPERNEWS_WORKERS", "8"))


def load_sources(path: Path | None = None) -> list[dict]:
    with open(path or config_path(), "rb") as f:
        return tomllib.load(f).get("source", [])


def post_ingest_hook() -> str:
    return os.environ.get("POST_INGEST_HOOK", "").strip()


def post_ingest_hook_timeout() -> float:
    return float(os.environ.get("POST_INGEST_HOOK_TIMEOUT", "300"))
