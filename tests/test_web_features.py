"""Tests for the changes that addressed issue #1, ported to FastAPI.

These tests intentionally avoid touching the network, the LLM SDKs, or
xelatex. They cover the four user-visible features added in that issue:

    1. INGEST_SCHEDULE cron-style scheduling
    2. INGEST_INTERVAL_SECONDS fallback when no schedule is set
    3. GET /ingest returns a 405 with a helpful JSON hint
    4. POST_INGEST_HOOK fires after a successful ingest

The hook's argument used to be the built PDF. Editions are pages now, so it
receives the edition snapshot — the same content, as JSON.

The app is driven through httpx.ASGITransport, which does not run the
lifespan — so importing or exercising the app never starts a scheduler.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest import mock

import httpx
import pytest

import tid.edition as ed
import tid.jobs as jobs
import tid.web as web


async def _noop() -> None:
    return None


@pytest.fixture
def clean_env():
    keys = (
        "INGEST_SCHEDULE", "INGEST_TIMEZONE", "INGEST_INTERVAL_SECONDS",
        "POST_INGEST_HOOK", "POST_INGEST_HOOK_TIMEOUT",
    )
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
async def client():
    app = web.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


# --- 1 & 2: scheduler modes -----------------------------------------------

async def test_cron_schedule_creates_one_job_per_time(clean_env):
    os.environ["INGEST_SCHEDULE"] = "07:00,18:30"
    os.environ["INGEST_TIMEZONE"] = "Europe/London"
    sched = web.start_scheduler(_noop)
    try:
        jobs = sched.get_jobs()
        assert len(jobs) == 2
        triggers = [str(j.trigger) for j in jobs]
        assert any("hour='7'" in t and "minute='0'" in t for t in triggers), triggers
        assert any("hour='18'" in t and "minute='30'" in t for t in triggers), triggers
        tzs = {str(j.trigger.timezone) for j in jobs}
        assert any("Europe/London" in z for z in tzs), tzs
    finally:
        sched.shutdown(wait=False)


async def test_cron_ignores_malformed_entries_but_keeps_valid_ones(clean_env):
    os.environ["INGEST_SCHEDULE"] = "07:00,not-a-time,18:00"
    sched = web.start_scheduler(_noop)
    try:
        assert len(sched.get_jobs()) == 2, "malformed entry must be skipped"
    finally:
        sched.shutdown(wait=False)


async def test_interval_fallback_when_no_schedule(clean_env):
    os.environ["INGEST_INTERVAL_SECONDS"] = "60"
    sched = web.start_scheduler(_noop)
    try:
        jobs = sched.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "ingest"
        assert "interval[" in str(jobs[0].trigger)
    finally:
        sched.shutdown(wait=False)


# --- 3: GET /ingest helper -------------------------------------------------

async def test_get_ingest_returns_helpful_405(client):
    r = await client.get("/ingest")
    assert r.status_code == 405
    body = r.json()
    assert "POST" in body["error"]
    assert "curl" in body["hint"].lower()


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


# --- 4: POST_INGEST_HOOK ---------------------------------------------------

@pytest.fixture
def hook_env(tmp_path: Path, monkeypatch):
    """A cache dir with one snapshot in it, plus a hook that records its argv."""
    monkeypatch.setenv("TID_CACHE", str(tmp_path))
    snapshot = tmp_path / "k.json"
    snapshot.write_text('{"key": "k", "items": []}')
    hook_log = tmp_path / "hook.log"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/usr/bin/env bash\necho "$1" > "{hook_log}"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return snapshot, hook, hook_log, tmp_path


def _stubbed_ingest():
    """Bypass the real ingest work: no network, no LLM, no store."""
    # One article in it, so the edition counts as published and the hook fires.
    built = ed.build(
        [{"id": "1", "title": "T", "url": "https://x.test/1", "source": "X",
          "section": "S", "summary": "d", "text": "body"}],
        key="k", date="2026-08-30", built_at="2026-08-30T06:00:00+00:00",
    )
    return (
        mock.patch.object(jobs, "cmd_ingest", new=mock.AsyncMock(return_value=0)),
        mock.patch.object(
            jobs, "build_edition_for_key", new=mock.AsyncMock(return_value=built)
        ),
        mock.patch.object(jobs, "warm_icons", new=mock.AsyncMock(return_value=0)),
        mock.patch.object(jobs.config, "load_sources", return_value=[]),
        mock.patch.object(jobs.config, "load_topics", return_value=[]),
        mock.patch.object(jobs, "open_store", return_value=mock.AsyncMock()),
        mock.patch.object(jobs, "current_key", new=mock.AsyncMock(return_value="k")),
    )


async def test_interval_job_is_actually_scheduled(clean_env):
    """Regression: `next_run_time=None` is APScheduler's *paused* marker, so
    passing it left the interval ingest sitting there forever."""
    os.environ["INGEST_INTERVAL_SECONDS"] = "3600"
    sched = web.start_scheduler(_noop)
    try:
        (job,) = sched.get_jobs()
        assert job.next_run_time is not None, "interval ingest never fires"
    finally:
        sched.shutdown(wait=False)


async def test_ingest_builds_the_edition_without_a_hook(clean_env, hook_env):
    """The archive must gain a row per ingest, visitor or no visitor."""
    os.environ.pop("POST_INGEST_HOOK", None)

    patches = _stubbed_ingest()
    for p in patches:
        p.start()
    try:
        await jobs.ingest()
        jobs.build_edition_for_key.assert_awaited()
    finally:
        for p in patches:
            p.stop()


async def test_failed_build_does_not_break_the_ingest(clean_env, hook_env):
    os.environ.pop("POST_INGEST_HOOK", None)

    patches = _stubbed_ingest()
    for p in patches:
        p.start()
    boom = mock.patch.object(
        jobs, "build_edition_for_key",
        new=mock.AsyncMock(side_effect=RuntimeError("the store went away")),
    )
    boom.start()
    try:
        await jobs.ingest()  # must not raise
    finally:
        boom.stop()
        for p in patches:
            p.stop()


async def test_hook_runs_with_snapshot_path_after_successful_ingest(
    clean_env, hook_env
):
    snapshot, hook, hook_log, _ = hook_env
    os.environ["POST_INGEST_HOOK"] = str(hook)

    patches = _stubbed_ingest()
    for p in patches:
        p.start()
    try:
        await jobs.ingest()
    finally:
        for p in patches:
            p.stop()

    assert hook_log.exists(), "hook script did not run"
    assert hook_log.read_text().strip() == str(snapshot)


async def test_hook_failure_does_not_propagate(clean_env, hook_env):
    _, _, _, tmp_path = hook_env
    bad_hook = tmp_path / "bad.sh"
    bad_hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    bad_hook.chmod(bad_hook.stat().st_mode | stat.S_IEXEC)
    os.environ["POST_INGEST_HOOK"] = str(bad_hook)

    patches = _stubbed_ingest()
    for p in patches:
        p.start()
    try:
        await jobs.ingest()  # must not raise
    finally:
        for p in patches:
            p.stop()


async def test_no_hook_means_no_subprocess(clean_env, hook_env):
    os.environ.pop("POST_INGEST_HOOK", None)

    patches = _stubbed_ingest()
    run_hook = mock.patch.object(jobs, "run_hook", new=mock.AsyncMock())
    for p in patches:
        p.start()
    mocked = run_hook.start()
    try:
        await jobs.ingest()
        mocked.assert_not_awaited()
    finally:
        run_hook.stop()
        for p in patches:
            p.stop()


# --- the hourly prepare job ------------------------------------------------
#
# The store is filled every hour and the site must not notice: an article is
# news when an edition is assembled, not when a gather happens to land.

async def test_prepare_is_scheduled_hourly_by_default(clean_env, monkeypatch):
    monkeypatch.delenv("PREPARE_INTERVAL_SECONDS", raising=False)
    sched = web.start_scheduler(_noop, _noop)
    try:
        by_id = {j.id: j for j in sched.get_jobs()}
        assert set(by_id) == {"ingest", "prepare"}
        assert "interval[1:00:00]" in str(by_id["prepare"].trigger)
        assert by_id["prepare"].next_run_time is not None
    finally:
        sched.shutdown(wait=False)


async def test_prepare_can_be_turned_off(clean_env, monkeypatch):
    monkeypatch.setenv("PREPARE_INTERVAL_SECONDS", "0")
    sched = web.start_scheduler(_noop, _noop)
    try:
        assert [j.id for j in sched.get_jobs()] == ["ingest"]
    finally:
        sched.shutdown(wait=False)


async def test_prepare_fills_the_store_and_publishes_nothing(clean_env, hook_env):
    """No topics, no snapshot, no hook — the store is the only thing that
    moves. Otherwise the front page would gain an edition every hour."""
    _, hook, hook_log, _ = hook_env
    os.environ["POST_INGEST_HOOK"] = str(hook)
    patches = _stubbed_ingest()
    prepare = mock.patch.object(
        jobs, "cmd_prepare", new=mock.AsyncMock(return_value=0)
    )
    for p in patches:
        p.start()
    prepared = prepare.start()
    try:
        await jobs.prepare()
        prepared.assert_awaited_once()
        jobs.cmd_ingest.assert_not_awaited()
        jobs.build_edition_for_key.assert_not_awaited()
        assert not hook_log.exists(), "the hook is for editions only"
        assert jobs.running() is None
    finally:
        prepare.stop()
        for p in patches:
            p.stop()


async def test_post_prepare_enqueues_the_prepare_job(clean_env):
    class _Queue:
        def __init__(self):
            self.calls = []

        async def enqueue(self, job, *args, job_id=None):
            self.calls.append((job, job_id))

        async def close(self):
            pass

    q = _Queue()
    app = web.create_app(queue=q)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/prepare")
    assert r.status_code == 202 and r.json() == {"status": "started"}
    assert q.calls == [("prepare", "prepare")]
