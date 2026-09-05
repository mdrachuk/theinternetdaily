"""What every source type shares: the raw item a fetch hands back, the link a
byline prints, and the protocol a type implements.

A *source type* is not a label on a feed. It decides three things at once —
what a record carries beyond the standard fields, how the items are loaded and
parsed, and how an article is presented on the page. Hacker News is the case
that forced the distinction: a story there is usually a link to somewhere
else, so its record needs the HN item id as well as the URL, its fetch speaks
to Algolia rather than to a feed, and its byline has to offer two places to go
— the discussion and the thing being discussed.

One class per kind, registered in `tid.sources`. The pipeline never asks
`if kind == "hn"`; it asks the type.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


def clean_title(s: str | None) -> str:
    """Decode HTML entities and collapse whitespace in feed-provided titles."""
    if not s:
        return ""
    return " ".join(html.unescape(s).split())


@dataclass
class RawItem:
    """One story as a fetch found it, before extraction."""
    source: str
    url: str
    title: str
    # Which source type produced it — and so which type's `links` present it.
    kind: str = "rss"
    # Date the source surfaced this item (HN submission for HN, feed pub for
    # RSS). Used for window filtering. The article's *own* publication date
    # comes from extract.py via trafilatura metadata.
    surfaced: str | None = None  # ISO date "YYYY-MM-DD" or None
    # Lead image advertised by the feed itself. Cheaper and more reliable than
    # the page's og:image (which extract.py falls back to), because a feed
    # names the image it considers the article's own.
    image: str | None = None
    # The type's own fields, stored on the row as they are. JSON-shaped: the
    # SQLite store keeps them as a JSON column, Mongo as a subdocument.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Link:
    """One place an article's byline can send the reader.

    A written article has one: its source, pointing at the piece. An
    aggregator has two: itself, pointing at the discussion, and the site it
    linked to. `role` names which, so the page can mark the difference
    without knowing the type.
    """
    label: str
    href: str
    role: str = "article"       # "article" | "discussion"
    title: str = ""             # the tooltip; empty means the label suffices


class SourceType(Protocol):
    """One kind of source, from config to page."""

    kind: str
    label: str      # for the subscriptions page: how this kind is fetched

    async def fetch(
        self, client: httpx.AsyncClient, src: dict[str, Any]
    ) -> list[RawItem]:
        """Every item the source currently offers, given its `[[source]]`
        table from sources.toml. Raises on a failed fetch; the gather logs and
        moves on to the next source."""
        ...

    def links(self, url: str, source: str, extra: dict[str, Any]) -> list[Link]:
        """Where the byline sends the reader, in display order. Pure: it works
        from the stored record alone, so an archived edition renders the same
        chips years later without the config."""
        ...
