"""FastAPI web service for papernews.

Routes:
  GET  /              landing page (cover preview + 'Read today' link)
  GET  /digest.pdf    current edition PDF (cached, built on demand)
  GET  /preview.png   page-1 PNG of the current edition
  GET  /sources       JSON list of configured sources + counts
  GET  /healthz       liveness probe
  GET  /readyz        readiness probe (store + config)
  POST /ingest        manual kick, via the job queue

Background:
  APScheduler (AsyncIOScheduler) enqueues `ingest` on a schedule.

Configuration is read from the environment per call by `papernews.config`;
the work itself lives in `papernews.jobs`, so an arq worker runs exactly the
same code without importing this module.

Environment:
  PAPERNEWS_STATE   SQLite path (default: state.db)
  PAPERNEWS_STORE   store URL; overrides PAPERNEWS_STATE (e.g. mongodb://…)
  PAPERNEWS_QUEUE   queue URL; unset = in-process (e.g. redis://redis:6379)
  PAPERNEWS_CONFIG  sources.toml path
  PAPERNEWS_CACHE   cache dir
  PAPERNEWS_WORKERS concurrent LLM batches

  Scheduling — pick one:
    INGEST_INTERVAL_SECONDS  every N seconds (default: 14400 = 4h)
    INGEST_SCHEDULE          "HH:MM,HH:MM,…" cron-style fixed times
    INGEST_TIMEZONE          IANA tz, used with INGEST_SCHEDULE (default: UTC)

  Post-ingest delivery hook:
    POST_INGEST_HOOK          executable on disk; receives the PDF path as $1
    POST_INGEST_HOOK_TIMEOUT  seconds (default: 300)
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)

from . import config, jobs
from .queue import open_queue
from .store import open_store


# --- Scheduler ------------------------------------------------------------

def start_scheduler(job) -> AsyncIOScheduler:
    """Start the background ingest scheduler on the running event loop.

    Two modes (in priority order):
      INGEST_SCHEDULE=07:00,18:00   → cron-style at the listed HH:MM times
      INGEST_INTERVAL_SECONDS=14400 → every N seconds (default 4h)

    The cron mode also honours INGEST_TIMEZONE (an IANA tz, default UTC).
    """
    sched = AsyncIOScheduler()
    schedule = os.environ.get("INGEST_SCHEDULE", "").strip()
    if schedule:
        tz = os.environ.get("INGEST_TIMEZONE", "UTC")
        for i, hm in enumerate(s.strip() for s in schedule.split(",") if s.strip()):
            try:
                h, m = hm.split(":")
                sched.add_job(
                    job, "cron",
                    hour=int(h), minute=int(m),
                    id=f"ingest_cron_{i}",
                    timezone=tz,
                )
            except (ValueError, KeyError):
                sys.stderr.write(f"[scheduler] ignoring invalid time: {hm!r}\n")
                sys.stderr.flush()
    else:
        every = int(os.environ.get("INGEST_INTERVAL_SECONDS", str(4 * 3600)))
        # No next_run_time here. Passing it explicitly as None is how
        # APScheduler represents a *paused* job (it is what job.pause()
        # writes), so the interval job would be added and never fire. Leaving
        # it out lets the trigger compute the first run, one interval out.
        sched.add_job(job, "interval", seconds=every, id="ingest")
    sched.start()
    return sched


# --- App ------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Own the scheduler for the app's lifetime. Importing this module must
    not start it — that is what made the old Flask version untestable."""
    app.state.scheduler = None
    if os.environ.get("PAPERNEWS_NO_SCHED") != "1":
        async def _enqueue_ingest() -> None:
            await app.state.queue.enqueue("ingest", job_id="ingest")

        app.state.scheduler = start_scheduler(_enqueue_ingest)
    try:
        yield
    finally:
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
        await app.state.queue.close()


