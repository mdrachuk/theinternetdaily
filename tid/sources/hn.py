"""`kind = "hn"`: Hacker News, top stories by points, via the Algolia API.

An HN story is two things at once: a link to something on the web, and a
discussion of it on news.ycombinator.com. The record keeps the item id so
both survive, and the byline prints both — the discussion first, because
that is what "Hacker News" means to the reader, then the site it points at.
An Ask HN or Show HN thread links nowhere else, and gets the one chip.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..icons import domain_for
from .base import Link, RawItem, clean_title

# Algolia HN search. Returns stories matching the numericFilters, ranked by
# popularity. We then re-sort by points; `since_hours` and `min_points` are
# the only bounds — every story that clears them is a story for the paper.
_HN_SEARCH = "https://hn.algolia.com/api/v1/search"

HN_HOST = "news.ycombinator.com"


def item_url(hn_id: str) -> str:
    """The discussion page for an HN item."""
    return f"https://{HN_HOST}/item?id={hn_id}"


async def fetch_hn(
    client: httpx.AsyncClient,
    source_name: str = "Hacker News",
    since_hours: int = 48,
    min_points: int = 50,
) -> list[RawItem]:
    since = int(time.time() - since_hours * 3600)
    params = {
        "tags": "story",
        # Algolia wants multiple numeric filters as a JSON-encoded array.
        # Passing a bare Python list makes httpx emit repeated
        # `numericFilters=` params, of which Algolia honours only the first —
        # which silently drops the min_points gate.
        "numericFilters": json.dumps(
            [f"created_at_i>{since}", f"points>{min_points}"]
        ),
        "hitsPerPage": 100,
    }
    r = await client.get(_HN_SEARCH, params=params, timeout=15)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    hits.sort(key=lambda h: h.get("points", 0), reverse=True)

    out: list[RawItem] = []
    for h in hits:
        title = clean_title(h.get("title"))
        hn_id = str(h.get("objectID") or "")
        if not title or not hn_id:
            continue
        # A self post (Ask HN, Show HN with no link) has no URL of its own;
        # the discussion *is* the article.
        url = h.get("url") or item_url(hn_id)
        ts = h.get("created_at_i")
        surfaced = (
            datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            if ts else None
        )
        extra: dict[str, Any] = {"hn_id": hn_id}
        for key, name in (("points", "points"), ("num_comments", "comments")):
            if isinstance(h.get(key), int):
                extra[name] = h[key]
        out.append(RawItem(
            source=source_name, url=url, title=title, kind="hn",
            surfaced=surfaced, extra=extra,
        ))
    return out


class HackerNewsSource:
    kind = "hn"
    label = "Hacker News, via Algolia"

    async def fetch(
        self, client: httpx.AsyncClient, src: dict[str, Any]
    ) -> list[RawItem]:
        return await fetch_hn(
            client,
            source_name=src["name"],
            since_hours=int(src.get("since_hours", 48)),
            min_points=int(src.get("min_points", 50)),
        )

    def links(self, url: str, source: str, extra: dict[str, Any]) -> list[Link]:
        hn_id = str(extra.get("hn_id") or "")
        domain = domain_for(url)
        comments = extra.get("comments")
        title = (
            f"{comments} comment{'' if comments == 1 else 's'} on Hacker News"
            if isinstance(comments, int) else "The discussion on Hacker News"
        )
        if not hn_id:
            # A row from before the id was recorded: nothing to point the
            # discussion chip at but the link itself, which is what the page
            # always did. The next re-gather back-fills the id.
            return [Link(label=source, href=url, role="article")]
        if not domain or domain == HN_HOST:
            # A self post, or a link with no host to name a chip after: the
            # discussion is the one place to go.
            href = url if domain == HN_HOST else item_url(hn_id)
            return [Link(label=source, href=href, role="discussion", title=title)]
        return [
            Link(label=source, href=item_url(hn_id), role="discussion",
                 title=title),
            Link(label=domain, href=url, role="article",
                 title=f"The story, at {domain}"),
        ]
