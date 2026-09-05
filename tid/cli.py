from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Sequence

import httpx

from . import config
from . import sources as source_types
from .extract import extract
from .http import client_context
from .llm import LLMBackend, make_backend
from .render import build_pdf
from .sources import RawItem
from .store import ArticleRow, Store, norm_title, open_store
from .topics import Topic, propose_topics, select_main, select_topic, standing_topics
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


def _load_topics(path: Path) -> list[Topic]:
    """The standing sections configured beside the sources."""
    return standing_topics(config.load_topics(path))


# Network-bound article extraction: many in flight is fine, but not unbounded
# — trafilatura's parse runs in a thread and the pool is finite.
FETCH_CONCURRENCY = 8


# --- stages -----------------------------------------------------------------

async def _fetch_source(
    client: httpx.AsyncClient, src: dict
) -> list[RawItem] | None:
    """Resolve one source config to its raw items. None on failure.

    Which items, and how, is the source type's business (`tid.sources`); this
    only turns its failures into a logged skip so one dead feed does not end
    the gather.
    """
    try:
        return await source_types.fetch(client, src)
    except LookupError as e:
        _log(f"  [warn] {e}")
        return None
    except Exception as e:
        _log(f"  [error] fetch failed: {e}")
        return None


async def _file(store: Store, it: RawItem, **fields) -> None:
    """Write one gathered item to the store, with everything the fetch knew
    about it. The source type's `kind` and `extra` ride along on every write,
    including the back-fill of an item already held."""
    await store.insert_raw(
        it.source, it.url, it.title,
        surfaced=it.surfaced, kind=it.kind, extra=it.extra,
        **{"text": None, "image": it.image, **fields},
    )


async def cmd_gather(
    client: httpx.AsyncClient,
    store: Store,
    sources: list[dict],
    concurrency: int = FETCH_CONCURRENCY,
) -> int:
    new_count = 0
    failed_count = 0
    dup_count = 0
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
        seen_here: set[str] = set()
        for it in items:
            if await store.exists(it.url):
                # Back-fill on a re-gather: insert_raw is a no-op insert for a
                # URL we hold, plus whatever the row lacks — the surfacing
                # date, the image, the source type's own fields.
                await _file(store, it)
                continue
            norm = norm_title(it.title)
            if norm in seen_here or await store.exists(it.url, it.title):
                # The same story under another URL. Skip it outright: the
                # old code ran the back-fill insert here too, which filed a
                # textless row under the *new* URL — a story that never
                # reached extraction and never counted as unreadable.
                dup_count += 1
                _log(f"  = {it.title[:70]}  (already have this story)")
                continue
            if norm:
                seen_here.add(norm)
            todo.append(it)

        results = await asyncio.gather(
            *(_extract_one(it) for it in todo), return_exceptions=True
        )
        for it, res in zip(todo, results):
            if isinstance(res, BaseException):
                _log(f"  [error] extract: {it.title[:60]}: {res}")
                await _file(store, it)
                failed_count += 1
                continue
            _, art = res
            if art is None:
                await _file(store, it)
                failed_count += 1
                _log(f"  - {it.title[:70]}  (no readable content)")
            else:
                # Prefer the article's own date; fall back to the surfacing
                # date so we always have something to display.
                await _file(
                    store, it,
                    text=art.text,
                    published=art.published or it.surfaced,
                    # The feed's own choice wins: it names the image the
                    # publisher attached to *this* article, where og:image is
                    # often a section banner or a site-wide logo.
                    image=it.image or art.image,
                )
                new_count += 1
                _log(f"  + {it.title[:70]}  ({len(art.text)} chars)")
    _log(f"[gather] +{new_count} new, {failed_count} unreadable, "
         f"{dup_count} duplicate")
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
    retries: int = 0,
    fallback: str | None = None,
) -> int:
    """Shared driver for the summarize and rewrite stages: chunk the pending
    rows, run `batch_fn` over each chunk with at most `workers` in flight, and
    store each batch's results as soon as it lands.

    Storing per batch rather than after the whole stage matters on a local
    model, where one stage runs for tens of minutes: a crash at article 43
    must not throw away 42 articles' worth of GPU time. Every stage is
    resumable — whatever was stored is simply not pending next time.

    A row the model failed on — the call raised, or the reply did not label
    it — is sent again up to `retries` more times, in a batch of only the
    failures. Whatever is still unusable after that is stored as `fallback`
    when one is given, so the row stops being pending; with no fallback it is
    left for the next run.
    """
    batches = _chunks_by_source(pending, batch_size)
    _log(f"[{stage}] {len(pending)} pending in {len(batches)} batch(es) "
         f"of {batch_size} (workers={workers})")
    sem = asyncio.Semaphore(workers)
    finished = 0

    async def _attempt(rows: list[ArticleRow]) -> list[str]:
        """One LLM call. A raised call counts as every row unusable, so the
        retry loop treats a timeout and an unparseable reply the same way."""
        try:
            return await batch_fn([(r.title, r.text or "") for r in rows])
        except Exception as e:
            _log(f"  [error] batch ({len(rows)} articles): {e}")
            return [""] * len(rows)

    async def _one(rows: list[ArticleRow]) -> tuple[int, int]:
        nonlocal finished
        done = 0
        todo = list(rows)
        async with sem:
            started = time.perf_counter()
            for attempt in range(retries + 1):
                if attempt:
                    _log(f"  ↻ retry {attempt}/{retries} for {len(todo)} "
                         f"article(s)")
                out = await _attempt(todo)
                failed: list[ArticleRow] = []
                for row, value in zip(todo, out):
                    if value:
                        await apply_fn(row.id, value)
                        done += 1
                    else:
                        failed.append(row)
                todo = failed
                if not todo:
                    break
            elapsed = time.perf_counter() - started
        if todo and fallback is not None:
            # Every attempt failed on these. File the placeholder so they stop
            # being re-sent on each ingest and the paper says what happened.
            for row in todo:
                await apply_fn(row.id, fallback)
        # Without a fallback the leftovers stay pending rather than storing
        # junk; the next run picks them up.
        errors = len(todo)
        finished += 1
        # Logged as each batch lands, not after the gather: a silent terminal
        # for twenty minutes is indistinguishable from a hang.
        _log(f"  ✓ batch of {len(rows)} "
             f"({finished}/{len(batches)}, {elapsed:.1f}s, {errors} unusable"
             f"{f', filed as {fallback!r}' if errors and fallback else ''})")
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
    from .summarize import NO_SUMMARY, SUMMARY_RETRIES, summarize_batch

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
        retries=SUMMARY_RETRIES,
        fallback=NO_SUMMARY,
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


