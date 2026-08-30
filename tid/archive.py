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

Which articles an edition carries is not recorded here at all: the store
stamps `rendered_at` on every article a snapshot takes, so "has this been
published" is a fact about the article rather than a window this module has to
reconstruct. All that is left for the archive to decide is where the very
first edition starts, when there is no publication history to go on — see
`floor`.

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
from datetime import datetime, timedelta, timezone
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


def snapshot_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def record(
    cache_dir: Path, key: str, date: str, articles: list[dict]
) -> ed.Edition:
    """Write the snapshot for a freshly assembled edition, and return it.

    Written to a temp file and renamed, so a reader scanning the directory
    mid-write sees either the old snapshot or the new one, never half of one.
    """
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    built = ed.build(articles, key=key, date=date, built_at=built_at)
    payload = ed.to_snapshot(built)
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


# How far back the *first* edition reaches, when no article has ever been
# published and there is therefore no publication state to go on: a fresh
# install, or an archive whose editions have been cleared. Not "the whole
# store" — rows are never deleted, so that would put months of accumulated
# articles on one front page.
NO_HISTORY_WINDOW = timedelta(hours=30)


def floor(cache_dir: Path) -> str | None:
    """The `fetched_at` bound for the next edition, or None for no bound.

    None is the normal answer: `rendered_at` already says which articles are
    unpublished, and an article that took three days to get through the
    rewrite queue should still run when it is finally ready.

    The bound only applies when nothing has ever been published, where that
    reasoning would instead empty the entire store onto one front page.
    """
    if readable(cache_dir):
        return None
    return (
        datetime.now(timezone.utc) - NO_HISTORY_WINDOW
    ).isoformat(timespec="seconds")


def latest(cache_dir: Path) -> Edition | None:
    """The most recently built readable edition, or None.

    What `/` falls back to when a new key assembles nothing: a sources.toml
    edit moves the key without gathering anything, and a paper that shows
    yesterday's news beats one that shows none.
    """
    rows = readable(cache_dir)
    return rows[0] if rows else None
