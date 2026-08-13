"""The list of every edition that has been built.

The cache (`tid.cache`) is keyed by a content hash, which answers "is
this edition still current?" but not "what did we publish last week" — the
key carries no date and the files carry no metadata. So each build drops a
small JSON sidecar next to its PDF, and the archive is those sidecars sorted
newest-first.

Sidecar-less PDFs (anything cached before this module existed) still show up,
described by what the filesystem knows: mtime for the date, size for the size,
no article count. That keeps the index honest about older editions instead of
hiding them.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .cache import pdf_path, preview_path

# Cache keys are `hashlib.sha256(...).hexdigest()[:24]`. Anything reaching a
# route handler as a "key" is matched against this before it is joined onto a
# path, so no request can walk out of the cache directory.
KEY_RE = re.compile(r"^[0-9a-f]{6,64}$")


def is_key(value: str) -> bool:
    return bool(KEY_RE.match(value))


@dataclass(frozen=True)
class Edition:
    key: str
    date: str                       # ISO date the edition was rendered for
    built_at: str                   # ISO-8601 UTC timestamp of the build
    articles: int | None            # None when only the PDF survives
    sources: dict[str, int] = field(default_factory=dict)
    size: int = 0
    has_preview: bool = False


def sidecar_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def record(cache_dir: Path, key: str, date: str, articles: list[dict]) -> Path:
    """Write the sidecar describing a freshly built edition.

    Written to a temp file and renamed, so a reader scanning the directory
    mid-write sees either the old sidecar or the new one, never half of one.
    """
    counts = Counter(a.get("source", "?") for a in articles)
    payload = {
        "key": key,
        "date": date,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "articles": len(articles),
        "sources": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }
    out = sidecar_path(cache_dir, key)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    return out


def _from_sidecar(cache_dir: Path, key: str, pdf: Path) -> Edition | None:
    try:
        data = json.loads(sidecar_path(cache_dir, key).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return Edition(
        key=key,
        date=str(data.get("date") or ""),
        built_at=str(data.get("built_at") or ""),
        articles=data.get("articles"),
        sources=dict(data.get("sources") or {}),
        size=pdf.stat().st_size,
        has_preview=preview_path(cache_dir, key).exists(),
    )


def _from_file(cache_dir: Path, key: str, pdf: Path) -> Edition:
    """Fallback for a PDF with no sidecar: describe it from its own mtime."""
    stat = pdf.stat()
    when = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return Edition(
        key=key,
        date=when.date().isoformat(),
        built_at=when.isoformat(timespec="seconds"),
        articles=None,
        size=stat.st_size,
        has_preview=preview_path(cache_dir, key).exists(),
    )


def editions(cache_dir: Path) -> list[Edition]:
    """Every built edition, newest build first."""
    if not cache_dir.is_dir():
        return []
    out: list[Edition] = []
    for pdf in cache_dir.glob("*.pdf"):
        key = pdf.stem
        if not is_key(key):
            continue
        try:
            out.append(_from_sidecar(cache_dir, key, pdf)
                       or _from_file(cache_dir, key, pdf))
        except OSError:
            continue  # vanished mid-scan; it simply isn't in the archive
    out.sort(key=lambda e: (e.built_at, e.key), reverse=True)
    return out


def find(cache_dir: Path, key: str) -> Path | None:
    """The archived PDF for `key`, or None if the key is bogus or missing."""
    if not is_key(key):
        return None
    pdf = pdf_path(cache_dir, key)
    return pdf if pdf.exists() else None
