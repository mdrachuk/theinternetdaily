"""The source types, and the registry that maps a `kind` to one.

    from tid import sources
    items = await sources.fetch(client, src)          # gather
    chips = sources.links_for(item)                   # render

Adding a kind is one module with a class that satisfies `SourceType`, plus a
line in `TYPES`. Nothing else in the package names a kind.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Link, RawItem, SourceType, clean_title
from .hn import HackerNewsSource, fetch_hn
from .rss import RssSource, entry_image, fetch_rss
from .wikipedia import WikipediaEventsSource, fetch_wikipedia_events

__all__ = [
    "DEFAULT_KIND",
    "Link",
    "RawItem",
    "SourceType",
    "TYPES",
    "clean_title",
    "entry_image",
    "fetch",
    "fetch_hn",
    "fetch_rss",
    "fetch_wikipedia_events",
    "get",
    "links_for",
]

# A `[[source]]` with no `kind` is a feed: that is what every one of them was
# before the field existed.
DEFAULT_KIND = "rss"

TYPES: dict[str, SourceType] = {
    t.kind: t for t in (RssSource(), HackerNewsSource(), WikipediaEventsSource())
}


def get(kind: str | None) -> SourceType | None:
    """The type for a kind, or None for one nobody registered."""
    return TYPES.get(kind or DEFAULT_KIND)


def kind_of(src: dict[str, Any]) -> str:
    return str(src.get("kind") or DEFAULT_KIND)


async def fetch(
    client: httpx.AsyncClient, src: dict[str, Any]
) -> list[RawItem]:
    """Resolve one `[[source]]` table to its raw items.

    Raises `LookupError` for an unknown kind, and whatever the type raises for
    a failed fetch; the gather turns either into a logged skip.
    """
    kind = kind_of(src)
    source_type = get(kind)
    if source_type is None:
        raise LookupError(f"unknown source kind {kind!r}")
    return await source_type.fetch(client, src)


def links_for(item: Any) -> list[Link]:
    """The byline's links for an article — an `edition.Item`, an
    `ArticleRow`, or anything else carrying `url`, `source`, `kind` and
    `extra`.

    A kind nobody registered — a snapshot written by a build that had a type
    this one does not — degrades to the feed's single link rather than to a
    byline with nothing to click.
    """
    kind = getattr(item, "kind", "") or DEFAULT_KIND
    source_type = get(kind) or TYPES[DEFAULT_KIND]
    return source_type.links(
        getattr(item, "url", "") or "",
        getattr(item, "source", "") or "",
        getattr(item, "extra", None) or {},
    )
