"""`kind = "youtube"`: a channel's uploads, via the Atom feed YouTube still
publishes for every channel.

A video is not a page to be read, and treating it as one fails twice over: the
watch page is a script bundle trafilatura gets nothing from, and the caption
track — the only text that is actually the video — is served by an endpoint
that refuses cloud addresses outright. So the type does not extract. The feed
carries a description with every upload, and that is the body: enough for the
summary to say what the video is about, and printed on the article page under
the player for the reader who wants it before pressing play.

Configured by the channel's public URL — `https://www.youtube.com/@Fireship`
— because that is what a person has to hand. The feed wants the channel id,
which the channel page names in its canonical link; it is resolved once per
process and remembered.
"""
from __future__ import annotations

import asyncio
import calendar
import re
import time
from typing import Any

import feedparser
import httpx

from ..extract import Article
from .base import Link, RawItem, clean_title

_FEED = "https://www.youtube.com/feeds/videos.xml"
_CANONICAL = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/([^"/?]+)"'
)
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")

# From a European address the channel page is a redirect to a consent form,
# which names no channel. The `SOCS` cookie is the form already answered and
# `ucbcb=1` is the same answer as a query flag; either alone gets the page,
# both are sent.
_CONSENTED = {"Cookie": "SOCS=CAI", "Accept-Language": "en"}
_CONSENT_FLAG = {"ucbcb": "1"}

_resolved: dict[str, str] = {}


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def embed_url(video_id: str) -> str:
    """The player, on the cookieless host: nothing is set until play is
    pressed, which is what lets the article page embed it at all."""
    return f"https://www.youtube-nocookie.com/embed/{video_id}"


def thumbnail_url(video_id: str, size: str = "hqdefault") -> str:
    return f"https://i.ytimg.com/vi/{video_id}/{size}.jpg"


async def resolve_channel_id(client: httpx.AsyncClient, url: str) -> str:
    """The `UC…` id behind a channel URL — a handle, a /channel/ link or a
    bare id all work. Fetched once; a channel does not change its id."""
    url = url.strip().rstrip("/")
    if _CHANNEL_ID.match(url):
        return url
    tail = url.rsplit("/", 1)[-1]
    if "/channel/" in url and _CHANNEL_ID.match(tail):
        return tail
    if url in _resolved:
        return _resolved[url]
    r = await client.get(
        url, params=_CONSENT_FLAG, headers=_CONSENTED, follow_redirects=True,
    )
    r.raise_for_status()
    m = _CANONICAL.search(r.text)
    if not m:
        raise ValueError(f"no channel id on {url}")
    _resolved[url] = m.group(1)
    return m.group(1)


async def fetch_youtube(
    client: httpx.AsyncClient,
    source_name: str,
    channel: str,
    since_hours: int | None = None,
) -> list[RawItem]:
    """Every upload the channel's feed is carrying — the last fifteen — bound
    by `since_hours` if the source sets one."""
    channel_id = await resolve_channel_id(client, channel)
    cutoff = time.time() - since_hours * 3600 if since_hours is not None else None
    r = await client.get(_FEED, params={"channel_id": channel_id})
    r.raise_for_status()
    d = await asyncio.to_thread(feedparser.parse, r.content)
    out: list[RawItem] = []
    for entry in d.entries:
        video_id = str(getattr(entry, "yt_videoid", "") or "")
        title = clean_title(getattr(entry, "title", None))
        if not video_id or not title:
            continue
        parsed = getattr(entry, "published_parsed", None)
        if cutoff is not None and parsed is not None:
            if calendar.timegm(parsed) < cutoff:
                continue
        extra: dict[str, Any] = {"video_id": video_id, "channel_id": channel_id}
        stats = getattr(entry, "media_statistics", None)
        try:
            extra["views"] = int(str((stats or {}).get("views", "")).strip())
        except ValueError:
            pass
        out.append(RawItem(
            source=source_name, url=video_url(video_id), title=title,
            kind="youtube",
            surfaced=time.strftime("%Y-%m-%d", parsed) if parsed else None,
            # No image here, though the feed names one: the gather lets a
            # feed's picture win over an extracted one, and the feed's is the
            # letterboxed 4:3. `extract` supplies the still.
            extra=extra,
            # The feed's own words for the video: the body, with no page to
            # read it out of.
            text=(getattr(entry, "summary", "") or "").strip(),
        ))
    return out


async def best_thumbnail(client: httpx.AsyncClient, video_id: str) -> str:
    """The widescreen still if YouTube has rendered one, else the letterboxed
    4:3 it always has. Only the former is worth a photograph's place on the
    front page; the latter prints black bars above and below the frame."""
    wide = thumbnail_url(video_id, "maxresdefault")
    try:
        r = await client.head(wide)
        if r.status_code == 200:
            return wide
    except httpx.HTTPError:
        pass
    return thumbnail_url(video_id)


class YouTubeSource:
    kind = "youtube"
    label = "YouTube channel feed"

    async def fetch(
        self, client: httpx.AsyncClient, src: dict[str, Any]
    ) -> list[RawItem]:
        since_hours = src.get("since_hours")
        return await fetch_youtube(
            client, src["name"], src.get("url") or src.get("channel_id", ""),
            since_hours=int(since_hours) if since_hours is not None else None,
        )

    async def extract(
        self, client: httpx.AsyncClient, item: RawItem
    ) -> Article | None:
        """No page is read. The description the feed gave is the body, and
        the one request made is for the wider still."""
        video_id = str(item.extra.get("video_id") or "")
        return Article(
            source=item.source, url=item.url, title=item.title,
            text=item.text or "",
            published=item.surfaced,
            image=await best_thumbnail(client, video_id) if video_id else None,
        )

    def links(self, url: str, source: str, extra: dict[str, Any]) -> list[Link]:
        views = extra.get("views")
        title = (f"{views:,} views on YouTube" if isinstance(views, int)
                 else "The video, on YouTube")
        return [Link(label=source, href=url, role="article", title=title)]

    def embed(self, url: str, extra: dict[str, Any]) -> str:
        video_id = str(extra.get("video_id") or "")
        return embed_url(video_id) if video_id else ""
