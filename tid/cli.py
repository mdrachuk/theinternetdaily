from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import config
from .extract import extract
from .fetch import RawItem, fetch_hn, fetch_rss, fetch_wikipedia_events
from .http import client_context
from .llm import LLMBackend, make_backend
from .render import build_pdf
from .store import ArticleRow, Store, open_store
from .wiki import (
    fetch_did_you_know,
    fetch_quote_of_day,
    fetch_world_news,
    summarize_world_news,
)


def _log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _load_sources(path: Path) -> list[dict]:
    return config.load_sources(path)


# Network-bound article extraction: many in flight is fine, but not unbounded
# — trafilatura's parse runs in a thread and the pool is finite.
FETCH_CONCURRENCY = 8


# --- stages -----------------------------------------------------------------

async def _fetch_source(
    client: httpx.AsyncClient, src: dict
) -> list[RawItem] | None:
    """Resolve one source config to its raw items. None on failure."""
    name = src["name"]
    kind = src.get("kind", "rss")
    limit = src.get("limit", 20)
    try:
        if kind == "hn":
            return await fetch_hn(
                client,
                source_name=name,
                limit=limit,
                since_hours=int(src.get("since_hours", 48)),
                min_points=int(src.get("min_points", 50)),
            )
        if kind == "rss":
            since_hours = src.get("since_hours")
            return await fetch_rss(
                client, name, src["url"], limit=limit,
                since_hours=int(since_hours) if since_hours is not None else None,
            )
        if kind == "wikipedia_events":
            return await fetch_wikipedia_events(
                source_name=name,
                days_back=src.get("days_back", 1),
            )
        _log(f"  [warn] unknown source kind '{kind}'")
        return None
    except Exception as e:
        _log(f"  [error] fetch failed: {e}")
        return None


async def cmd_gather(
    client: httpx.AsyncClient,
    store: Store,
    sources: list[dict],
    concurrency: int = FETCH_CONCURRENCY,
) -> int:
    new_count = 0
    failed_count = 0
    sem = asyncio.Semaphore(concurrency)

    async def _extract_one(it: RawItem):
        async with sem:
            return it, await extract(client, it.url, it.title, it.source)

    for src in sources:
        _log(f"[gather] {src['name']}")
        items = await _fetch_source(client, src)
        if items is None:
            continue

        todo: list[RawItem] = []
        for it in items:
            if await store.exists(it.url, it.title):
                # Back-fill the surfacing date on a re-gather, even if the
                # row already exists.
                await store.insert_raw(
                    it.source, it.url, it.title,
                    text=None, surfaced=it.surfaced,
                )
                continue
            todo.append(it)

        results = await asyncio.gather(
            *(_extract_one(it) for it in todo), return_exceptions=True
        )
        for it, res in zip(todo, results):
            if isinstance(res, BaseException):
                _log(f"  [error] extract: {it.title[:60]}: {res}")
                await store.insert_raw(
                    it.source, it.url, it.title,
                    text=None, surfaced=it.surfaced,
                )
                failed_count += 1
                continue
            _, art = res
            if art is None:
                await store.insert_raw(
                    it.source, it.url, it.title,
                    text=None, surfaced=it.surfaced,
                )
                failed_count += 1
                _log(f"  - {it.title[:70]}  (no readable content)")
            else:
                # Prefer the article's own date; fall back to the surfacing
                # date so we always have something to display.
                await store.insert_raw(
                    it.source, it.url, it.title,
                    text=art.text,
                    surfaced=it.surfaced,
                    published=art.published or it.surfaced,
                )
                new_count += 1
                _log(f"  + {it.title[:70]}  ({len(art.text)} chars)")
    _log(f"[gather] +{new_count} new, {failed_count} unreadable")
    return 0


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _chunks_by_source(rows: list[ArticleRow], n: int) -> list[list[ArticleRow]]:
    """Chunk pending rows into batches that never mix sources.

    One LLM call carries several articles, and the model treats them as one
    piece of work: put a Russian article in a batch with three English ones and
    it summarizes all four in English, ignoring the instruction to keep each
    article's own language. Batching per source keeps every call
    single-language, since a source is one publication.

    It also shrinks the blast radius of a bad reply to a single source.
    """
    groups: dict[str, list[ArticleRow]] = {}
    for row in rows:
        groups.setdefault(row.source, []).append(row)
    batches: list[list[ArticleRow]] = []
    for items in groups.values():
        batches.extend(_chunks(items, n))
    return batches


