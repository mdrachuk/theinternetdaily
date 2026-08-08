"""Redis-backed job queue — optional, behind `pip install papernews[redis]`.

Why it exists in a single-user repo: durable jobs survive a restart, and
separate worker processes with their own `max_jobs` are the only thing that
actually bounds LLM concurrency across processes (plan step 8). A semaphore in
one process cannot do that.

Job functions are looked up by name in the same registry the local queue uses,
so the job graph is identical under both backends.
"""
from __future__ import annotations

from typing import Any

from .base import JobFn


class ArqQueue:
    """Structural conformance to queue.base.JobQueue.

    The caller owns the arq pool unless this class created it.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        queue_name: str = "papernews",
        pool: Any | None = None,
    ):
        self.redis_url = redis_url
        self.queue_name = queue_name
        self._pool = pool
        self._owns_pool = pool is None

    async def pool(self) -> Any:
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._pool = await create_pool(
                RedisSettings.from_dsn(self.redis_url),
                default_queue_name=self.queue_name,
            )
        return self._pool

    async def enqueue(
        self, job: str, *args: Any, job_id: str | None = None
    ) -> None:
        pool = await self.pool()
        # arq drops a job whose _job_id is already queued or running, which is
        # exactly the idempotency the protocol asks for.
        await pool.enqueue_job(job, *args, _job_id=job_id)

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.aclose()
            self._pool = None


def worker_settings(
    jobs: dict[str, JobFn],
    redis_url: str = "redis://localhost:6379",
    queue_name: str = "papernews",
    max_jobs: int = 4,
    job_timeout: int = 3600,
) -> type:
    """Build an arq WorkerSettings class for `arq papernews.worker.Settings`.

    `max_jobs` is the real concurrency cap: one worker process per queue with
    max_jobs matched to the backend (e.g. vLLM's --max-num-seqs) keeps the GPU
    from being oversubscribed. `job_timeout` is generous because a 12B model at
    70 W is slow.
    """
    from arq.connections import RedisSettings

    async def _startup(ctx):  # pragma: no cover - exercised by arq itself
        return None

    return type(
        "WorkerSettings",
        (),
        {
            "functions": [
                _named_job(name, fn) for name, fn in jobs.items()
            ],
            "redis_settings": RedisSettings.from_dsn(redis_url),
            "queue_name": queue_name,
            "max_jobs": max_jobs,
            "job_timeout": job_timeout,
            "on_startup": _startup,
        },
    )


def _named_job(name: str, fn: JobFn):
    """Wrap a plain async function as an arq task under `name`.

    arq passes a context dict as the first argument and takes the job name from
    `__qualname__`, so the registry's names have to be stamped on the wrapper.
    """
    from arq import func

    async def _task(ctx, *args):
        return await fn(*args)

    _task.__name__ = name
    _task.__qualname__ = name
    return func(_task, name=name)
