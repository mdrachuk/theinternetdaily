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
from .cli import cmd_ingest, cmd_prepare, collect_current_edition
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
    *, snapshot: bool = True,
) -> ed.Edition:
    """Assemble the edition for `key`, snapshotting it on first request.

    The edition is every ready article no previous edition carried, from every
    source. Taking a snapshot is what *makes* those articles published — the
    store stamps `rendered_at` on each one — so the two have to happen
    together, and in that order: a snapshot whose articles failed to be marked
    repeats itself, which is a great deal better than marking articles that
    never made it into a paper.

    Only the `ingest` job calls this with `snapshot=True`, once its pipeline
    has drained. The site never assembles a paper of its own: it serves the
    newest snapshot (`current_edition`), so the hourly `prepare` job can fill
    the store all day without a single article reaching the front page early.
    `snapshot=False` is a dry run — the same paper, written nowhere and marking
    nothing.

    Cheap — a store read and a JSON write, no LLM and no typesetting.
    """
    cache = config.cache_dir()
    existing = archive.load(cache, key)
    if existing is not None:
        return ed.refiled(existing, sources)
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


def current_edition(sources: list[dict]) -> ed.Edition:
    """The paper the site shows: the newest snapshot, re-filed against the
    sources config as it reads now.

    Deliberately not "whatever the store would produce right now". Between
    editions the store keeps filling — that is the hourly `prepare` job's
    whole purpose — and the front page must not follow it; an article becomes
    news when an edition is assembled, not when a gather happens to land.
    A fresh install with no snapshot yet shows an empty paper until the first
    `ingest` runs.
    """
    cache = config.cache_dir()
    newest = archive.latest(cache)
    if newest is not None:
        loaded = archive.load(cache, newest.key)
        if loaded is not None:
            return ed.refiled(loaded, sources)
    return ed.build([], key="", date=date.today().isoformat(), built_at="")


async def warm_icons(edition: ed.Edition) -> int:
    """Cache the source marks an edition needs, so no reader waits on them.

    Runs after ingest, on the box, over a connection that is already up. A
    reader who opens the paper on a train then gets the whole thing from disk.
    """
    domains = {icons.domain_for(i.url) for i in edition.items}
    domains.discard("")
    cache = config.cache_dir()
    todo = [d for d in domains if not icons.is_fresh(icons.icon_path(cache, d))]
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

# One pipeline at a time within a process: both jobs run LLM stages over the
# same pending rows, and two of them at once would summarize the same batch
# twice. Across processes that is the queue's job (an arq worker with
# max_jobs=1), which is why this is not the only guard.
_pipeline_lock = asyncio.Lock()
_running: str | None = None


def running() -> str | None:
    """The name of the job holding the pipeline right now, or None."""
    return _running


def ingest_running() -> bool:
    return _running == "ingest"


async def _with_pipeline(name: str, work) -> None:
    """Run `work(client, store, backend, sources)` as the one pipeline job.

    Waits for the lock rather than skipping: under the queue the two jobs
    never overlap anyway, and an edition that is due while a gather is still
    running comes out late rather than not at all.
    """
    global _running
    async with _pipeline_lock:
        _running = name
        sources = config.load_sources()
        store = open_store(config.store_url())
        backend = make_backend(config.llm_backend())
        try:
            async with client_context() as client:
                await work(client, store, backend, sources)
        finally:
            _running = None
            await backend.aclose()
            await store.close()


async def prepare() -> None:
    """gather → summarize, and nothing else. The hourly job.

    Leaves the store fuller and the site untouched: no topics, no edition, no
    hook. Whatever it writes waits in the store for the next `ingest`, which
    then has a few stragglers to process instead of a whole day's feeds.
    """
    async def _work(client, store, backend, sources):
        await cmd_prepare(client, store, backend, sources, config.workers())

    await _with_pipeline("prepare", _work)


async def ingest() -> None:
    """gather → summarize → topics → edition, then the delivery hook.

    Topics run last inside `cmd_ingest` and before the edition is assembled,
    which is the only order that works: the stage reads the finished articles
    to decide what this edition's sections are, and the assemble step then
    lays the paper out along them.
    """
    async def _work(client, store, backend, sources):
        standing = standing_topics(config.load_topics())
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

    await _with_pipeline("ingest", _work)


# The job registry: the same names, arguments and semantics under the
# in-process queue and under arq.
JOBS = {"prepare": prepare, "ingest": ingest}
