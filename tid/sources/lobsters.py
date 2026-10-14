"""`kind = "lobsters"`: Lobsters (lobste.rs), top stories by score, via the
site's JSON listing.

Lobsters is Hacker News with a smaller room: a story is a link to somewhere
on the web plus a discussion of it, tagged and voted on. The record keeps
the story's short id, so the byline offers both — the discussion first, then
the site — exactly as an HN story does. A text post links nowhere else, and
gets the one chip.

There is no search API. `/newest/page/N.json` lists stories newest-first,
twenty-five to a page, and a page covers roughly a day; the fetch walks pages
until it is past the window, then keeps what clears `min_points`. Scores run
an order of magnitude below HN's — a story in the twenties has done well.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..icons import domain_for
from .base import Link, RawItem, clean_title

LOBSTERS_HOST = "lobste.rs"
_NEWEST = f"https://{LOBSTERS_HOST}/newest/page/{{page}}.json"
# A page is about a day, so this bounds the window at a fortnight or so;
# far past any `since_hours` worth setting.
_MAX_PAGES = 15


def story_url(short_id: str) -> str:
    """The discussion page for a story. The site also serves a slugged form,
    `/s/<id>/<slug>`, which is what `comments_url` carries; the bare one
    redirects to it and never goes stale."""
    return f"https://{LOBSTERS_HOST}/s/{short_id}"


def _created(story: dict[str, Any]) -> datetime | None:
    raw = story.get("created_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def fetch_lobsters(
    client: httpx.AsyncClient,
    source_name: str = "Lobsters",
    since_hours: int = 48,
    min_points: int = 10,
) -> list[RawItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    stories: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        r = await client.get(_NEWEST.format(page=page), timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        stories.extend(batch)
        oldest = [c for c in (_created(s) for s in batch) if c is not None]
        if not oldest or min(oldest) < cutoff:
            break

    out: list[RawItem] = []
    for s in stories:
        title = clean_title(s.get("title"))
        short_id = str(s.get("short_id") or "")
        score = s.get("score")
        created = _created(s)
        if not title or not short_id or not isinstance(score, int):
            continue
        if created is not None and created < cutoff:
            continue
        if score < min_points:
            continue
        # A text post has no URL of its own; the discussion is the article.
        url = s.get("url") or story_url(short_id)
        extra: dict[str, Any] = {"short_id": short_id, "points": score}
        if isinstance(s.get("comment_count"), int):
            extra["comments"] = s["comment_count"]
        tags = s.get("tags")
        if isinstance(tags, list) and tags:
            extra["tags"] = [str(t) for t in tags]
        out.append(RawItem(
            source=source_name, url=url, title=title, kind="lobsters",
            surfaced=created.date().isoformat() if created else None,
            extra=extra,
        ))
    out.sort(key=lambda i: i.extra["points"], reverse=True)
    return out


class LobstersSource:
    kind = "lobsters"
    label = "Lobsters, via its JSON listing"

    async def fetch(
        self, client: httpx.AsyncClient, src: dict[str, Any]
    ) -> list[RawItem]:
        return await fetch_lobsters(
            client,
            source_name=src["name"],
            since_hours=int(src.get("since_hours", 48)),
            min_points=int(src.get("min_points", 10)),
        )

    def links(self, url: str, source: str, extra: dict[str, Any]) -> list[Link]:
        short_id = str(extra.get("short_id") or "")
        domain = domain_for(url)
        comments = extra.get("comments")
        title = (
            f"{comments} comment{'' if comments == 1 else 's'} on Lobsters"
            if isinstance(comments, int) else "The discussion on Lobsters"
        )
        if not short_id:
            return [Link(label=source, href=url, role="article")]
        if not domain or domain == LOBSTERS_HOST:
            href = url if domain == LOBSTERS_HOST else story_url(short_id)
            return [Link(label=source, href=href, role="discussion", title=title)]
        return [
            Link(label=source, href=story_url(short_id), role="discussion",
                 title=title),
            Link(label=domain, href=url, role="article",
                 title=f"The story, at {domain}"),
        ]
