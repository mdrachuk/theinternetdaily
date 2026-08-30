# The Internet Daily — plan

**This repo stays single-user, open-source, and self-hostable.** It also
becomes a *library*: a separate downstream repo will import it and package the
same functionality for multi-user flows (auth, tenants, per-user schedules,
whatever else a hosted product needs).

That reframing means the work is no longer "grow this into a product". It is:
modernise the runtime, run the model locally, and **carve clean seams** so the
downstream repo can wrap the pipeline without forking it.

| # | Step | Status |
|---|------|--------|
| 1 | migrate runtime to uv | **done** |
| 2 | migrate to async python | **done** |
| 3 | pluggable storage + job queue (sqlite default, mongo/redis as extras) | **done** |
| 4 | switch the llm backend to vLLM, run Gemma on this server | **done** |
| 5 | test the setup end to end | **done** — see the outcome under step 5 |
| 6 | library API hardening — kill global state, define the seams | todo |
| 7 | web app as a library consumer (FastAPI, single-user) | todo |
| 8 | generate papers in workers, bounded LLM concurrency | todo |
| 9 | configurable generation time (single-user) | todo |

## Settled decisions

- **Web stack**: FastAPI + Jinja2, server-rendered, uvicorn. No JS build step.
- **LLM**: vLLM serving `google/gemma-3-12b-it` in FP8 over its
  OpenAI-compatible API, with a benchmark gate before committing (step 5).
- **Scope**: single-user. No auth, no tenants, no user table in this repo.
- **Multi-tenancy strategy**: the library never knows about tenants. It is
  *instance-scoped* — an `InternetDaily` object owns one config + one store. The
  downstream repo achieves multi-user by constructing one instance per user.
  This is the cheapest possible seam and it keeps this repo honestly
  single-user.

## Sequencing

2 → 3 → 6 → 7 → 8 → 9 is the chain. **Step 4 is independent** and has a
hardware prerequisite that may need a reboot window, so start it in parallel
with step 2. Step 6 is the pivot: everything after it is written against the
library API rather than against module globals.

---

## Step 2 — async python

Goal: every I/O path is `async`, `ThreadPoolExecutor` disappears, and the
public API is async-first (which is what the downstream service needs).

**Networking**
- `requests` → a shared `httpx.AsyncClient`, *owned by the caller* (see step 6
  — no module-level client), with per-host connection limits, a global
  timeout, and a real User-Agent. Touches `fetch.py`, `wiki.py`.
- `feedparser.parse(url)` fetches on its own blocking path. Fetch bytes with
  httpx, then `await asyncio.to_thread(feedparser.parse, data)`.
- `trafilatura.fetch_url` likewise → httpx GET, then `trafilatura.extract` +
  `extract_metadata` inside `asyncio.to_thread` (CPU-bound HTML parsing —
  keep it off the event loop). `extract.py`.

**LLM**
- `llm.py`: `httpx.stream` → `httpx.AsyncClient.stream`; `anthropic.Anthropic`
  → `AsyncAnthropic`. `chat()` becomes `async def chat()`.
- `summarize_batch` / `rewrite_batch` become async.

**Orchestration**
- `cli.py`: drop `ThreadPoolExecutor`/`as_completed`; batches run under
  `asyncio.gather` with an `asyncio.Semaphore(workers)`. `main()` becomes a
  thin `asyncio.run(_main())` wrapper so `[project.scripts]` still works.

**Subprocesses**
- `render.py` (xelatex) and `preview.py` (pdftoppm) →
  `asyncio.create_subprocess_exec` with `wait_for` timeouts. Today a hung
  xelatex blocks a gunicorn thread; on an event loop it would block everything.

**Web**
- `web.py`: Flask → FastAPI. `send_file` → `FileResponse`,
  `threading.Lock` → `asyncio.Lock`, gunicorn → uvicorn in the Dockerfile CMD.
  APScheduler → `AsyncIOScheduler` for now; revisited in step 8.

**Store**: stays synchronous SQLite for this step. Its interface changes in
step 3 anyway, so wrapping it in `to_thread` is throwaway work.

