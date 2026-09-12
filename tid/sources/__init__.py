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

from .. import extract as _extract
from .base import Link, RawItem, SourceType, clean_title
from .hn import HackerNewsSource, fetch_hn
from .rss import RssSource, entry_image, fetch_rss
from .wikipedia import WikipediaEventsSource, fetch_wikipedia_events
from .youtube import YouTubeSource, fetch_youtube

__all__ = [
    "DEFAULT_KIND",
    "Link",
    "RawItem",
    "SourceType",
    "TYPES",
    "clean_title",
    "embed_for",
    "entry_image",
    "extract",
    "fetch",
    "fetch_hn",
    "fetch_rss",
    "fetch_wikipedia_events",
    "fetch_youtube",
    "get",
    "links_for",
]

# A `[[source]]` with no `kind` is a feed: that is what every one of them was
# before the field existed.
DEFAULT_KIND = "rss"

TYPES: dict[str, SourceType] = {
    t.kind: t for t in (
        RssSource(), HackerNewsSource(), WikipediaEventsSource(), YouTubeSource(),
    )
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


async def extract(
    client: httpx.AsyncClient, item: RawItem
) -> _extract.Article | None:
    """The body of one freshly gathered item, the way its type gets it.

    Most types have no say and the page is fetched and read with trafilatura.
    A type that defines `extract` answers itself — YouTube from the feed's
    description, with no page fetched at all.
    """
    source_type = get(item.kind)
    own = getattr(source_type, "extract", None)
    if own is not None:
        return await own(client, item)
    return await _extract.extract(client, item.url, item.title, item.source)


def _presenting(item: Any) -> tuple[SourceType, str, dict[str, Any]]:
    """The type that presents an article, with the fields it reads. A kind
    nobody registered — a snapshot written by a build that had a type this
    one does not — degrades to the feed's type rather than to nothing."""
    kind = getattr(item, "kind", "") or DEFAULT_KIND
    return (
        get(kind) or TYPES[DEFAULT_KIND],
        getattr(item, "url", "") or "",
        getattr(item, "extra", None) or {},
    )


def embed_for(item: Any) -> str:
    """What the article page frames instead of a photograph — a video's
    player — or "" for an article that has nothing to play."""
    source_type, url, extra = _presenting(item)
    embed = getattr(source_type, "embed", None)
    return embed(url, extra) if embed is not None else ""


def links_for(item: Any) -> list[Link]:
    """The byline's links for an article — an `edition.Item`, an
    `ArticleRow`, or anything else carrying `url`, `source`, `kind` and
    `extra`.

    A kind nobody registered degrades to the feed's single link rather than
    to a byline with nothing to click.
    """
    source_type, url, extra = _presenting(item)
    return source_type.links(url, getattr(item, "source", "") or "", extra)
