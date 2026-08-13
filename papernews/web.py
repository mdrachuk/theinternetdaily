"""FastAPI web service for papernews.

Routes:
  GET  /                  index: latest edition + every past edition
  GET  /digest.pdf        current edition PDF (cached, built on demand)
  GET  /preview.png       page-1 PNG of the current edition
  GET  /digest/{key}.pdf  an archived edition by cache key
  GET  /digest/{key}.png  page-1 PNG of an archived edition
  GET  /archive.json      the archive as JSON
  GET  /sources           JSON list of configured sources + counts
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

import html
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)

from . import archive, config, jobs
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
        return _render_index(archive.editions(config.cache_dir()),
                             await _current_key_or_none())

    @app.get("/archive.json")
    async def archive_json():
        current = await _current_key_or_none()
        return {
            "current": current,
            "editions": [
                {**vars(e), "url": f"/digest/{e.key}.pdf",
                 "preview": f"/digest/{e.key}.png",
                 "is_current": e.key == current}
                for e in archive.editions(config.cache_dir())
            ],
        }

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

    @app.get("/digest/{key}.pdf")
    async def archived_pdf(key: str):
        pdf = archive.find(config.cache_dir(), key)
        if pdf is None:
            raise HTTPException(status_code=404, detail="no such edition")
        stamp = _edition_date(config.cache_dir(), key)
        return FileResponse(
            pdf,
            media_type="application/pdf",
            # Same-day editions differ only by key, so the key goes in the
            # filename — otherwise two downloads collide in ~/Downloads.
            filename=f"papernews-{stamp}-{key[:6]}.pdf",
            content_disposition_type="inline",
            # Archived editions are immutable: the key *is* the content hash.
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/digest/{key}.png")
    async def archived_preview(key: str):
        pdf = archive.find(config.cache_dir(), key)
        if pdf is None:
            raise HTTPException(status_code=404, detail="no such edition")
        png = await jobs.build_preview_for_key(key, pdf)
        return FileResponse(
            png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
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


async def _current_key_or_none() -> str | None:
    """The key the current edition *would* have, without building anything.

    The index must render even when the store is unreachable — a broken badge
    is a better page than a 500 — so failures collapse to None.
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


def _edition_date(cache_dir, key: str) -> str:
    for e in archive.editions(cache_dir):
        if e.key == key:
            return e.date
    return date.today().isoformat()


def _human_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%A %-d %B %Y")
    except ValueError:
        return iso or "unknown date"


def _human_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M UTC")
    except ValueError:
        return ""


def _human_size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


def _describe(e) -> str:
    """The one-line summary under an edition: article count and sources."""
    if e.articles is None:
        return "archived edition"
    bits = [f"{e.articles} article" + ("" if e.articles == 1 else "s")]
    if e.sources:
        named = ", ".join(f"{html.escape(k)} {v}" for k, v in
                          list(e.sources.items())[:4])
        extra = len(e.sources) - 4
        bits.append(named + (f", +{extra} more" if extra > 0 else ""))
    return " · ".join(bits)