async def cmd_topics(
    store: Store,
    backend: LLMBackend,
    workers: int | None = None,
    standing: Sequence[Topic] = (),
) -> int:
    """Decide what today's edition is about, and file every article into it.

    Three passes over the articles that are ready and unpublished — which is
    exactly what the next edition will carry:

      1. one call naming the edition's topics, most significant first — on
         top of the `standing` sections from the config, which are always in
         the set (`tid.topics.merge_topics`);
      2. one call per article choosing its most specific topic;
      3. one call per topic naming that topic's main stories, in order.

    Runs over the whole unpublished set every time, not over "articles missing
    a topic". A topic set describes one edition: an article that was filed
    yesterday and still has not been published belongs in *this* paper's
    sections, under this paper's names. So every row is rewritten, including
    back to unfiled when the model cannot place it.

    Failure at any pass costs the layout, never the edition: unfiled articles
    fall back to their sources.toml section (`tid.edition`), so a paper still
    comes out.
    """
    rows = await store.pending_render()
    if not rows:
        _log("[topics] nothing pending")
        return 0
    # Newest first: `topic_max_articles` truncates the naming prompt on a small
    # model, and the day's newest stories are the ones that should shape the
    # sections.
    rows.sort(key=lambda r: r.sort_date, reverse=True)

    topics = await propose_topics(
        backend, [(r.title, r.summary or "") for r in rows], standing
    )
    if not topics:
        _log(f"[topics] the model named no usable topics for {len(rows)} "
             "article(s); the edition falls back to sources.toml sections")
        return 0
    _log(f"[topics] {len(rows)} article(s) → "
         + ", ".join(t.name for t in topics))
    if standing:
        fixed = {t.name for t in standing}
        own = [t.name for t in topics if t.name not in fixed]
        _log(f"  · {len(standing)} standing section(s); the day's own: "
             + (", ".join(own) if own else "none"))

    workers = _resolve_workers(backend, workers)
    sem = asyncio.Semaphore(workers)

    async def _label(row: ArticleRow) -> str:
        async with sem:
            return await select_topic(
                backend, row.title, row.summary or "", topics
            )

    labels = await asyncio.gather(
        *(_label(r) for r in rows), return_exceptions=True
    )
    filed: dict[str, list[ArticleRow]] = {t.name: [] for t in topics}
    unfiled: list[ArticleRow] = []
    for row, label in zip(rows, labels):
        if isinstance(label, BaseException):
            _log(f"  [error] filing {row.title[:60]}: {label}")
            unfiled.append(row)
        elif label in filed:
            filed[label].append(row)
        else:
            unfiled.append(row)

    async def _mains(name: str, group: list[ArticleRow]) -> list[int]:
        async with sem:
            return await select_main(
                backend, name, [(r.title, r.summary or "") for r in group]
            )

    names = [t.name for t in topics if filed[t.name]]
    chosen = await asyncio.gather(
        *(_mains(name, filed[name]) for name in names), return_exceptions=True
    )

    mains: dict[str, list[int]] = {}
    for name, res in zip(names, chosen):
        if isinstance(res, BaseException):
            _log(f"  [error] main stories for {name}: {res}")
            mains[name] = []
        else:
            mains[name] = res

    for order, topic in enumerate(topics):
        group = filed[topic.name]
        picks = mains.get(topic.name, [])
        for i, row in enumerate(group):
            rank = picks.index(i) if i in picks else None
            await store.set_topic(row.id, topic.name, order, rank)
        if group:
            _log(f"  · {topic.name}: {len(group)} article(s), "
                 f"{len(picks)} main")
    for row in unfiled:
        # Clear rather than leave: the row may still carry a topic from the
        # previous edition, and a name this edition never chose would open a
        # column of one article.
        await store.set_topic(row.id, None, None, None)
    if unfiled:
        _log(f"  · unfiled: {len(unfiled)} article(s) keep their "
             "sources.toml section")
    return 0


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