def create_app(queue=None) -> FastAPI:
    app = FastAPI(title="papernews", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    # concurrency=1: the LLM stages and xelatex are the bottleneck, so a second
    # concurrent ingest would only contend for them.
    app.state.queue = queue if queue is not None else open_queue(
        config.queue_url(), jobs=jobs.JOBS, concurrency=1
    )

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/readyz")
    async def readyz():
        """Readiness: the store must answer and the config must parse."""
        checks: dict[str, str] = {}
        status = 200
        store = open_store(config.store_url())
        try:
            await store.counts()
            checks["store"] = "ok"
        except Exception as e:
            checks["store"] = f"error: {e}"
            status = 503
        finally:
            await store.close()
        try:
            config.load_sources()
            checks["config"] = "ok"
        except Exception as e:
            checks["config"] = f"error: {e}"
            status = 503
        return JSONResponse(checks, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _LANDING_HTML

    @app.get("/sources")
    async def sources_endpoint():
        sources = config.load_sources()
        store = open_store(config.store_url())
        try:
            return {
                "sources": [
                    {"name": s["name"], "kind": s.get("kind"),
                     "limit": s.get("limit")}
                    for s in sources
                ],
                "max_fetched_at": await store.max_fetched_at(),
            }
        finally:
            await store.close()

    @app.get("/digest.pdf")
    async def digest_pdf():
        pdf = await _current_pdf()
        return FileResponse(
            pdf,
            media_type="application/pdf",
            filename=f"papernews-{date.today().isoformat()}.pdf",
            content_disposition_type="inline",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/preview.png")
    async def preview_png():
        pdf, key = await _current_pdf(with_key=True)
        png = await jobs.build_preview_for_key(key, pdf)
        return FileResponse(
            png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/ingest")
    async def trigger_ingest():
        # Optional manual kick; for cron-style external triggers. It goes
        # through the queue, so a Redis-backed deployment runs it on a worker
        # instead of in the web process — same call either way.
        if jobs.ingest_running():
            return JSONResponse({"status": "already running"}, status_code=202)
        await app.state.queue.enqueue("ingest", job_id="ingest")
        return JSONResponse({"status": "started"}, status_code=202)

    @app.get("/ingest")
    async def ingest_get_hint():
        # Friendly 405 — easier than rediscovering you wanted POST.
        return JSONResponse(
            {
                "error": "POST required to trigger ingest",
                "hint": "curl -X POST http://localhost:8000/ingest",
                "note": "the background scheduler also runs ingest automatically",
            },
            status_code=405,
        )

    return app


async def _current_pdf(with_key: bool = False):
    """Resolve (and build if needed) the PDF for the current edition."""
    sources = config.load_sources()
    store = open_store(config.store_url())
    try:
        key = await jobs.current_key(store, sources)
        pdf = await jobs.build_pdf_for_key(key, store, sources)
    finally:
        await store.close()
    return (pdf, key) if with_key else pdf


_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>papernews</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: Georgia, "Times New Roman", serif; max-width: 720px;
           margin: 4rem auto; padding: 0 1.25rem; color: #222; }
    h1   { font-size: 2.4rem; margin: 0 0 0.2rem; }
    .sub { color: #777; margin: 0 0 2rem; font-size: 1rem; }
    a.cta { display: inline-block; padding: 0.7rem 1.4rem; border: 1px solid #222;
            text-decoration: none; color: #222; font-weight: bold; margin-top: 1rem;}
    a.cta:hover { background: #222; color: #fff; }
    img.cover { width: 100%; height: auto; border: 1px solid #eee;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08); }
    .meta { color: #999; font-size: 0.85rem; margin-top: 3rem; }
  </style>
</head>
<body>
  <h1>papernews</h1>
  <p class="sub">A curated PDF you read on your reMarkable, not in a browser.</p>
  <img class="cover" src="/preview.png" alt="Cover preview">
  <p><a class="cta" href="/digest.pdf">Read today (PDF)</a></p>
  <p class="meta">Updated automatically every few hours. <a href="/sources">Sources</a>.</p>
</body>
</html>
"""


# ASGI entry point: `uvicorn papernews.web:app`
app = create_app()
