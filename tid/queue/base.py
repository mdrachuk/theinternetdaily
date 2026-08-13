"""Job queue protocol.

The pipeline enqueues named jobs; how they run is the backend's business. The
default (`tid.queue.local`) runs them in-process on the event loop under
a semaphore — a single-user box does not need Redis. `tid.queue.arq_queue`
puts them on Redis for durable, cross-process execution, which is what a
downstream multi-user service and a shared GPU actually need.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

# A job is an async callable registered under a name.
JobFn = Callable[..., Awaitable[Any]]


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(
        self, job: str, *args: Any, job_id: str | None = None
    ) -> None:
        """Schedule `job` with `args`.

        `job_id` is an idempotency key: enqueueing the same id twice while the
        first is still pending or running must not run it twice.
        """
        ...

    async def close(self) -> None: ...
