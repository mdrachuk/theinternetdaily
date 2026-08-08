"""FastAPI web service for papernews.

Routes:
  GET /              landing page (cover preview + 'Read today' link)
  GET /digest.pdf    current edition PDF (cached, built on demand)
  GET /preview.png   page-1 PNG of the current edition
  GET /sources       JSON list of configured sources + counts
  GET /healthz       liveness probe

Background:
  APScheduler (AsyncIOScheduler) runs `ingest` on a schedule.

Environment (read per call, never at import time):
  PAPERNEWS_STATE        path to state.db          (default: state.db)
  PAPERNEWS_CONFIG       path to sources.toml      (default: sources.toml)
  PAPERNEWS_CACHE        path to cache dir         (default: archive/cache)
  PAPERNEWS_WORKERS      LLM workers               (default: 8)

  Scheduling — pick one:
    INGEST_INTERVAL_SECONDS    every N seconds         (default: 14400 = 4h)
    INGEST_SCHEDULE            "HH:MM,HH:MM,..." cron-style fixed times
    INGEST_TIMEZONE            IANA tz, used with INGEST_SCHEDULE (default: UTC)

  Post-ingest delivery hook:
    POST_INGEST_HOOK           executable on disk; receives the PDF path as $1
    POST_INGEST_HOOK_TIMEOUT   seconds (default: 300)

  ANTHROPIC_API_KEY      required for the Anthropic backend
"""
from __future__ import annotations

import asyncio
import os
import sys
import tomllib
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)

from .cache import edition_key, ensure_dir, pdf_path, preview_path
from .cli import cmd_ingest, collect_current_edition, gather_decorations
from .http import client_context
from .preview import render_cover_png
from .render import build_pdf
from .store import Store


# --- Config helpers -------------------------------------------------------
#
# Read the environment when a request or job actually needs it. Import-time
# constants make the module impossible to reconfigure from a test or from a
# downstream library consumer.

def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, default))


def state_path() -> Path:
    return _env_path("PAPERNEWS_STATE", "state.db")


def config_path() -> Path:
    return _env_path("PAPERNEWS_CONFIG", "sources.toml")


def cache_dir() -> Path:
    return _env_path("PAPERNEWS_CACHE", "archive/cache")


def workers() -> int:
    return int(os.environ.get("PAPERNEWS_WORKERS", "8"))


def _load_sources() -> list[dict]:
    with open(config_path(), "rb") as f:
        return tomllib.load(f).get("source", [])


# --- Build pipeline -------------------------------------------------------

# Per-key lock so concurrent requests for the same cache key only build once.
_build_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _build_locks.get(key)
    if lock is None:
        # Safe without a guard: the event loop never preempts between the get
        # and the set, so two coroutines cannot both miss and both insert.
        lock = asyncio.Lock()
        _build_locks[key] = lock
    return lock


def _current_key(store: Store, sources: list[dict]) -> str:
    return edition_key(store.max_fetched_at(), sources)


async def _build_pdf_for_key(key: str, store: Store, sources: list[dict]) -> Path:
    """Build the current-edition PDF into the cache, keyed by `key`."""
    cache = cache_dir()
    out = pdf_path(cache, key)
    if out.exists():
        return out
    async with _lock_for(key):
        if out.exists():
            return out
        ensure_dir(cache)
        articles = collect_current_edition(store, sources)
        async with client_context() as client:
            decorations = await gather_decorations(client)
        # Use the cache dir as build workdir so .build/ stays beside the PDF.
        tmp_pdf = await build_pdf(
            date.today().isoformat(),
            articles,
            cache,
            decorations=decorations,
        )
        if tmp_pdf != out:
            tmp_pdf.replace(out)
    return out


async def _build_preview_for_key(key: str, pdf: Path) -> Path:
    out = preview_path(cache_dir(), key)
    if out.exists():
        return out
    async with _lock_for(f"preview:{key}"):
        if out.exists():
            return out
        await render_cover_png(pdf, out, dpi=180)
    return out


# --- Background ingest ----------------------------------------------------

_ingest_lock = asyncio.Lock()


def ingest_running() -> bool:
    return _ingest_lock.locked()


async def _run_hook(hook: str, pdf: Path) -> None:
    """Run the post-ingest delivery hook with the PDF path as $1."""
    timeout = float(os.environ.get("POST_INGEST_HOOK_TIMEOUT", "300"))
    proc = await asyncio.create_subprocess_exec(
        hook, str(pdf),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"hook timed out after {timeout:.0f}s")


async def do_ingest() -> None:
    if _ingest_lock.locked():
        return  # one ingest at a time
    async with _ingest_lock:
        sources = _load_sources()
        store = Store(state_path())
        async with client_context() as client:
            await cmd_ingest(client, store, sources, workers())

        # Optional post-ingest delivery hook. The hook is an executable on the
        # container's filesystem (usually dropped in via the bind volume) that
        # receives the freshly-built PDF path as its single argument. Useful
        # for SCP-ing to a reMarkable, mailing it somewhere, printing, etc.
        hook = os.environ.get("POST_INGEST_HOOK", "").strip()
        if hook:
            try:
                key = _current_key(store, sources)
                pdf = await _build_pdf_for_key(key, store, sources)
                await _run_hook(hook, pdf)
            except Exception as e:
                sys.stderr.write(f"[post-ingest hook] {e}\n")
                sys.stderr.flush()


# --- Scheduler ------------------------------------------------------------

def start_scheduler(job=do_ingest) -> AsyncIOScheduler:
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
        sched.add_job(job, "interval",
                      seconds=every, id="ingest",
                      next_run_time=None)
    sched.start()
    return sched


# --- App ------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Own the scheduler for the app's lifetime. Importing this module must
    not start it — that is what made the old Flask version untestable."""
    app.state.scheduler = None
    if os.environ.get("PAPERNEWS_NO_SCHED") != "1":
        app.state.scheduler = start_scheduler()
    try:
        yield
    finally:
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="papernews", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/readyz")
    async def readyz():
        """Readiness: the store must answer and the config must parse."""
        checks: dict[str, str] = {}
        status = 200
        try:
            Store(state_path()).counts()
            checks["store"] = "ok"
        except Exception as e:
            checks["store"] = f"error: {e}"
            status = 503
        try:
            _load_sources()
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
        sources = _load_sources()
        store = Store(state_path())
        return {
            "sources": [
                {"name": s["name"], "kind": s.get("kind"), "limit": s.get("limit")}
                for s in sources
            ],
            "max_fetched_at": store.max_fetched_at(),
        }

    @app.get("/digest.pdf")
    async def digest_pdf():
        sources = _load_sources()
        store = Store(state_path())
        key = _current_key(store, sources)
        pdf = await _build_pdf_for_key(key, store, sources)
        return FileResponse(
            pdf,
            media_type="application/pdf",
            filename=f"papernews-{date.today().isoformat()}.pdf",
            content_disposition_type="inline",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/preview.png")
    async def preview_png():
        sources = _load_sources()
        store = Store(state_path())
        key = _current_key(store, sources)
        pdf = await _build_pdf_for_key(key, store, sources)
        png = await _build_preview_for_key(key, pdf)
        return FileResponse(
            png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/ingest")
    async def trigger_ingest():
        # Optional manual kick; for cron-style external triggers.
        if ingest_running():
            return JSONResponse({"status": "already running"}, status_code=202)
        # Fire and forget: the caller gets 202 and the run continues on the
        # loop. Keep a reference so the task isn't garbage collected midway.
        app.state.ingest_task = asyncio.create_task(do_ingest())
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
