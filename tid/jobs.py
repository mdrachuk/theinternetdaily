"""The unit-of-work layer: everything a queue can be asked to run.

Kept free of any web framework so an arq worker process
(`arq tid.worker.WorkerSettings`) can import it without pulling FastAPI,
and so the job graph is identical under either queue backend.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

from . import archive, config, edition as ed, icons
from .cache import edition_key, ensure_dir
from .cli import cmd_ingest, collect_current_edition
from .http import client_context
from .llm import make_backend
from .store import Store, open_store
from .topics import standing_topics


def _log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


# --- edition cache --------------------------------------------------------

# Per-key lock so concurrent requests for the same cache key only assemble once.
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


async def build_edition_for_key(
    key: str, store: Store, sources: list[dict],
    *, snapshot: bool | None = None,
) -> ed.Edition:
    """Assemble the edition for `key`, snapshotting it on first request.

    The edition is every ready article no previous edition carried, from every
    source. Taking a snapshot is what *makes* those articles published — the
    store stamps `rendered_at` on each one — so the two have to happen
    together, and in that order: a snapshot whose articles failed to be marked
    repeats itself, which is a great deal better than marking articles that
    never made it into a paper.

    `snapshot` defaults to "not while an ingest is running". A paper assembled
    halfway through one would carry whatever had finished rewriting at that
    moment and mark the rest published-but-absent; the ingest builds its own
    edition when it is done, and passes True.

    Cheap — a store read and a JSON write, no LLM and no typesetting — so the
    web process doing this inline on a cache miss costs a reader a moment, not
    a minute. That is the whole reason the PDF build used to need a queue.
    """
    cache = config.cache_dir()
    existing = archive.load(cache, key)
    if existing is not None:
        return ed.refiled(existing, sources)
    if snapshot is None:
        snapshot = not ingest_running()
    async with _lock_for(key):
        existing = archive.load(cache, key)
        if existing is not None:
            return ed.refiled(existing, sources)
        ensure_dir(cache)
        today = date.today().isoformat()
        floor = archive.floor(cache)
        if floor is not None and snapshot:
            # Nothing has ever been published, so this edition runs a window.
            # Everything older than it has to be retired in the same breath:
            # the *next* edition sees published history, applies no window, and
            # would otherwise hand the reader the entire backlog the window
            # just excluded.
            retired = await store.retire_before(floor, today)
            if retired:
                _log(f"[edition] retired {retired} article(s) older than {floor}")
        articles = await collect_current_edition(store, sources, floor)
        if not articles or not snapshot:
            built = ed.build(articles, key=key, date=today, built_at="")
            if built.total:
                return built
            # Nothing new to publish. Keep showing the last paper rather than a
            # blank one: a sources.toml edit moves the key without gathering
            # anything, and that must not empty the front page.
            previous = archive.latest(cache)
            if previous is not None:
                loaded = archive.load(cache, previous.key)
                if loaded is not None:
                    return ed.refiled(loaded, sources)
            return built
        built = archive.record(cache, key, today, articles)
        await store.mark_rendered([a["id"] for a in articles], today)
        return built


async def warm_icons(edition: ed.Edition) -> int:
    """Cache the source marks an edition needs, so no reader waits on them.

    Runs after ingest, on the box, over a connection that is already up. A
    reader who opens the paper on a train then gets the whole thing from disk.
    """
    domains = {icons.domain_for(i.url) for i in edition.items}
    domains.discard("")
    cache = config.cache_dir()
    todo = [d for d in domains if not icons.icon_path(cache, d).exists()]
    if not todo:
        return 0
    async with client_context() as client:
        await asyncio.gather(
            *(icons.fetch_icon(client, cache, d) for d in todo),
            return_exceptions=True,
        )
    return len(todo)


# --- delivery -------------------------------------------------------------

async def run_hook(hook: str, snapshot: Path) -> None:
    """Run the post-ingest delivery hook with the edition snapshot path as $1.

    It used to receive the PDF. With the paper rendered as a website there is
    no file to hand over but the snapshot itself, which is the same edition in
    a form a script can actually read: JSON, with every article's full text.
    """
    timeout = config.post_ingest_hook_timeout()
    proc = await asyncio.create_subprocess_exec(
        hook, str(snapshot),
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
    """gather → summarize → rewrite → topics, then the optional delivery hook.

    Topics run last inside `cmd_ingest` and before the edition is assembled,
    which is the only order that works: the stage reads the finished articles
    to decide what this edition's sections are, and the assemble step then
    lays the paper out along them.
    """
    if _ingest_lock.locked():
        return
    async with _ingest_lock:
        sources = config.load_sources()
        standing = standing_topics(config.load_topics())
        store = open_store(config.store_url())
        backend = make_backend(config.llm_backend())
        try:
            async with client_context() as client:
                await cmd_ingest(
                    client, store, backend, sources, config.workers(), standing
                )

            # Assemble the edition here rather than leaving it to whoever loads
            # the site first. An unattended box has to end every ingest with a
            # row in the archive, otherwise the archive records when someone
            # happened to visit, not what was published.
            snapshot = None
            try:
                key = await current_key(store, sources)
                # snapshot=True: this is the ingest's own build, after
                # the pipeline has drained, so the paper is complete.
                built = await build_edition_for_key(
                    key, store, sources, snapshot=True
                )
                if built.total:
                    snapshot = archive.snapshot_path(config.cache_dir(), key)
                    await warm_icons(built)
            except Exception as e:
                _log(f"[post-ingest build] {e}")

            # The hook is an executable on the container's filesystem (usually
            # dropped in via the bind volume) that receives the freshly-written
            # edition snapshot as its single argument. Useful for mailing the
            # day's paper somewhere, pushing it to a reader, archiving it off
            # the box, etc.
            hook = config.post_ingest_hook()
            if hook and snapshot is not None:
                try:
                    await run_hook(hook, snapshot)
                except Exception as e:
                    _log(f"[post-ingest hook] {e}")
        finally:
            await backend.aclose()
            await store.close()


# The job registry: the same names, arguments and semantics under the
# in-process queue and under arq.
JOBS = {"ingest": ingest}
