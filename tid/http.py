"""HTTP client construction.

The client is *caller-owned*: nothing in this package creates a module-level
client at import time, because a downstream library consumer needs to control
connection pooling, proxies and lifetime itself. Library functions that do
network I/O take an `httpx.AsyncClient` as their first argument.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

USER_AGENT = (
    "tid/0.1 (+https://github.com/mdrachuk/theinternetdaily) "
    "python-httpx"
)

# Feeds and article pages are slow tails; the per-request timeout is what
# keeps one dead host from stalling a gather.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=60.0)

# Per-host cap keeps us from hammering a single feed's origin while the global
# cap bounds total sockets during a wide fan-out.
DEFAULT_LIMITS = httpx.Limits(
    max_connections=32,
    max_keepalive_connections=16,
)


def new_client(
    *,
    timeout: httpx.Timeout | float | None = None,
    limits: httpx.Limits | None = None,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """Build the shared client. The caller owns it and must close it."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    return httpx.AsyncClient(
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        limits=limits or DEFAULT_LIMITS,
        headers=hdrs,
        follow_redirects=follow_redirects,
    )


@asynccontextmanager
async def client_context(**kwargs) -> AsyncIterator[httpx.AsyncClient]:
    """`async with client_context() as client:` for scripts and the CLI."""
    client = new_client(**kwargs)
    try:
        yield client
    finally:
        await client.aclose()