**Tests**: move from `unittest` to `pytest` + `pytest-asyncio`; rewrite
`tests/test_web_features.py` against `httpx.ASGITransport`.
`test_render.py` / `test_since_hours.py` are mostly pure functions and survive
with minimal edits.

**Exit criteria**: `tid build` produces the same PDF from the same
store; `pytest` green; no `requests` or `ThreadPoolExecutor` left.

---

## Step 3 — pluggable storage + job queue

The original plan was "migrate to MongoDB + Redis". Under the library framing
that is the wrong default: a single-user self-hoster should not have to run
two servers to read a newspaper, but the downstream multi-user service will
absolutely want both. So: **define the backend protocols here, ship SQLite as
the default, and ship Mongo/Redis in-repo as optional extras** so the
downstream imports them instead of reimplementing them.

*(If you'd rather this repo be Mongo-only, say so — it collapses this step to
a straight port and deletes the protocol layer, at the cost of a heavier
self-host story.)*

**Storage protocol** — `tid/store/base.py`
```python
class Store(Protocol):
    async def exists(self, url: str, title: str) -> bool: ...
    async def insert_raw(self, ...) -> None: ...
    async def pending_summary(self) -> list[ArticleRow]: ...
    async def set_summary(self, article_id: str, summary: str) -> None: ...
    async def pending_rewrite(self) -> list[ArticleRow]: ...
    async def set_body(self, article_id: str, body: str) -> None: ...
    async def ready_since(self, source, since) -> list[ArticleRow]: ...
    async def max_fetched_at(self) -> str: ...
    async def counts(self) -> dict[str, int]: ...
```
- Rows become a typed `ArticleRow` dataclass, not `sqlite3.Row`. `cli.py`
  currently does `r["body"]`-style access in several places; that coupling has
  to go for any non-SQLite backend to work.
- `tid/store/sqlite.py` — the current implementation, made async via
  `to_thread` (or `aiosqlite`). **Default, zero extra services.**
- `tid/store/mongo.py` — behind `pip install tid[mongo]`, using
  `pymongo`'s `AsyncMongoClient` (Motor is EOL — do not add it). Indexes:
  unique `url_hash`, `title_norm`, `(source, sort_date)`, partial indexes on
  `summary: null` / `body: null`. Denormalize a `sort_date` field at insert
  time rather than porting the `COALESCE(published, surfaced, fetched_at)`
  ordering into an aggregation.
- No `tenant_id` anywhere. The downstream gets isolation by pointing each
  user's instance at its own database/collection prefix — which the Mongo
  store takes as a constructor argument.

**Queue protocol** — `tid/queue/base.py`
```python
class JobQueue(Protocol):
    async def enqueue(self, job: str, *args, job_id: str | None = None) -> None: ...
```
- `tid/queue/local.py` — in-process `asyncio` execution with a
  semaphore. **Default**; a single-user box does not need Redis.
- `tid/queue/arq_queue.py` — `arq` + Redis behind `tid[redis]`,
  for the downstream and for anyone who wants durable jobs. Idempotency via
  arq's `_job_id`.

**Compose**: keep the default `docker-compose.yml` as one service. Add
`docker-compose.mongo.yml` / `docker-compose.redis.yml` overlays for the
optional backends, so `docker compose up` stays a one-liner.

**Migration**: `tid migrate --from sqlite --to mongo` as a store-to-store
copy through the protocol — useful to the downstream too.

**Exit criteria**: the same test suite passes against both stores (one
parametrized fixture); default `docker compose up` still needs no extra
services.

---

## Step 4 — vLLM + Gemma on this box

**Hardware (verified on this machine)**
- GPU: **NVIDIA RTX 4000 SFF Ada Generation** — 20 GB GDDR6, SM 8.9, 70 W.
  Ada means native FP8.
- 20 cores, 62 GB RAM, 1.1 TB free on `/`.
- **Blocker**: the loaded kernel module is `580.159.03` but userspace libs are
  `580.173.02`, so `nvidia-smi` fails with a version mismatch. Fix first —
  reboot (uptime is 201 days, so one is due) or unload/reload the `nvidia*`
  modules. Nothing GPU-related works until `nvidia-smi` does.