_STYLE = """
    :root { color-scheme: light dark; }
    body { font-family: Georgia, "Times New Roman", serif; max-width: 760px;
           margin: 4rem auto; padding: 0 1.25rem; color: #222; }
    h1   { font-size: 2.4rem; margin: 0 0 0.2rem; letter-spacing: -0.01em; }
    h2   { font-size: 1.1rem; text-transform: uppercase; letter-spacing: 0.08em;
           color: #777; font-weight: normal; margin: 3.5rem 0 0.5rem;
           border-bottom: 1px solid #e5e5e5; padding-bottom: 0.5rem; }
    .sub { color: #777; margin: 0 0 2.5rem; font-size: 1rem; }
    a.cta { display: inline-block; padding: 0.7rem 1.4rem; border: 1px solid #222;
            text-decoration: none; color: #222; font-weight: bold; }
    a.cta:hover { background: #222; color: #fff; }
    img.cover { width: 100%; height: auto; border: 1px solid #eee;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08); }
    .latest { margin-bottom: 1.2rem; }
    .when { font-size: 1.3rem; margin: 0 0 0.2rem; }
    .facts { color: #888; font-size: 0.9rem; margin: 0 0 1rem; }
    ul.editions { list-style: none; padding: 0; margin: 0; }
    ul.editions li { display: flex; justify-content: space-between;
                     align-items: baseline; gap: 1rem; padding: 0.7rem 0;
                     border-bottom: 1px solid #f0f0f0; }
    ul.editions a { color: #222; text-decoration: none;
                    border-bottom: 1px solid #bbb; }
    ul.editions a:hover { border-bottom-color: #222; }
    .row-meta { color: #999; font-size: 0.85rem; text-align: right;
                white-space: nowrap; }
    .badge { display: inline-block; font-size: 0.7rem; text-transform: uppercase;
             letter-spacing: 0.08em; border: 1px solid #bbb; color: #777;
             padding: 0.05rem 0.4rem; margin-left: 0.5rem; vertical-align: 0.15em; }
    .empty { color: #888; font-style: italic; }
    .meta { color: #999; font-size: 0.85rem; margin-top: 3.5rem;
            border-top: 1px solid #eee; padding-top: 1rem; }
    .meta a { color: #999; }
    @media (prefers-color-scheme: dark) {
      body { color: #ddd; background: #141414; }
      h2 { color: #888; border-bottom-color: #2a2a2a; }
      a.cta { color: #ddd; border-color: #ddd; }
      a.cta:hover { background: #ddd; color: #141414; }
      ul.editions li { border-bottom-color: #262626; }
      ul.editions a { color: #ddd; border-bottom-color: #555; }
      ul.editions a:hover { border-bottom-color: #ddd; }
      img.cover { border-color: #2a2a2a; }
      .meta { border-top-color: #262626; }
    }
"""


def _render_index(editions: list, current_key: str | None) -> str:
    """The whole site index: today's edition on top, everything else below."""
    if editions:
        latest = editions[0]
        # /digest.pdf builds on demand, so when new content has landed but no
        # PDF exists for it yet, that link is what triggers the build.
        stale = current_key is not None and current_key != latest.key
        cover = f"/digest/{latest.key}.png"
        href = f"/digest/{latest.key}.pdf"
        hero = f"""  <div class="latest">
    <p class="when">{html.escape(_human_date(latest.date))}</p>
    <p class="facts">Built {html.escape(_human_time(latest.built_at))} ·
       {_describe(latest)} · {_human_size(latest.size)}</p>
    <a href="{href}"><img class="cover" src="{cover}" alt="Cover of the latest edition"></a>
  </div>
  <p><a class="cta" href="{href}">Read this edition (PDF)</a></p>"""
        if stale:
            hero += ('\n  <p class="facts">Newer articles have arrived since. '
                     '<a href="/digest.pdf">Build the current edition</a> '
                     '(takes a minute or two).</p>')
    else:
        hero = """  <p class="empty">No editions have been built yet.</p>
  <p><a class="cta" href="/digest.pdf">Build the first edition (PDF)</a></p>
  <p class="facts">The first build takes a minute or two.</p>"""

    rows = []
    for e in editions[1:]:
        badge = ('<span class="badge">current</span>'
                 if e.key == current_key else "")
        rows.append(
            f'    <li><span><a href="/digest/{e.key}.pdf">'
            f"{html.escape(_human_date(e.date))}</a>{badge}<br>"
            f'<span class="row-meta">{_describe(e)}</span></span>'
            f'<span class="row-meta">{html.escape(_human_time(e.built_at))}<br>'
            f"{_human_size(e.size)}</span></li>"
        )
    previous = (
        "  <h2>Previous editions</h2>\n  <ul class=\"editions\">\n"
        + "\n".join(rows) + "\n  </ul>"
    ) if rows else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>papernews</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{_STYLE}</style>
</head>
<body>
  <h1>papernews</h1>
  <p class="sub">A curated PDF you read on your reMarkable, not in a browser.</p>
{hero}
{previous}
  <p class="meta">Rebuilt automatically every few hours ·
     <a href="/digest.pdf">latest</a> ·
     <a href="/archive.json">archive.json</a> ·
     <a href="/sources">sources</a></p>
</body>
</html>
"""


# ASGI entry point: `uvicorn papernews.web:app`
app = create_app()
