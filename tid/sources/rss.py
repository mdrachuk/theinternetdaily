"""`kind = "rss"`: any Atom or RSS feed, parsed by feedparser.

The plain case, and the default. One link per article — the source, pointing
at the piece — and nothing beyond the standard fields on the record.
"""
from __future__ import annotations

import asyncio
import calendar
import html
import re
import time
from typing import Any

import feedparser
import httpx

from .base import Link, RawItem, clean_title

# Feeds advertise the article image in three different places, none of them
# required, and a fair number inline it in the summary HTML instead.
_IMG_IN_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)', re.I)


def _media_width(media: dict) -> int:
    """The pixel width a media element advertises, or 0 when it names none."""
    try:
        return int(str(media.get("width") or "").strip() or 0)
    except ValueError:
        return 0


def entry_image(entry) -> str | None:
    """The best image URL a feed entry offers, or None.

    A feed that publishes one photograph at several sizes lists the smallest
    first — the Guardian advertises 140, 460 and 700px variants of the same
    picture — so taking the first one printed a thumbnail. The widest
    advertised variant wins instead; feed order only decides between elements
    that name no width at all.
    """
    best: str | None = None
    best_width = -1
    for media in list(getattr(entry, "media_thumbnail", None) or ()) \
            + list(getattr(entry, "media_content", None) or ()):
        if not isinstance(media, dict) or not media.get("url"):
            continue
        if str(media.get("medium", "image")) != "image":
            continue
        width = _media_width(media)
        if width > best_width:
            best, best_width = media["url"], width
    if best:
        return best
    for enc in getattr(entry, "enclosures", None) or ():
        href = enc.get("href") if isinstance(enc, dict) else None
        if href and str(enc.get("type", "")).startswith("image/"):
            return href
    # Last resort: the first <img> in the entry's own HTML.
    blobs = [getattr(entry, "summary", "") or ""]
    blobs += [c.get("value", "") for c in getattr(entry, "content", None) or ()
              if isinstance(c, dict)]
    for blob in blobs:
        m = _IMG_IN_HTML.search(blob)
        if m:
            return html.unescape(m.group(1))
    return None


async def fetch_rss(
    client: httpx.AsyncClient,
    source_name: str,
    feed_url: str,
    since_hours: int | None = None,
) -> list[RawItem]:
    """Every entry the feed is currently carrying, oldest bound by
    `since_hours` if the source sets one. No count cap: what the feed offers
    is what we take, and `Store.exists` keeps a re-gather from doing the work
    twice."""
    cutoff = time.time() - since_hours * 3600 if since_hours is not None else None
    r = await client.get(feed_url)
    r.raise_for_status()
    # feedparser.parse(url) would do its own blocking fetch; hand it bytes and
    # keep its (CPU-bound, sometimes slow) parse off the event loop.
    d = await asyncio.to_thread(feedparser.parse, r.content)
    out: list[RawItem] = []
    for entry in d.entries:
        url = getattr(entry, "link", None)
        title = clean_title(getattr(entry, "title", None))
        if not url or not title:
            continue
        parsed = (
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        if cutoff is not None and parsed is not None:
            if calendar.timegm(parsed) < cutoff:
                continue
        surfaced = time.strftime("%Y-%m-%d", parsed) if parsed else None
        out.append(RawItem(
            source=source_name, url=url, title=title, kind="rss",
            surfaced=surfaced, image=entry_image(entry),
        ))
    return out


class RssSource:
    kind = "rss"
    label = "RSS / Atom feed"

    async def fetch(
        self, client: httpx.AsyncClient, src: dict[str, Any]
    ) -> list[RawItem]:
        since_hours = src.get("since_hours")
        return await fetch_rss(
            client, src["name"], src["url"],
            since_hours=int(since_hours) if since_hours is not None else None,
        )

    def links(self, url: str, source: str, extra: dict[str, Any]) -> list[Link]:
        return [Link(label=source, href=url, role="article")]