- `nvidia-container-toolkit` 1.18.1 is installed; still needs
  `nvidia-ctk runtime configure --runtime=docker` + a Docker restart, then a
  throwaway CUDA container to verify.

**Model access**: `google/gemma-3-12b-it` is gated — accept the licence on
Hugging Face, put `HF_TOKEN` in `.env`, mount `./data/hf` as the cache so the
~25 GB download happens once.

**vLLM service** (in `docker-compose.vllm.yml`, so the default compose stays
CPU-only and portable):
```
vllm  (vllm/vllm-openai, GPU via deploy.resources.devices,
       --model google/gemma-3-12b-it --quantization fp8
       --kv-cache-dtype fp8 --max-model-len 16384
       --max-num-seqs 4 --gpu-memory-utilization 0.90)
```
FP8 weights are ~12 GB, leaving ~6–7 GB of KV cache — that is what forces the
modest `max-model-len` / `max-num-seqs`. Step 5 tunes these.

**Client** — `llm.py`
- Add a `vllm` backend POSTing to `/v1/chat/completions` (streaming) with
  httpx. No `openai` SDK needed. Config: base URL, model, timeout.
- Keep `anthropic` — needed for the quality diff in step 5, and it is the
  backend most OSS users will actually have.
- Drop the `ollama` branch (or keep it — it is 25 lines and is the easiest
  local option for users without a GPU; **recommend keeping it**, since this
  repo is now explicitly for self-hosters).
- Formalise `LLMBackend` as a protocol here rather than in step 6 — it is the
  single most likely thing a downstream wants to swap.

**Batching must be re-tuned** — the part most likely to bite:
- `rewrite.py` currently sends 8 articles × 16 000 chars asking for
  `4096 × 8 = 32 768` output tokens: ~60k tokens per call, far past a 16k
  `max-model-len`. `_BATCH_SIZE` / `_MAX_CHARS` become per-backend config —
  local Gemma gets rewrite batches of 1–2, summarize batches of 4–8.
- App-side semaphore sized to `max_num_seqs`; vLLM does its own continuous
  batching, so oversubscribing just queues.

**Output robustness**: the batch protocols are positional text markers
(`N. `, `=== ARTICLE N START ===`) and a 12B model breaks them more often than
Haiku does. Move both to JSON with vLLM guided decoding
(`guided_json` / `response_format`), keeping the current regex parsers as the
fallback path. Smaller batches also shrink the blast radius of one bad reply.

**Exit criteria**: `nvidia-smi` clean; a full ingest with zero Anthropic
calls; a sample PDF built entirely locally.

---

## Step 5 — test the setup

- **Benchmark**: tokens/s, peak VRAM, wall-clock per stage on a real
  ~60-article ingest. Record in the README.
- **Quality diff**: the same 20 articles through `claude-haiku-4-5` and Gemma,
  side by side. Watch specifically what `render.py` depends on: fenced code
  blocks, inline backticks, untouched LaTeX math delimiters. A model that
  mangles `$...$` produces broken PDFs, not merely worse prose.
- **Decision gate**: if 12B FP8 does not fit, is too slow at 70 W, or degrades
  code/math handling → fall back to `gemma-3-4b-it` or cut `max-model-len`.
  Record the outcome here.
- **Regression tests**: fixture-based tests for the batch parsers (including
  malformed responses) and an offline end-to-end test with a fake LLM backend,
  so CI never needs a GPU.

**Exit criteria**: documented numbers, a recorded go/no-go on 12B, green tests
that touch no network.

### Outcome — 2026-08-08

**Go on 12B.** It fits with room to spare, and the failures that did turn up
were ours, not the model's.

Model served: **`google/gemma-4-12B-it-qat-w4a16-ct`**, not the planned
`gemma-3-12b-it` FP8 — it was already in the host HF cache, so it cost no
25 GB download, and 4-bit QAT weights are smaller than FP8 ones. Everything
below is on vLLM 0.26.0, `--max-model-len 16384 --max-num-seqs 4
--kv-cache-dtype fp8`, on the RTX 4000 SFF Ada at 70 W.

