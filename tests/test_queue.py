"""Job queue protocol behaviour, on the in-process default.

The arq backend is not exercised here — it needs Redis, and its job semantics
are arq's. What has to hold for *both* is in this file: name lookup,
idempotency by job id, bounded concurrency, and a failing job not taking the
process (or the queue) down with it.
"""
from __future__ import annotations

import asyncio

import pytest

from tid.queue import LocalQueue, open_queue


async def test_enqueue_runs_the_registered_job():
    seen: list[str] = []

    async def job(arg):
        seen.append(arg)

    q = LocalQueue({"work": job})
    await q.enqueue("work", "hello")
    await q.drain()
    assert seen == ["hello"]


async def test_unknown_job_is_an_error_not_a_silent_drop():
    q = LocalQueue({})
    with pytest.raises(KeyError):
        await q.enqueue("nope")


async def test_same_job_id_does_not_run_twice_while_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def job():
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()

    q = LocalQueue({"ingest": job})
    await q.enqueue("ingest", job_id="ingest")
    await started.wait()
    await q.enqueue("ingest", job_id="ingest")  # dropped: already running
    release.set()
    await q.drain()
    assert runs == 1


async def test_the_id_frees_up_once_the_job_finishes():
    runs = 0

    async def job():
        nonlocal runs
        runs += 1

    q = LocalQueue({"ingest": job})
    await q.enqueue("ingest", job_id="ingest")
    await q.drain()
    await q.enqueue("ingest", job_id="ingest")
    await q.drain()
    assert runs == 2


async def test_concurrency_is_bounded():
    """The GPU is the reason this matters: one 70 W card cannot run several
    LLM jobs at once."""
    inflight = 0
    peak = 0

    async def job(_):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1

    q = LocalQueue({"work": job}, concurrency=2)
    for i in range(6):
        await q.enqueue("work", i, job_id=f"work-{i}")
    await q.drain()
    assert peak == 2


async def test_a_failing_job_does_not_break_the_queue(capsys):
    ran: list[str] = []

    async def boom():
        raise RuntimeError("nope")

    async def fine():
        ran.append("fine")

    q = LocalQueue({"boom": boom, "fine": fine})
    await q.enqueue("boom", job_id="a")
    await q.drain()
    await q.enqueue("fine", job_id="b")
    await q.drain()
    assert ran == ["fine"]
    assert "job a failed" in capsys.readouterr().err


async def test_close_cancels_outstanding_work():
    async def slow():
        await asyncio.sleep(30)

    q = LocalQueue({"slow": slow})
    await q.enqueue("slow", job_id="s")
    await asyncio.sleep(0)  # let it start
    await q.close()
    assert q.pending() == 0


def test_open_queue_defaults_to_local():
    assert isinstance(open_queue(), LocalQueue)
    assert isinstance(open_queue("local"), LocalQueue)


def test_open_queue_rejects_an_unknown_url():
    with pytest.raises(ValueError):
        open_queue("kafka://localhost")


def test_open_queue_builds_the_arq_backend_for_redis_urls():
    pytest.importorskip("arq", reason="needs tid[redis]")
    from tid.queue.arq_queue import ArqQueue

    q = open_queue("redis://localhost:6379")
    assert isinstance(q, ArqQueue)
