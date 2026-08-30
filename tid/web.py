"""FastAPI web service for The Internet Daily.

The paper *is* the website. Every article the pipeline gathered, summarized and
rewrote is laid out as a front page with its continuation below the fold, and
each one has a page of its own carrying the full rewritten text — no clicking
through to the source unless you want to.

Routes:
  GET  /                    the current edition
  GET  /e/{key}             an edition from the archive
  GET  /e/{key}/a/{id}      one article's full text, in that edition
  GET  /a/{id}              the same, resolved in the current edition
  GET  /sources             the subscription list
  GET  /sources.json        the same, as JSON
  GET  /archive.json        every edition, as JSON
  GET  /icon/{domain}.png   a cached source mark
  GET  /healthz             liveness probe
  GET  /readyz              readiness probe (store + config)
  POST /ingest              manual kick, via the job queue

Both edition routes take `?m=read|watch|listen` to show one medium only.

Background:
  APScheduler (AsyncIOScheduler) enqueues `ingest` on a schedule.

PDFs are not built. `tid render` still typesets one from the same store if you
want a copy for an e-ink reader, but nothing on this service does it, and no
route serves one.

Configuration is read from the environment per call by `tid.config`;
the work itself lives in `tid.jobs`, so an arq worker runs exactly the
same code without importing this module.

Environment:
  TID_STATE   SQLite path (default: state.db)
  TID_STORE   store URL; overrides TID_STATE (e.g. mongodb://…)
  TID_QUEUE   queue URL; unset = in-process (e.g. redis://redis:6379)
  TID_CONFIG  sources.toml path
  TID_CACHE   cache dir (edition snapshots + cached source marks)
  TID_WORKERS concurrent LLM batches

  Scheduling — pick one:
    INGEST_INTERVAL_SECONDS  every N seconds (default: 14400 = 4h)
    INGEST_SCHEDULE          "HH:MM,HH:MM,…" cron-style fixed times
    INGEST_TIMEZONE          IANA tz, used with INGEST_SCHEDULE (default: UTC)

  Post-ingest delivery hook:
    POST_INGEST_HOOK          executable on disk; receives the snapshot path as $1
    POST_INGEST_HOOK_TIMEOUT  seconds (default: 300)
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from . import archive, config, edition as ed, icons, jobs, site
from .http import client_context
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
    if os.environ.get("TID_NO_SCHED") != "1":
        async def _enqueue_ingest() -> None:
            await app.state.queue.enqueue("ingest", job_id="ingest")

        app.state.scheduler = start_scheduler(_enqueue_ingest)
    try:
        yield
    finally:
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
        await app.state.queue.close()


def _medium(value: str | None) -> str | None:
    """Sanitize `?m=`. An unknown value is ignored rather than rejected: a
    stale bookmark should still show you a newspaper."""
    return value if value in ed.MEDIA else None


def create_app(queue=None) -> FastAPI:
    app = FastAPI(title="The Internet Daily", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    # concurrency=1: the LLM stages are the bottleneck, so a second concurrent
    # ingest would only contend for them.
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

    # --- the paper --------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(m: str | None = None):
        return _page(await _current_edition(), _medium(m))

    @app.get("/e/{key}", response_class=HTMLResponse)
    async def archived(key: str, m: str | None = None):
        edition = archive.load(config.cache_dir(), key)
        if edition is None:
            raise HTTPException(status_code=404, detail="no such edition")
        return _page(edition, _medium(m))

    @app.get("/a/{article_id}", response_class=HTMLResponse)
    async def article_current(article_id: str):
        return _article_page(await _current_edition(), article_id)

    @app.get("/e/{key}/a/{article_id}", response_class=HTMLResponse)
    async def article_archived(key: str, article_id: str):
        edition = archive.load(config.cache_dir(), key)
        if edition is None:
            raise HTTPException(status_code=404, detail="no such edition")
        return _article_page(edition, article_id)

    # --- the sources ------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    async def sources_page():
        latest = archive.readable(config.cache_dir())
        rows = _source_rows(latest[0].sources if latest else {})
        store = open_store(config.store_url())
        try:
            counts, fetched_at = await store.counts(), await store.max_fetched_at()
        except Exception:
            counts, fetched_at = dict.fromkeys(
                ("total", "rendered", "pending_summary", "pending_rewrite"), 0
            ), ""
        finally:
            await store.close()
        return HTMLResponse(site.render_sources(rows, counts, fetched_at))

    @app.get("/sources.json")
    async def sources_json():
        store = open_store(config.store_url())
        try:
            return {
                "sources": _source_rows(),
                "current_key": await _current_key_or_none(),
                "max_fetched_at": await store.max_fetched_at(),
            }
        finally:
            await store.close()

    @app.get("/archive.json")
    async def archive_json():
        current = await _current_key_or_none()
        return {
            "current": current,
            "editions": [
                {**vars(e), "url": f"/e/{e.key}", "is_current": e.key == current}
                for e in archive.editions(config.cache_dir())
            ],
        }

    # --- source marks -----------------------------------------------------

    @app.get("/icon/{domain}.png")
    async def icon(domain: str):
        """A source's favicon, fetched once and then served from disk.

        The reader's browser only ever talks to this origin; the one lookup
        upstream happens here, usually during ingest before anyone has asked.
        """
        cache = config.cache_dir()
        path = icons.icon_path(cache, domain) if icons.is_domain(domain) else None
        if path is None:
            raise HTTPException(status_code=404, detail="not a domain")
        if not path.exists():
            try:
                async with client_context() as client:
                    await icons.fetch_icon(client, cache, domain)
            except Exception:
                pass
        if not path.exists():
            return Response(
                icons.BLANK_PNG, media_type="image/png",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return FileResponse(
            path, media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    # --- ingest -----------------------------------------------------------

    @app.post("/ingest")
    async def trigger_ingest():
        # Optional manual kick, for cron-style external triggers. It goes
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

    # --- retired ----------------------------------------------------------

    @app.get("/digest.pdf")
    @app.get("/preview.png")
    @app.get("/digest/{rest:path}")
    async def retired_pdf_routes(rest: str = ""):
        """The PDF era, answered honestly rather than with a 404.

        Anything bookmarked here wanted an edition, and an edition is what the
        site still has — as a page.
        """
        return RedirectResponse("/", status_code=301)

    return app


# --- helpers --------------------------------------------------------------

def _page(edition: ed.Edition, medium: str | None) -> HTMLResponse:
    """One rendered edition, with its neighbours wired into the day nav."""
    rows = archive.readable(config.cache_dir())
    keys = [e.key for e in rows]           # newest first
    prev = next_ = None
    if edition.key in keys:
        i = keys.index(edition.key)
        if i + 1 < len(rows):
            prev = _step(rows[i + 1], medium)
        if i > 0:
            next_ = _step(rows[i - 1], medium)
    elif rows:
        # The current edition is assembled but not yet snapshotted (it is
        # empty). The archive's newest is still the way back.
        prev = _step(rows[0], medium)
    return HTMLResponse(site.render_edition(
        ed.filtered(edition, medium),
        prev=prev, next=next_, medium=medium, base=_base(edition, keys),
    ))


def _base(edition: ed.Edition, keys: list[str]) -> str:
    """This edition's own URL. An edition with nothing in it was never
    snapshotted, so `/e/{key}` would 404 — only `/` can show it."""
    return f"/e/{edition.key}" if edition.key in keys else "/"


def _step(row: archive.Edition, medium: str | None) -> dict:
    href = f"/e/{row.key}" + (f"?m={medium}" if medium else "")
    return {"href": href, "label": site.short_date(row.date)}


def _article_page(edition: ed.Edition, article_id: str) -> HTMLResponse:
    item = edition.find(article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="no such article")
    keys = [e.key for e in archive.readable(config.cache_dir())]
    return HTMLResponse(
        site.render_article(item, edition.key, _base(edition, keys))
    )


def _source_rows(in_edition: dict[str, int] | None = None) -> list[dict]:
    """sources.toml, in the shape the subscriptions page and its JSON want.

    `in_edition` is the latest edition's per-source article count, which is the
    only honest answer to "how much does this feed actually give me" that does
    not need a second pass over the store.
    """
    counts = in_edition or {}
    rows = []
    for s in config.load_sources():
        name = s.get("name", "?")
        url = s.get("url", "")
        hours = s.get("since_hours")
        rows.append({
            "name": name,
            "kind": s.get("kind"),
            "url": url,
            "section": s.get("section") or name,
            "medium": s.get("medium") or "read",
            "limit": s.get("limit", 10),
            "window": f"last {hours}h" if hours else "",
            "rate": (f"{counts[name]} in the latest edition"
                     if counts.get(name) else ""),
            "icon": icons.icon_url(url),
        })
    return rows


async def _current_edition() -> ed.Edition:
    """The edition for right now, assembling and snapshotting it if needed."""
    sources = config.load_sources()
    store = open_store(config.store_url())
    try:
        key = await jobs.current_key(store, sources)
        return await jobs.build_edition_for_key(key, store, sources)
    finally:
        await store.close()


async def _current_key_or_none() -> str | None:
    """The key the current edition *would* have, without assembling anything.

    archive.json must answer even when the store is unreachable — a missing
    `current` is a better response than a 500 — so failures collapse to None.
    """
    try:
        sources = config.load_sources()
        store = open_store(config.store_url())
        try:
            return await jobs.current_key(store, sources)
        finally:
            await store.close()
    except Exception:
        return None


# ASGI entry point: `uvicorn tid.web:app`
app = create_app()