| | planned (FP8 12B) | measured (w4a16 QAT 12B) |
|---|---|---|
| weights | ~12 GB | **8.29 GiB** |
| KV cache | ~6–7 GB | **8.89 GiB = 139 374 tokens** |
| peak VRAM | — | **18.4 / 20.0 GiB** |
| concurrency at 16k ctx | — | **8.5x** (we use 4) |

Full edition, 44 articles, zero Anthropic calls (there is no API key on the
box): gather 61 s, summarize 38 s, rewrite 807 s, decorations ~6 s, xelatex
~7 s for 142 pages — **~15 minutes end to end**. Aggregate output throughput
was 60 tok/s on summarize and 83 tok/s on rewrite; per-stream it is ~25 tok/s,
so concurrency is doing real work.

Quality: 0 empty summaries, 0 empty bodies, 0 `tex_body` failures, median
summary 31 words. No article lost math; 3 of 44 *gained* correct delimiters
where `trafilatura` had flattened them.

**The quality diff against Haiku was not run** — there is no Anthropic key on
this box. `scripts/quality_diff.py` grew a `--columns` flag so a single
backend (or the stored output of an ingest) can be scored on its own; the
Haiku column is a one-command run whenever a key exists.

Two real bugs came out of this, both invisible until a real edition was built:

1. **JSON escaping destroys LaTeX.** `{"body": "… A \frac{N}{D} …"}` is valid
   JSON — `\f` is a form feed — so the body reached xelatex as `A ^^Lrac{…}`
   and the build died. Only `\f \b \n \r \t` are exposed (every other LaTeX
   command is not a legal JSON escape, so guided decoding forces a doubled
   backslash). Repaired in `tid/protocol.py`, with the mirror-image case
   — a double-escaped line break printing a literal `\n` mid-sentence — handled
   too, outside code spans.
2. **The rewrite output budget was a flat 4096 tokens.** Rewriting is ~1:1, so
   the longest articles were truncated mid-reply; a truncated batch protocol
   parses to nothing, so those articles stayed pending forever while burning a
   full GPU call each run. The budget now comes from the backend
   (`rewrite_output_tokens`), and a `finish_reason=length` is logged.

Tuning left on the table, deliberately, since the numbers are already fine:
`--max-num-seqs` could go to 8 and `--max-model-len` well past 16k on this KV
cache.

## Step 6 — library API hardening

The pivot. Right now the package is a CLI with importable helpers: the
downstream would have to import `tid.cli._collect_current_edition` and
`tid.cli._gather_decorations` (both private, both what `web.py` already
does) and set environment variables to configure anything. That is not a
library.

**Kill global and import-time state**
- `llm.py` reads `LLM_BACKEND` at import (`_BACKEND = os.environ.get(...)` at
  module level) — a downstream cannot serve two backends, or change one
  without reimporting.
- `web.py` runs `app = create_app()` and `start_scheduler()` at import time,
  and reads six paths/env vars at module scope. Importing the package must not
  start a scheduler.
- Every `os.environ.get` moves into one `Config` object (pydantic-settings,
  constructed from env *by the CLI*, constructed explicitly by a downstream).

**Public API** — `tid/__init__.py` (currently empty)
```python
from tid import InternetDaily, Config, SourceConfig, Edition, Article

pn = InternetDaily(config=Config(...), store=..., llm=..., queue=...)
await pn.ingest()                    # gather + summarize + rewrite
edition = await pn.current_edition() # the render-ready article set
pdf     = await pn.render(edition, out_dir)
png     = await pn.preview(pdf)
```
`_collect_current_edition` and `_gather_decorations` become public,
documented methods. `cli.py` and `web.py` become *consumers* of this API and
nothing else — if they still need a private import, the API is incomplete.

