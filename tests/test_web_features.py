"""Tests for the changes that addressed issue #1, ported to FastAPI.

These tests intentionally avoid touching the network, the LLM SDKs, or
xelatex. They cover the four user-visible features added in that issue:

    1. INGEST_SCHEDULE cron-style scheduling
    2. INGEST_INTERVAL_SECONDS fallback when no schedule is set
    3. GET /ingest returns a 405 with a helpful JSON hint
    4. POST_INGEST_HOOK fires after a successful ingest

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

import papernews.web as web


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
    sched = web.start_scheduler(job=_noop)
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
    sched = web.start_scheduler(job=_noop)
    try:
        assert len(sched.get_jobs()) == 2, "malformed entry must be skipped"
    finally:
        sched.shutdown(wait=False)


async def test_interval_fallback_when_no_schedule(clean_env):
    os.environ["INGEST_INTERVAL_SECONDS"] = "60"
    sched = web.start_scheduler(job=_noop)
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
def hook_env(tmp_path: Path):
    """A stub PDF plus an executable hook that records its argv."""
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-stub")
    hook_log = tmp_path / "hook.log"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/usr/bin/env bash\necho "$1" > "{hook_log}"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_pdf, hook, hook_log, tmp_path


def _stubbed_ingest(fake_pdf: Path):
    """Bypass the real ingest work: no network, no LLM, no PDF build."""
    return (
        mock.patch.object(web, "cmd_ingest", new=mock.AsyncMock(return_value=0)),
        mock.patch.object(
            web, "_build_pdf_for_key", new=mock.AsyncMock(return_value=fake_pdf)
        ),
        mock.patch.object(web, "_load_sources", return_value=[]),
        mock.patch.object(web, "Store", return_value=mock.MagicMock()),
        mock.patch.object(web, "_current_key", return_value="testkey"),
    )


async def test_hook_runs_with_pdf_path_after_successful_ingest(clean_env, hook_env):
    fake_pdf, hook, hook_log, _ = hook_env
    os.environ["POST_INGEST_HOOK"] = str(hook)

    patches = _stubbed_ingest(fake_pdf)
    for p in patches:
        p.start()
    try:
        await web.do_ingest()
    finally:
        for p in patches:
            p.stop()

    assert hook_log.exists(), "hook script did not run"
    assert hook_log.read_text().strip() == str(fake_pdf)


async def test_hook_failure_does_not_propagate(clean_env, hook_env):
    fake_pdf, _, _, tmp_path = hook_env
    bad_hook = tmp_path / "bad.sh"
    bad_hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    bad_hook.chmod(bad_hook.stat().st_mode | stat.S_IEXEC)
    os.environ["POST_INGEST_HOOK"] = str(bad_hook)

    patches = _stubbed_ingest(fake_pdf)
    for p in patches:
        p.start()
    try:
        await web.do_ingest()  # must not raise
    finally:
        for p in patches:
            p.stop()


async def test_no_hook_means_no_subprocess(clean_env, hook_env):
    fake_pdf, _, _, _ = hook_env
    os.environ.pop("POST_INGEST_HOOK", None)

    patches = _stubbed_ingest(fake_pdf)
    run_hook = mock.patch.object(web, "_run_hook", new=mock.AsyncMock())
    for p in patches:
        p.start()
    mocked = run_hook.start()
    try:
        await web.do_ingest()
        mocked.assert_not_awaited()
    finally:
        run_hook.stop()
        for p in patches:
            p.stop()
