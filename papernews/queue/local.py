"""In-process job queue — the default. No Redis, no extra processes.

Jobs run as asyncio tasks under a semaphore, so `concurrency` bounds how many
run at once. That is enough for a single-user box; it is *not* enough to bound
a GPU shared by several processes, which is why the arq backend exists (see
plan step 8).
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from .base import JobFn


class LocalQueue:
    """Structural conformance to queue.base.JobQueue."""

    def __init__(self, jobs: dict[str, JobFn] | None = None, concurrency: int = 4):
        self.jobs: dict[str, JobFn] = dict(jobs or {})
        self._sem = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()
        # In-flight idempotency keys. A durable queue would persist these;
        # in-process, "pending or running" is all there is.
        self._inflight: set[str] = set()

    def register(self, name: str, fn: JobFn) -> None:
        self.jobs[name] = fn

    async def enqueue(
        self, job: str, *args: Any, job_id: str | None = None
    ) -> None:
        fn = self.jobs.get(job)
        if fn is None:
            raise KeyError(f"no such job: {job!r}")
        key = job_id or job
        if key in self._inflight:
            return  # already pending or running
        self._inflight.add(key)
        task = asyncio.create_task(self._run(fn, key, args), name=f"job:{key}")
        # Hold a reference: a bare create_task result can be garbage collected
        # mid-flight, which cancels the job.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, fn: JobFn, key: str, args: tuple) -> None:
        try:
            async with self._sem:
                await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sys.stderr.write(f"[queue] job {key} failed: {e}\n")
            sys.stderr.flush()
        finally:
            self._inflight.discard(key)

    def pending(self) -> int:
        return len(self._inflight)

    async def drain(self) -> None:
        """Wait for everything currently queued to finish. Used by the CLI and
        by tests; a long-running server never needs it."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()
