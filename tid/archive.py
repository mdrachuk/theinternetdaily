"""Every edition that has been assembled, kept as a snapshot on disk.

An edition is a JSON file in the cache directory named after its key: the
articles it was built from, with their bodies, in the order the store handed
them over. The *layout* is not stored — `tid.edition.build` recomputes it on
every render, which costs microseconds and means an improvement to the front
page reaches editions published last month too.

Snapshots are what makes yesterday's paper still readable. The store keeps
moving: a source is re-filed, a rewrite lands, a gather brings in fifty more
stories. Without a snapshot, "the edition of 12 August" would quietly become
"whatever the store would produce for 12 August today", which is a different
paper each time you open it.

Each snapshot also records where it *ended*: `watermark`, the newest
`fetched_at` among the articles it carries. That is what the next edition
starts from, so "everything since the last sync" is a fact on disk rather
than a guess about the clock. `content` — the store's `max_fetched_at` at
build time — sits beside it so that re-filing a source in sources.toml, which
moves the edition key without bringing in a single new article, does not look
like a sync that never happened (see `previous`).

Editions from before this module stored items — the PDF era — still list, with
`has_items` false. Nothing can render them as a page, and pretending they are
gone would be worse than saying so.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import edition as ed

# Cache keys are `hashlib.sha256(...).hexdigest()[:24]`. Anything reaching a
# route handler as a "key" is matched against this before it is joined onto a
# path, so no request can walk out of the cache directory.
KEY_RE = re.compile(r"^[0-9a-f]{6,64}$")


def is_key(value: str) -> bool:
    return bool(KEY_RE.match(value))


@dataclass(frozen=True)
class Edition:
    """One row of the archive: what a snapshot says about itself."""
    key: str
    date: str                       # ISO date the edition was assembled for
    built_at: str                   # ISO-8601 UTC timestamp of the build
    articles: int | None            # None when the snapshot predates the count
    sources: dict[str, int] = field(default_factory=dict)
    size: int = 0                   # snapshot bytes on disk
    has_items: bool = False         # false = PDF-era, cannot be rendered
    content: str = ""               # store max_fetched_at this was built from
    watermark: str = ""             # newest fetched_at among its articles


def snapshot_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def record(
    cache_dir: Path,
    key: str,
    date: str,
    articles: list[dict],
    content: str = "",
) -> ed.Edition:
    """Write the snapshot for a freshly assembled edition, and return it.

    Written to a temp file and renamed, so a reader scanning the directory
    mid-write sees either the old snapshot or the new one, never half of one.
    """
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    built = ed.build(articles, key=key, date=date, built_at=built_at)
    payload = ed.to_snapshot(built)
    payload["content"] = content
    # Taken over the articles actually carried, not over the store: a story
    # that was gathered but is still awaiting a summary must not be stepped
    # over by a watermark it never appeared under, or it would never run.
    payload["watermark"] = max(
        (a.get("fetched_at") or "" for a in articles), default=""
    )
    # Counted over the articles handed in, not the laid-out edition, so the
    # number in the archive is "what the store offered", independent of how
    # many columns the front page happened to have room for.
    payload["sources"] = dict(sorted(
        Counter(a.get("source", "?") for a in articles).items(),
        key=lambda kv: -kv[1],
    ))
    out = snapshot_path(cache_dir, key)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    return built


def load(cache_dir: Path, key: str) -> ed.Edition | None:
    """The full edition for `key`, ready to render, or None."""
    if not is_key(key):
        return None
    try:
        data = json.loads(snapshot_path(cache_dir, key).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("items"):
        return None
    return ed.from_snapshot(data)


def _describe(path: Path) -> Edition | None:
    try:
        data = json.loads(path.read_text("utf-8"))
        size = path.stat().st_size
    except (OSError, ValueError):
        return None
    return Edition(
        key=path.stem,
        date=str(data.get("date") or ""),
        built_at=str(data.get("built_at") or ""),
        articles=data.get("articles"),
        sources=dict(data.get("sources") or {}),
        size=size,
        has_items=bool(data.get("items")),
        content=str(data.get("content") or ""),
        watermark=str(data.get("watermark") or ""),
    )


def editions(cache_dir: Path) -> list[Edition]:
    """Every assembled edition, newest build first."""
    if not cache_dir.is_dir():
        return []
    out: list[Edition] = []
    for path in cache_dir.glob("*.json"):
        if not is_key(path.stem):
            continue
        row = _describe(path)
        if row is not None:
            out.append(row)
    out.sort(key=lambda e: (e.built_at, e.key), reverse=True)
    return out


def readable(cache_dir: Path) -> list[Edition]:
    """The editions a page can actually be built from, newest first."""
    return [e for e in editions(cache_dir) if e.has_items]


def previous(cache_dir: Path, content: str) -> Edition | None:
    """The last edition published from *different* content than `content`.

    Matched on the content token rather than just "the newest snapshot",
    because the edition key also moves when sources.toml changes. Re-filing a
    source into another column should re-lay-out the same articles, not start
    a fresh window that skips them all and leaves the reader a blank paper
    until the next gather.

    A snapshot from before editions recorded a token has none to compare, and
    counts as different: it was published by an older build, so it is
    unambiguously in the past. Skipping it instead would make the first
    edition after an upgrade start from nowhere and re-publish the entire
    store in one go.
    """
    for row in readable(cache_dir):
        if row.content != content:
            return row
    return None


def boundary(cache_dir: Path, content: str) -> str | None:
    """The `fetched_at` an edition built from `content` should start after.

    None when there is no previous edition to follow — the first paper carries
    everything the store has ready.
    """
    prev = previous(cache_dir, content)
    if prev is None:
        return None
    # Snapshots written before editions recorded a watermark still pin down a
    # moment: anything gathered after that paper was built is new to a reader
    # who has seen it. Same format on both sides (UTC isoformat to the second),
    # so the comparison is sound.
    return prev.watermark or prev.built_at or None
