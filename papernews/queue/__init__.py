"""Job queues: a protocol, an in-process default, and an optional arq backend.

    from papernews.queue import LocalQueue
    q = LocalQueue({"ingest": do_ingest}, concurrency=1)
    await q.enqueue("ingest", job_id="ingest")
"""
from __future__ import annotations

from .base import JobFn, JobQueue
from .local import LocalQueue

__all__ = ["JobFn", "JobQueue", "LocalQueue", "open_queue"]


def open_queue(url: str | None = None, jobs: dict[str, JobFn] | None = None,
               concurrency: int = 4) -> JobQueue:
    """Build a queue from a URL. `None` or "local" gives the in-process one;
    a `redis://` URL gives the arq-backed one (needs `papernews[redis]`)."""
    if not url or url == "local":
        return LocalQueue(jobs, concurrency=concurrency)
    if url.startswith("redis://") or url.startswith("rediss://"):
        from .arq_queue import ArqQueue

        return ArqQueue(redis_url=url)
    raise ValueError(f"unsupported queue url: {url!r}")