async def _run_llm_stage(
    stage: str,
    pending: list[ArticleRow],
    batch_fn,
    apply_fn,
    workers: int,
    batch_size: int,
) -> int:
    """Shared driver for the summarize and rewrite stages: chunk the pending
    rows, run `batch_fn` over each chunk with at most `workers` in flight, and
    store each batch's results as soon as it lands.

    Storing per batch rather than after the whole stage matters on a local
    model, where one stage runs for tens of minutes: a crash at article 43
    must not throw away 42 articles' worth of GPU time. Every stage is
    resumable — whatever was stored is simply not pending next time.
    """
    batches = _chunks_by_source(pending, batch_size)
    _log(f"[{stage}] {len(pending)} pending in {len(batches)} batch(es) "
         f"of {batch_size} (workers={workers})")
    sem = asyncio.Semaphore(workers)
    finished = 0

    async def _one(rows: list[ArticleRow]) -> tuple[int, int]:
        nonlocal finished
        async with sem:
            started = time.perf_counter()
            out = await batch_fn([(r.title, r.text or "") for r in rows])
            elapsed = time.perf_counter() - started
        done = errors = 0
        for row, value in zip(rows, out):
            if value:
                await apply_fn(row.id, value)
                done += 1
            else:
                # The model failed to label this one. Leave it pending rather
                # than storing junk; the next run picks it up.
                errors += 1
        finished += 1
        # Logged as each batch lands, not after the gather: a silent terminal
        # for twenty minutes is indistinguishable from a hang.
        _log(f"  ✓ batch of {len(rows)} "
             f"({finished}/{len(batches)}, {elapsed:.1f}s, {errors} unusable)")
        return done, errors

    results = await asyncio.gather(
        *(_one(b) for b in batches), return_exceptions=True
    )
    done = errors = 0
    for batch, res in zip(batches, results):
        if isinstance(res, BaseException):
            errors += len(batch)
            _log(f"  [error] batch ({len(batch)} articles): {res}")
            continue
        done += res[0]
        errors += res[1]
    _log(f"[{stage}] done {done}/{len(pending)}, {errors} errors")
    return 0


def _resolve_workers(backend: LLMBackend, workers: int | None) -> int:
    """An explicit --workers wins; otherwise take the backend's own cap. For
    vLLM that should match --max-num-seqs: the server batches continuously, so
    oversubscribing past it only queues."""
    return workers if workers else backend.limits.max_concurrent


async def cmd_summarize(
    store: Store, backend: LLMBackend, workers: int | None = None
) -> int:
    from .summarize import summarize_batch

    pending = await store.pending_summary()
    if not pending:
        _log("[summarize] nothing pending")
        return 0

    async def _batch(items):
        return await summarize_batch(backend, items)

    return await _run_llm_stage(
        "summarize", pending, _batch, store.set_summary,
        _resolve_workers(backend, workers),
        backend.limits.summarize_batch,
    )


async def cmd_rewrite(
    store: Store, backend: LLMBackend, workers: int | None = None
) -> int:
    from .rewrite import rewrite_batch

    pending = await store.pending_rewrite()
    if not pending:
        _log("[rewrite] nothing pending")
        return 0

    async def _batch(items):
        return await rewrite_batch(backend, items)

    return await _run_llm_stage(
        "rewrite", pending, _batch, store.set_body,
        _resolve_workers(backend, workers),
        backend.limits.rewrite_batch,
    )


def _format_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return iso


