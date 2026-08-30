"""The edition key: "which edition is this?", as a hash.

The current edition is determined by:
  - the high-water mark of new content (max fetched_at in the store)
  - the sources config (sources.toml hashed)

When either changes, the key changes and the next request assembles — and
snapshots (see `tid.archive`) — a new edition.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _source_fields(s: dict) -> dict:
    """The parts of a source config that change which articles get rendered."""
    fields = {
        "name": s.get("name"),
        "kind": s.get("kind"),
    }
    # since_hours changes how far back a gather reaches, and so which articles
    # ever enter the store — but only include it when actually set, so configs
    # that don't use it keep the keys their snapshots were built under.
    if s.get("since_hours") is not None:
        fields["since_hours"] = s["since_hours"]
    # `section` and `medium` change the edition's *shape* rather than its
    # contents, but a reader who re-files a source into another column expects
    # to see that without waiting for the next ingest. Included only when set,
    # for the same upgrade reason as since_hours above.
    for optional in ("section", "medium"):
        if s.get(optional) is not None:
            fields[optional] = s[optional]
    return fields


def edition_key(content_token: str, sources_config: list[dict]) -> str:
    """Stable hash representing 'which edition this is'."""
    payload = json.dumps(
        {
            "content": content_token,
            "sources": [_source_fields(s) for s in sources_config],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def ensure_dir(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
