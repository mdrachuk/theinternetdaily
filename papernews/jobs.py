"""The unit-of-work layer: everything a queue can be asked to run.

Kept free of any web framework so an arq worker process
(`arq papernews.worker.WorkerSettings`) can import it without pulling FastAPI,
and so the job graph is identical under either queue backend.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

from . import archive, config
from .cache import edition_key, ensure_dir, pdf_path, preview_path
from .cli import cmd_ingest, collect_current_edition, gather_decorations
from .http import client_context
from .llm import LLMBackend, make_backend
from .preview import render_cover_png
from .render import build_pdf
from .store import Store, open_store


def _log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


# --- edition cache --------------------------------------------------------

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


async def current_key(store: Store, sources: list[dict]) -> str:
    """Identify 'which edition this is' — content high-water mark + config."""
    return edition_key(await store.max_fetched_at(), sources)


async def build_pdf_for_key(
    key: str,
    store: Store,
    sources: list[dict],
    backend: LLMBackend | None = None,
) -> Path:
    """Build the current-edition PDF into the cache, keyed by `key`.

    A backend is needed only for the cover's world-news bullets; when the
    caller hasn't got one, build a short-lived one.
    """
    cache = config.cache_dir()
    out = pdf_path(cache, key)
    if out.exists():
        return out
    async with _lock_for(key):
        if out.exists():
            return out
        ensure_dir(cache)
        articles = await collect_current_edition(store, sources)
        own_backend = backend is None
        llm = backend or make_backend(config.llm_backend())
        try:
            async with client_context() as client:
                decorations = await gather_decorations(client, llm)
        finally:
            if own_backend:
                await llm.aclose()
        # Use the cache dir as build workdir so .build/ stays beside the PDF.
        today = date.today().isoformat()
        tmp_pdf = await build_pdf(
            today, articles, cache, decorations=decorations
        )
        if tmp_pdf != out:
            tmp_pdf.replace(out)
        # Sidecar last: it is what puts this edition on the archive index, so
        # it must not appear before the PDF it describes is in place.
        archive.record(cache, key, today, articles)
    return out


async def build_preview_for_key(key: str, pdf: Path) -> Path:
    out = preview_path(config.cache_dir(), key)
    if out.exists():
        return out
    async with _lock_for(f"preview:{key}"):
        if out.exists():
            return out
        await render_cover_png(pdf, out, dpi=180)
    return out


# --- delivery -------------------------------------------------------------

async def run_hook(hook: str, pdf: Path) -> None:
    """Run the post-ingest delivery hook with the PDF path as $1.

    Step 6 turns this into a `Delivery` interface with the subprocess hook as
    one implementation; the shape is already that of one.
    """
    timeout = config.post_ingest_hook_timeout()
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


# --- jobs -----------------------------------------------------------------

# One ingest at a time within a process. Across processes that is the queue's
# job (an arq worker with max_jobs=1), which is why this is not the only guard.
_ingest_lock = asyncio.Lock()


def ingest_running() -> bool:
    return _ingest_lock.locked()


async def ingest() -> None:
    """gather → summarize → rewrite, then the optional delivery hook."""
    if _ingest_lock.locked():
        return
    async with _ingest_lock:
        sources = config.load_sources()
        store = open_store(config.store_url())
        backend = make_backend(config.llm_backend())
        try:
            async with client_context() as client:
                await cmd_ingest(
                    client, store, backend, sources, config.workers()
                )

            # Build the edition here rather than leaving it to whoever asks
            # for /digest.pdf first. An unattended box has to end every ingest
            # with a PDF on disk and a row in the archive, otherwise the
            # archive records when someone happened to visit, not what was
            # published.
            pdf = None
            try:
                key = await current_key(store, sources)
                pdf = await build_pdf_for_key(key, store, sources, backend)
            except Exception as e:
                _log(f"[post-ingest build] {e}")

            # The hook is an executable on the container's filesystem (usually
            # dropped in via the bind volume) that receives the freshly-built
            # PDF path as its single argument. Useful for SCP-ing to a
            # reMarkable, mailing it somewhere, printing, etc.
            hook = config.post_ingest_hook()
            if hook and pdf is not None:
                try:
                    await run_hook(hook, pdf)
                except Exception as e:
                    _log(f"[post-ingest hook] {e}")
        finally:
            await backend.aclose()
            await store.close()


# The job registry: the same names, arguments and semantics under the
# in-process queue and under arq.
JOBS = {"ingest": ingest}
