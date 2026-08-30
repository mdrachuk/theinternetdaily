from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import trafilatura
from trafilatura.metadata import extract_metadata


@dataclass
class Article:
    source: str
    url: str
    title: str
    text: str
    published: str | None = None  # ISO date from page metadata, may be None
    image: str | None = None      # og:image from page metadata, may be None


async def fetch_html(client: httpx.AsyncClient, url: str) -> str | None:
    """GET a page and return its decoded body, or None if it isn't usable
    HTML. Replaces `trafilatura.fetch_url`, which does its own blocking
    request with its own connection pool."""
    try:
        r = await client.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    ctype = r.headers.get("content-type", "")
    if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
        return None
    return r.text or None


def _parse(html: str, source: str, url: str, title: str) -> Article | None:
    """Blocking half of `extract` — runs in a worker thread."""
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text or len(text) < 200:
        return None
    published: str | None = None
    image: str | None = None
    try:
        md = extract_metadata(html)
        if md and md.date:
            published = md.date  # trafilatura returns "YYYY-MM-DD"
        # og:image / twitter:image. Only absolute URLs are usable: the page is
        # rendered on our own origin, so a site-relative path would 404.
        if md and getattr(md, "image", None):
            candidate = str(md.image)
            if candidate.startswith(("http://", "https://")):
                image = candidate
    except Exception:
        pass
    return Article(source=source, url=url, title=title, text=text,
                   published=published, image=image)


async def extract(
    client: httpx.AsyncClient, url: str, title: str, source: str
) -> Article | None:
    html = await fetch_html(client, url)
    if not html:
        return None
    # trafilatura's extraction is pure CPU (lxml + heuristics) and takes tens
    # to hundreds of milliseconds per page — long enough to stall the loop.
    return await asyncio.to_thread(_parse, html, source, url, title)
