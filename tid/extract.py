from __future__ import annotations

import asyncio
import concurrent.futures as cf
import multiprocessing as mp
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


class ParserCrashed(RuntimeError):
    """The parser process died on this input — a native fault (lxml, libxml2)
    that Python cannot catch, only survive by not being in the same process."""


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


# --- the parser pool ---------------------------------------------------------
#
# trafilatura's extraction is pure CPU (lxml + heuristics) and takes tens to
# hundreds of milliseconds per page — long enough to stall the event loop, so it
# never runs inline. It used to run in a thread, until one page took the whole
# server down with a `double free or corruption` out of libxml2: a fault in C
# is not an exception, and a thread shares the heap it just corrupted. Parsing
# in a child process turns the same fault into one unreadable article and a
# log line naming the URL.
#
# Two workers is plenty: the gather is network-bound and the parse is a few
# hundred milliseconds. "spawn" rather than "fork" so a worker starts from a
# clean interpreter — forking a process that holds an event loop, an httpx
# pool and a database client is how you get the *next* mysterious crash.

_POOL_WORKERS = 2
_pool: cf.ProcessPoolExecutor | None = None


def _get_pool() -> cf.ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = cf.ProcessPoolExecutor(
            max_workers=_POOL_WORKERS, mp_context=mp.get_context("spawn"),
        )
    return _pool


def _drop_pool() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def in_parser_process(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a picklable, module-level `fn(*args)` in the parser pool.

    A worker dying breaks the whole pool: every parse in flight fails, not
    only the one that hit the fault. So each call gets one retry on a fresh
    pool. An innocent bystander passes the second time; the offending input
    crashes it again and is reported as such. The pool is rebuilt on the
    next call either way.
    """
    loop = asyncio.get_running_loop()
    for attempt in (1, 2):
        try:
            return await loop.run_in_executor(_get_pool(), fn, *args)
        except cf.process.BrokenProcessPool:
            _drop_pool()
            if attempt == 2:
                raise ParserCrashed("parser process crashed on this page")


def _parse(html: str, source: str, url: str, title: str) -> Article | None:
    """Blocking half of `extract` — runs in a parser process."""
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
    try:
        return await in_parser_process(_parse, html, source, url, title)
    except ParserCrashed as e:
        raise ParserCrashed(f"{e}: {url}") from None
