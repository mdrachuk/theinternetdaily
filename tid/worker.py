"""arq worker entry point.

    pip install tid[redis]
    arq tid.worker.WorkerSettings

`max_jobs` is the real concurrency cap. A single-user box does not need this at
all — the default in-process queue is enough — but a shared GPU does: only a
process-level cap keeps vLLM from being oversubscribed (plan step 8).
"""
from __future__ import annotations

import os

from .jobs import JOBS
from .queue.arq_queue import worker_settings

WorkerSettings = worker_settings(
    JOBS,
    redis_url=os.environ.get("TID_QUEUE", "redis://localhost:6379"),
    queue_name=os.environ.get("TID_QUEUE_NAME", "tid"),
    max_jobs=int(os.environ.get("TID_MAX_JOBS", "1")),
    job_timeout=int(os.environ.get("TID_JOB_TIMEOUT", "3600")),
)