**Seams the downstream will need** (each a Protocol + a default impl):
| Seam | Why the downstream wants it |
|---|---|
| `Store` | per-user databases (step 3) |
| `JobQueue` | Redis-backed, shared workers (step 3) |
| `LLMBackend` | routing, quotas, a different model per plan (step 4) |
| `SourceFetcher` registry | add source kinds without forking `fetch.py`'s `if kind == ...` chain |
| `Delivery` | today a `POST_INGEST_HOOK` subprocess; should be an interface, with the subprocess hook as one impl |
| `Renderer` / template dir | per-user branding, alternate layouts |
| `Clock` | testable scheduling, and step 9 |

**Typed config instead of dicts**: sources are raw `dict`s threaded through
`cli.py`, `cache.py`, and `web.py` with `.get("since_hours")` scattered about.
A `SourceConfig` dataclass with validation, plus `SourceConfig.from_toml`,
makes the downstream's own config UI trivial and kills a class of bug.

**Packaging**
- Optional extras: `tid[mongo]`, `[redis]`, `[anthropic]`, `[vllm]`,
  `[web]`. Core install should not pull Flask/FastAPI for someone who only
  wants the CLI.
- Ship `py.typed`, run `mypy`/`pyright` in CI.
- Semantic versioning starting at `0.2.0`, a CHANGELOG, and a documented
  stability promise for the names in `__init__.py`.
- **`requires-python = ">=3.14"` is worth reconsidering** — it is the single
  biggest barrier to anyone consuming this as a library. Nothing in the code
  needs 3.14. Recommend `>=3.12`.
- `logging` instead of `_log()`'s direct stderr writes; a library must not own
  the process's stderr. No `sys.exit` outside `cli.py`.
- An `examples/` dir and a `docs/library.md` showing the downstream's exact
  use case: N instances, one per user, sharing a queue.

**Exit criteria**: a smoke test that imports only public names, builds a PDF
with an in-memory store and a fake LLM, and never reads an env var.

---

## Step 7 — web app as a library consumer

Single-user, no auth. Same content in the browser as in the PDF, plus
configuration.

**Rendering the body is the real work.** The rewritten body is markdown-lite
(triple-backtick fences, inline backticks, LaTeX math) and `render.py` has
~150 lines of careful tokenizing to turn it into LaTeX. The browser needs the
same tokenizer emitting HTML. Do **not** write a second parser — lift the
stash/expand logic into `tid/markup.py` with two emitters (`to_tex`,
`to_html`), or the PDF and the page drift apart on exactly the inputs that are
hardest to get right. Math via vendored KaTeX (offline, no CDN). `markup.py`
is also a useful public export for the downstream.

**Templates** (Jinja2): `base`, `index` (today's edition grouped by source, in
config order), `article`, `archive`, `settings/sources`, `settings/schedule`.

**Routes**
```
GET  /                  today's edition
GET  /a/{article_id}    single article
GET  /archive           past editions
GET  /digest.pdf        same edition as PDF
GET  /preview.png       cover preview
GET  /settings/sources  add/remove/reorder, kind, url, section, medium,
                        since_hours, min_points (validated via SourceConfig)
POST /settings/sources  save + invalidate the edition cache
GET  /settings/schedule generation time (step 9)
POST /ingest            manual kick (already exists)
```
Sources move from a read-only `sources.toml` to editable config — write back
to the TOML file (keeps the file the source of truth and stays diffable/
git-able, which suits a self-hosted tool) rather than into the store.
`cache.edition_key` already keys on the source config, so edits invalidate
correctly.

**Optional and cheap**: read state (mark as read; hide read items from the web
view but keep them in the PDF).

**Exit criteria**: read today's paper in a browser, edit sources, see the next
edition reflect it, download identical content as PDF — with `web.py` importing
only public API.

---

## Step 8 — workers, bounded LLM concurrency

The constraint is real regardless of user count: one 70 W card cannot run many
LLM jobs at once, and the downstream will have many more of them queued.

**Default (single-user)**: in-process `asyncio` workers with an
`asyncio.Semaphore` sized to vLLM's `max_num_seqs`. No Redis required.