async def gather_decorations(
    client: httpx.AsyncClient, backend: LLMBackend
) -> dict:
    """Fetch the cover decorations (Wikipedia world news + QOTD + DYK)."""
    decorations: dict = {}
    wn_res, qotd_res, dyk_res = await asyncio.gather(
        fetch_world_news(client),
        fetch_quote_of_day(client),
        fetch_did_you_know(client, limit=4),
        return_exceptions=True,
    )
    if isinstance(wn_res, BaseException):
        _log(f"  [warn] world news: {wn_res}")
    elif wn_res:
        try:
            decorations["world_news"] = await summarize_world_news(
                backend, wn_res
            )
            from datetime import date as _d
            decorations["world_news_date"] = _d.today().strftime("%B %-d, %Y")
        except Exception as e:
            _log(f"  [warn] world news: {e}")
    if isinstance(qotd_res, BaseException):
        _log(f"  [warn] qotd: {qotd_res}")
    elif qotd_res:
        decorations["quote"] = {"text": qotd_res[0], "author": qotd_res[1]}
    if isinstance(dyk_res, BaseException):
        _log(f"  [warn] dyk: {dyk_res}")
    elif dyk_res:
        decorations["dyk"] = dyk_res
    return decorations


async def collect_current_edition(store: Store, sources: list[dict]) -> list[dict]:
    """Pick the latest N articles per source (N = source.limit), in source
    config order. Returns render-ready dicts."""
    out: list[dict] = []
    for src in sources:
        name = src["name"]
        limit = int(src.get("limit", 10))
        # Rows are never deleted, so gather-time filtering alone would leave
        # previously-ingested stale articles in the edition forever — the
        # window has to be re-applied here at render time.
        #
        # Note this makes the edition clock-dependent: the same store and
        # config yield a different edition once the window rolls past an
        # article. The cache key (cache.edition_key) only moves on new
        # content or config changes, so a cached PDF can outlive its window
        # until the next ingest. That's deliberate — it beats serving an
        # empty paper between ingests.
        since_hours = src.get("since_hours")
        since_date = (
            (datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
            .date().isoformat()
            if since_hours is not None else None
        )
        rows = await store.latest_per_source(name, limit, since_date=since_date)
        for r in rows:
            out.append({
                "source": r.source,
                "url": r.url,
                "title": r.title,
                "text": r.body or r.text,
                "summary": r.summary,
                "date": _format_date(r.published or r.surfaced),
            })
    return out


async def cmd_render(
    client: httpx.AsyncClient,
    store: Store,
    backend: LLMBackend,
    date: str,
    out_dir: Path,
    sources: list[dict],
) -> int:
    """Build the current edition: latest N per source + live decorations.

    No time-window filter, no read state. PDF reflects whatever is currently
    in the store at this moment.
    """
    articles = await collect_current_edition(store, sources)
    if not articles:
        _log("[render] no ready articles in store yet")
        return 0
    _log("[render] fetching cover decorations (Wikipedia world news + QOTD + DYK)")
    decorations = await gather_decorations(client, backend)
    _log(f"[render] {len(articles)} articles → PDF")
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = await build_pdf(date, articles, out_dir, decorations=decorations)
    print(str(pdf))
    return 0


async def cmd_ingest(
    client: httpx.AsyncClient,
    store: Store,
    backend: LLMBackend,
    sources: list[dict],
    workers: int | None = None,
) -> int:
    """Run gather + summarize + rewrite. No PDF — that's the renderer's job."""
    rc = await cmd_gather(client, store, sources)
    if rc:
        return rc
    rc = await cmd_summarize(store, backend, workers)
    if rc:
        return rc
    return await cmd_rewrite(store, backend, workers)


async def cmd_status(store: Store) -> int:
    c = await store.counts()
    print(f"total articles         : {c['total']}")
    print(f"  unreadable           : {c['unreadable']}")
    print(f"  awaiting summary     : {c['pending_summary']}")
    print(f"  awaiting rewrite     : {c['pending_rewrite']}")
    print(f"  awaiting render      : {c['pending_render']}")
    print(f"  already rendered     : {c['rendered']}")
    return 0


async def cmd_migrate(src_url: str, dst_url: str, batch: int = 500) -> int:
    """Copy every article from one store to another, through the protocol.

    Store-to-store rather than dump-and-load, so it works for any pair of
    backends — including a downstream's own implementation.
    """
    src = open_store(src_url)
    dst = open_store(dst_url)
    try:
        rows = await src.all_rows()
        _log(f"[migrate] {len(rows)} rows: {src_url} -> {dst_url}")
        copied = 0
        for chunk in _chunks(rows, batch):
            copied += await dst.upsert_rows(chunk)
            _log(f"  … {copied}/{len(rows)}")
        counts = await dst.counts()
        _log(f"[migrate] done; target now holds {counts['total']} articles")
    finally:
        await src.close()
        await dst.close()
    return 0


# --- CLI --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tid")
    p.add_argument("--config", type=Path, default=Path("sources.toml"))
    p.add_argument("--out",    type=Path, default=Path("archive"))
    p.add_argument("--state",  type=Path, default=Path("state.db"),
                   help="SQLite file (shorthand for --store)")
    p.add_argument("--backend", default=None,
                   help="LLM backend: anthropic, vllm, or ollama "
                        "(default: LLM_BACKEND, else anthropic)")
    p.add_argument("--store", default=None,
                   help="store URL, e.g. state.db or "
                        "mongodb://localhost:27017/tid "
                        "(default: TID_STORE, else --state)")

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gather", help="fetch + extract new articles into the store")

    # --workers unset means "ask the backend": a 70 W local GPU and a hosted
    # API want very different numbers.
    sp_sum = sub.add_parser("summarize", help="summarize articles still missing a summary")
    sp_sum.add_argument("--workers", type=int, default=None)

    sp_rw = sub.add_parser("rewrite", help="reformat article bodies into clean paragraphs")
    sp_rw.add_argument("--workers", type=int, default=None)

    sp_ing = sub.add_parser("ingest", help="gather + summarize + rewrite (no PDF)")
    sp_ing.add_argument("--workers", type=int, default=None)

    sp_ren = sub.add_parser("render", help="render the current edition PDF")
    sp_ren.add_argument("--date", default=date_cls.today().isoformat())

    sub.add_parser("status", help="print store counts")

    sp_mig = sub.add_parser(
        "migrate", help="copy articles between two stores")
    sp_mig.add_argument("--from", dest="src", required=True,
                        help="source store URL (e.g. state.db)")
    sp_mig.add_argument("--to", dest="dst", required=True,
                        help="target store URL (e.g. mongodb://localhost/tid)")

    sp_b = sub.add_parser("build", help="ingest + render (default)")
    sp_b.add_argument("--workers", type=int, default=None)
    sp_b.add_argument("--date",    default=date_cls.today().isoformat())
    return p


def store_url(args) -> str:
    """Resolve the store URL: --store, then TID_STORE, then --state."""
    return args.store or os.environ.get("TID_STORE") or str(args.state)


async def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "build"

    if cmd == "migrate":
        return await cmd_migrate(args.src, args.dst)

    if not args.config.exists():
        _log(f"[fatal] config not found: {args.config}")
        return 2

    # Always load config (cheap; renderer needs source order).
    sources = _load_sources(args.config)
    if cmd in ("gather", "ingest", "build") and not sources:
        _log("[fatal] no sources configured")
        return 2

    store = open_store(store_url(args))
    if cmd == "status":
        try:
            return await cmd_status(store)
        finally:
            await store.close()

    backend = make_backend(args.backend)
    workers = getattr(args, "workers", None)
    try:
        # One client for the whole run, closed on the way out.
        async with client_context() as client:
            if cmd == "gather":
                return await cmd_gather(client, store, sources)
            if cmd == "summarize":
                return await cmd_summarize(store, backend, workers)
            if cmd == "rewrite":
                return await cmd_rewrite(store, backend, workers)
            if cmd == "ingest":
                return await cmd_ingest(client, store, backend, sources, workers)
            if cmd == "render":
                return await cmd_render(
                    client, store, backend, args.date, args.out, sources
                )
            if cmd == "build":
                rc = await cmd_ingest(client, store, backend, sources, workers)
                if rc:
                    return rc
                return await cmd_render(
                    client, store, backend, args.date, args.out, sources
                )
    finally:
        await backend.aclose()
        await store.close()

    _log(f"[fatal] unknown command: {cmd}")
    return 2


def main(argv: list[str] | None = None) -> int:
    """Sync entry point for `[project.scripts]`."""
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