async def collect_current_edition(
    store: Store, sources: list[dict], floor: str | None = None
) -> list[dict]:
    """Every ready article no edition has carried yet, in source config order.
    Returns render-ready dicts.

    There is no per-source cap. An edition is bounded by what is new, not by
    how many stories a feed happened to file: whatever the last sync brought
    in, the paper carries, and every one of those articles gets its own page.

    An article runs in the first edition published after it becomes ready —
    which is not the same as the first one after it was gathered, because
    summarizing and rewriting a whole feed takes longer than one edition's
    worth of patience. `floor` is only for the case where nothing has ever been
    published; `jobs.build_edition_for_key` gets it from `archive.floor`.
    """
    out: list[dict] = []
    for src in sources:
        name = src["name"]
        rows = await store.unpublished(name, floor)
        for r in rows:
            out.append({
                "id": r.id,
                "source": r.source,
                # What the topic stage decided this edition is about. `section`
                # below is the fallback for anything it did not file.
                "topic": r.topic,
                "topic_order": r.topic_order,
                "main_rank": r.main_rank,
                # Layout hints that live in sources.toml, carried on every
                # article so the edition builder never needs the config again.
                "section": src.get("section") or name,
                # `medium` is read/watch/listen (how you consume it), not
                # `kind`, which is rss/hn (how we fetch it).
                "medium": src.get("medium") or "read",
                # The source type, and its own fields. A row from before the
                # columns existed carries neither; the config still knows the
                # kind, so the byline is right even before a re-gather fills
                # the row in.
                "kind": r.kind or source_types.kind_of(src),
                "extra": r.extra,
                "url": r.url,
                "title": r.title,
                "text": r.body or r.text,
                "summary": r.summary,
                "image": r.image,
                "date": _format_date(r.published or r.surfaced),
                "iso_date": r.published or r.surfaced or r.fetched_at[:10],
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
    standing: Sequence[Topic] = (),
) -> int:
    """gather + summarize + rewrite + topics. No PDF — that's the renderer's job.

    Topics run last because they read the finished set: only an article that
    has a summary and a rewritten body will be in the next edition, and the
    topic set is a judgement about that edition rather than about each article.
    """
    rc = await cmd_gather(client, store, sources)
    if rc:
        return rc
    rc = await cmd_summarize(store, backend, workers)
    if rc:
        return rc
    rc = await cmd_rewrite(store, backend, workers)
    if rc:
        return rc
    return await cmd_topics(store, backend, workers, standing)


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
    # Defaults come from the environment, not from literals: inside the
    # container TID_STATE and TID_CONFIG are what point at /data and the
    # mounted sources.toml, and a one-off `tid topics` there has to reach the
    # same store the web process is serving from — not create an empty
    # state.db in the working directory.
    p.add_argument("--config", type=Path, default=config.config_path())
    p.add_argument("--out",    type=Path, default=Path("archive"))
    p.add_argument("--state",  type=Path, default=config.state_path(),
                   help="SQLite file (shorthand for --store); "
                        "default: $TID_STATE, else state.db")
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

    sp_top = sub.add_parser(
        "topics",
        help="name the edition's topics and file every article into them")
    sp_top.add_argument("--workers", type=int, default=None)

    sp_ing = sub.add_parser(
        "ingest", help="gather + summarize + rewrite + topics (no PDF)")
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
    try:
        standing = _load_topics(args.config)
    except ValueError as e:
        _log(f"[fatal] {args.config}: {e}")
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
            if cmd == "topics":
                return await cmd_topics(store, backend, workers, standing)
            if cmd == "ingest":
                return await cmd_ingest(
                    client, store, backend, sources, workers, standing
                )
            if cmd == "render":
                return await cmd_render(
                    client, store, backend, args.date, args.out, sources
                )
            if cmd == "build":
                rc = await cmd_ingest(
                    client, store, backend, sources, workers, standing
                )
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