**Optional (`tid[redis]`, and what the downstream uses)**: separate arq
worker processes per queue — process-level `max_jobs` is what actually
guarantees the GPU is not oversubscribed, where a semaphore in a shared
process does not:
```
worker-ingest   queue=ingest, max_jobs=20   (network-bound)
worker-llm      queue=llm,    max_jobs=1-2  (matched to vLLM max_num_seqs)
worker-render   queue=render, max_jobs=2    (xelatex, CPU-bound)
```

**Job graph** (identical under both queue impls — that is the point of the
protocol):
```
run()
  └─ fetch_source(source)      [ingest]  fan-out per source
       └─ extract_article(url) [ingest]  fan-out per article
            └─ summarize / rewrite   [llm]  batched
                 └─ render_edition   [render]
                      └─ deliver     [ingest]
```
- Idempotency by job id; retries with exponential backoff; generous
  `job_timeout` on LLM jobs (a 12B model at 70 W is slow).
- Permanent failures recorded with their error and surfaced in the web UI, so
  a user can see why their paper is thin.
- Backpressure: if a run is already in flight, skip rather than stack.
- APScheduler is deleted here; scheduling moves to step 9's `Clock`/scheduler
  seam.

**Exit criteria**: a full run never exceeds the LLM concurrency cap (verified
against vLLM's running-sequence metric); a killed worker resumes without
repeating LLM work.

---

## Step 9 — configurable generation time

Single-user, so this stays small — but built on a seam the downstream can
replace with per-user schedules.

- Config: `generation_times` (list of `"HH:MM"`, allowing a morning and an
  evening edition), `timezone` (IANA), `lead_minutes`.
  `INGEST_SCHEDULE` / `INGEST_TIMEZONE` already exist and mostly do this —
  formalise them into `Config` and expose them in the settings UI.
- Compute the next run with `zoneinfo` at each tick, never by precomputing UTC
  offsets — that breaks across DST.
- **Lead time**: a full run takes minutes, so start at
  `generation_time - lead_minutes` and have the paper *ready* at the requested
  time rather than starting then. Seed `lead_minutes` from the previous run's
  measured duration.
- Guard against two papers a day for the same slot with a run-log entry keyed
  by `(local_date, slot)`.
- The scheduler itself is a `Scheduler` protocol over the `Clock` seam: the
  default reads one config; the downstream's implementation iterates users.
  This repo never learns what a user is.

**Exit criteria**: configured times fire correctly across a DST transition
(fake-clock test); the settings page round-trips the config.

---

## What this repo deliberately does *not* get

Owned by the downstream multi-user repo, and each has a named seam above:

| Downstream concern | Seam it plugs into |
|---|---|
| Auth, sessions, users | none — it wraps the library, one instance per user |
| Tenant isolation | `Store` constructor (per-user db/prefix) |
| Per-user schedules | `Scheduler` / `Clock` |
| Shared GPU across users | `JobQueue` (arq) + `LLMBackend` |
| Billing, quotas, rate limits | `LLMBackend` wrapper |
| Email/push delivery | `Delivery` |
| Branding per user | `Renderer` / template dir |

If any of these needs a change *inside* `tid` to work, that is a bug in
the seam — fix the seam, don't add the feature here.

---

## Cross-cutting

- **Config**: one `pydantic-settings` module replacing ~15 scattered
  `os.environ.get` calls. Env is read by `cli.py`, not by library modules.
- **Health**: `/healthz` (liveness) and `/readyz` (pings the store, queue, LLM).
- **Logging**: `logging` with structured extras (`job_id`, `source`), never
  direct stderr writes from library code.
- **Testing**: `pytest` + `pytest-asyncio`; store tests parametrized across
  backends; a fake LLM backend so CI needs no GPU; a public-API smoke test
  that asserts nothing private is required.
- **CI**: lint (ruff), types (mypy), tests, and a build of the wheel — a
  library that does not build is not a library.
- **Docs**: the README documents Flask, SQLite, Ollama, gunicorn and
  single-file config throughout; it needs a full pass, plus a new
  `docs/library.md` for downstream consumers.
- **Rollout**: every step leaves a working `docker compose up` and a working
  `tid build`.
