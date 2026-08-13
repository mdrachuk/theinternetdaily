# The Internet Daily

![The Internet Daily on a reMarkable, next to a cup of coffee](assets/hero.jpg)

Every news site looks different. Hacker News, MacRumors, Quanta, my
favourite ML blog, my favourite math blog — each one its own layout, fonts,
colors, ads. To read anything I had to wade through somebody's design
choices first and focus past the visual noise.

I much prefer reading the way a LaTeX paper or an old magazine looks: quiet
typography, generous margins, no color, nothing competing for attention.

**The Internet Daily** is the fix. A script pulls all those feeds, has Claude clean
up, translate to English, and rewrite the article bodies — the **full
text**, not just summaries — and renders the result into one consistently
typeset LaTeX PDF. Every article is *in* the PDF; you read entirely
offline, no clicking through, no opening tabs.

A side benefit I didn't expect to like but very much do: one place to read
the day's news instead of five tabs being refreshed all day. One or two
issues per day, no more.

Designed for an e-ink reader like the reMarkable, but it works just as well
in any browser's PDF viewer.

**👉 [See `sample-2026-06-04.pdf` for a real day's output.](sample-2026-06-04.pdf)**

## Status

Hobby project; works. Things will move. Expect rough edges.

## How to use

You need: a machine that can run Docker (your laptop, a NAS, a $5/mo VPS,
anything), an LLM backend (Anthropic API key **or** a local
[Ollama](https://ollama.com) instance), and ~2 GB of disk for the image.

```bash
# 1) Pull
git clone https://github.com/mdrachuk/theinternetdaily
cd theinternetdaily

# 2) Configure
cp .env.example .env
$EDITOR .env             # paste ANTHROPIC_API_KEY=sk-ant-... (or set LLM_BACKEND=ollama)

# 3) Pick your sources
$EDITOR sources.toml     # add/remove RSS/HN entries, set per-source limits

# 4) (Optional) Tweak the look
$EDITOR tid/template.tex.j2

# 5) Build + run
docker compose up --build -d

# Open http://localhost:8000
# First PDF builds on demand and is cached. Background ingest runs every 4h.
```

Everything you'd normally want to change is in **two files**:

- **`sources.toml`** — which feeds, how many items per feed, in what order.
  Two source kinds today: `kind = "hn"` (Hacker News, top-by-points via the
  Algolia API) and `kind = "rss"` (any Atom/RSS feed via feedparser).
- **`tid/template.tex.j2`** — the LaTeX template. Page size, fonts,
  colors, layout, what goes on the cover, everything. Edit, restart the
  container, refresh `/digest.pdf`.

Optional but useful:

- **`tid/summarize.py`** + **`tid/rewrite.py`** — the LLM
  system prompts. When using Anthropic, change `ANTHROPIC_MODEL` to
  `claude-sonnet-4-6` for fancier rewrites at ~10× the cost; adjust
  `_SYSTEM` to change the editorial voice (e.g. disable the
  auto-translate-to-English rule).
- **`tid/wiki.py`** — what goes into the World news block and the
  Quote-of-the-day source.

### Getting the PDF onto a reMarkable

A few different ways, no special script needed:

- **Manual** — open `http://your-machine:8000/digest.pdf` in a browser on
  your phone/laptop and upload it to your reMarkable from there (drag-and-
  drop on `my.remarkable.com`, or the reMarkable mobile app, or the USB Web
  Interface at `http://10.11.99.1` while connected by USB).
- **[`rmapi`](https://github.com/ddvk/rmapi)** — a third-party CLI that
  pushes files to your reMarkable cloud account. Pair once, then:
  ```bash
  curl -s http://your-machine:8000/digest.pdf -o today.pdf
  rmapi put today.pdf /TheInternetDaily
  ```
  Stick that two-liner in cron on the host and the device picks it up on
  next sync automatically.
- **[Remailable](https://github.com/remailable/remailable)** — a third-party
  email-to-reMarkable bridge ([remailable.getneutrality.org](https://remailable.getneutrality.org)).
  You email the PDF as an attachment to your assigned address and it appears
  on the device. Useful if your host can `mail`/`mutt` but can't
  reach the reMarkable directly. (reMarkable has no first-party
  email-to-device; do not believe earlier versions of this README that
  implied otherwise.)

No native push is built-in because everyone's setup is different and you
probably don't want me poking your reMarkable cloud account with your token.

## Quick start

```bash
git clone https://github.com/yourname/theinternetdaily
cd theinternetdaily
cp .env.example .env
# paste your ANTHROPIC_API_KEY into .env (get one at
# https://console.anthropic.com/settings/keys)
docker compose up --build
```

Then visit `http://localhost:8000` — landing page with a preview image and a
link to `/digest.pdf`. The first PDF builds on demand, takes ~1–2 minutes the
first time and is then cached until new content arrives.

State lives in `./data/state.db` (bind-mounted from the host) so it survives
container restarts.

## LLM backends

Every LLM call goes through an `LLMBackend` (`tid/llm.py`) — a protocol
with three implementations. Pick one with `LLM_BACKEND`, or pass `--backend` to
the CLI. Nothing is decided at import time, so one process can drive two
backends if it wants to.

Each backend carries its own batching limits, and that is not a detail: the
batch that is comfortable for Haiku (8 articles × 16 000 chars, asking for
32 768 output tokens — roughly 60k tokens) does not fit in a local model's
context window at all. `--workers` defaults to the backend's own concurrency
cap for the same reason.

### Anthropic (default)

```bash
# .env
LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Uses `claude-haiku-4-5` by default. Override with `ANTHROPIC_MODEL=claude-sonnet-4-6`
for higher quality at ~10× the cost.

### vLLM (local GPU)

Serves an open-weights model on your own card over vLLM's OpenAI-compatible
API. No API key, no per-token cost, nothing leaves the machine.

```bash
# .env
LLM_BACKEND=vllm
HF_TOKEN=hf_...                 # google/gemma-3-12b-it is a gated model
VLLM_MODEL=google/gemma-3-12b-it
```

```bash
docker compose -f docker-compose.yml -f docker-compose.vllm.yml up
```

Prerequisites on the host: an NVIDIA GPU with a working `nvidia-smi`, the
container toolkit wired into Docker (`nvidia-ctk runtime configure
--runtime=docker`, then restart Docker), and the model licence accepted on
Hugging Face. The ~25 GB of weights is cached in `./data/hf`, so the download
happens once.

Sizing on a 20 GB card (an RTX 4000 SFF Ada at 70 W is the reference here):
FP8 weights are ~12 GB, leaving ~6–7 GB of KV cache, which is what sets
`--max-model-len 16384` and `--max-num-seqs 4`. Ada has native FP8, so
`--quantization fp8` costs little quality. All of it is overridable:

| variable | default | what it does |
|---|---|---|
| `VLLM_MODEL` | `google/gemma-3-12b-it` | served model |
| `VLLM_MAX_MODEL_LEN` | `16384` | context window |
| `VLLM_MAX_NUM_SEQS` | `4` | server-side concurrency |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.90` | fraction of VRAM vLLM may use |
| `VLLM_TIMEOUT` | `1800` | client timeout; a 12B model at 70 W is slow |

Where the backend supports guided decoding — vLLM does, via
`response_format: json_schema` — the batch protocols switch from positional
text markers (`N. `, `=== ARTICLE N START ===`) to JSON, because a 12B model
breaks markers far more often than Haiku does. The marker parsers remain as the
fallback path, so a reply in either shape is understood.

### Ollama (local CPU or small GPU)

Kept because it is the easiest local option for anyone without a big GPU.

```bash
# .env
LLM_BACKEND=ollama
OLLAMA_HOST=http://your-ollama-host:11434   # default: http://localhost:11434
OLLAMA_MODEL=qwen2.5:3b                    # default: mistral
OLLAMA_TIMEOUT=1800                        # seconds; increase for slow hardware
```

**Model recommendations:** The rewrite step is token-heavy — aim for a model
that balances speed and quality for your hardware.

| Model | VRAM | Notes |
|-------|------|-------|
| `qwen2.5:3b` | ~2 GB | Fast, fits on most GPUs |
| `mistral:7b` | ~5 GB | Better quality, needs a discrete GPU |
| `qwen2.5:7b` | ~5 GB | Good quality/speed balance |

CPU inference works but is slow. The Ollama backend already limits itself to
one batch in flight, so there is no need to set `TID_WORKERS` by hand.

## Storage and job queues

Both are pluggable, and both default to **nothing extra to run**:

| | default | optional |
|---|---|---|
| storage | SQLite file (`TID_STATE`) | MongoDB — `tid[mongo]` |
| jobs | in-process asyncio queue | Redis + arq workers — `tid[redis]` |

Switch either with a URL:

```bash
# .env
TID_STORE=mongodb://mongo:27017/tid
TID_QUEUE=redis://redis:6379
```

and bring up the matching overlay so the service exists:

```bash
docker compose -f docker-compose.yml -f docker-compose.mongo.yml up
docker compose -f docker-compose.yml -f docker-compose.redis.yml up
```

The plain `docker compose up` remains a single container.

Moving between backends is a store-to-store copy through the protocol, so it
works for any pair:

```bash
uv run tid migrate --from state.db --to mongodb://localhost:27017/tid
```

The Redis overlay also starts an `arq` worker. That is worth understanding
even as a single user: with a queue, the web process only *enqueues* work and a
separate process with `TID_MAX_JOBS` runs it — a process-level cap is the
only thing that genuinely bounds how many LLM jobs hit one GPU at a time.

## What it produces

A 100–200 page PDF with:

- **Cover page**: title + date + article count, quote of the day from
  Wikiquote, a "World news" block (5 tech headlines + 2 Western items from
  Wikipedia's Current Events portal, each compressed to a single sentence).
- **Contents**: every article grouped by source, with dot-leaders to its
  publication date.
- **"Did you know…"** trivia nuggets from Wikipedia's Main Page.
- **The articles themselves**, set in two-column Latin Modern with proper
  paragraph indents, hyphenation, microtypography. Math (`$x = y$`,
  `$$\int f$$`, `\(...\)`, `\[...\]`) is rendered as real LaTeX math. Code
  blocks (fenced or inline) come through in monospace.
- All non-English source content (heise, etc.) is translated to English
  during the rewrite step. You can disable that in the prompt if you don't
  want it.

### Cover page

[📄 See the full sample PDF →](sample-2026-06-04.pdf)

[![Cover page: title, quote of the day, world news, table of contents](assets/cover.png)](sample-2026-06-04.pdf)

### Article body

[📄 See the full sample PDF →](sample-2026-06-04.pdf)

[![A typical two-column article page, set in Latin Modern](assets/article.png)](sample-2026-06-04.pdf)

## Architecture

```
                   sources.toml
                       │
            ┌──────────┴──────────┐
            │                     │
            ▼                     ▼
       ┌────────┐            ┌────────┐
       │ gather │            │ wiki/  │
       │  HN +  │            │ news + │
       │  RSS   │            │  QOTD  │
       └───┬────┘            └───┬────┘
           ▼                     │
       ┌────────┐                │
       │extract │                │
       │ (traf- │                │
       │  ilatura)               │
       └───┬────┘                │
           ▼                     │
       ┌─────────┐               │
       │summarize│ ─── LLM       │
       └───┬─────┘               │
           ▼                     │
       ┌─────────┐               │
       │ rewrite │ ─── LLM       │
       └───┬─────┘               │
           ▼                     ▼
       SQLite store (state.db)   in-memory
           │                     │
           └──────────┬──────────┘
                     ▼
              ┌──────────┐
              │  render  │ ── xelatex
              └────┬─────┘
                   ▼
             archive/cache/<hash>.pdf
```

Four stages, each idempotent and resumable:

1. **gather** — pulls new items from each source, runs `trafilatura` to
   extract the article body, stores the raw text. Pure I/O — no LLM cost.
2. **summarize** — batches up to 8 articles per LLM call and produces a
   ≤40-word two-sentence summary for each (used as the lede in the front
   matter and in the contents listing).
3. **rewrite** — batches up to 8 articles per LLM call and produces a
   clean, properly-paragraphed, translated-to-English version of each
   article body for the renderer. Preserves code fences and `$math$` exactly.
4. **render** — pulls the latest N articles per source from the store,
   plus fresh world news + quote + DYK, and runs them through a Jinja
   template into xelatex → PDF. Results are cached by a hash of "what's in
   the store" + "what's in sources.toml". Same content + same config → same
   cached PDF served instantly.

A background `APScheduler` job (`AsyncIOScheduler`, on the app's event loop)
runs steps 1–3 every 4 hours (configurable).
Each ingest ends by building the edition, so the PDF and its archive entry
exist whether or not anyone visits. `/digest.pdf` still builds on demand when
the cache is cold — a fresh install, or a config change between runs.

## HTTP endpoints

| route          | what it does                                            |
|----------------|---------------------------------------------------------|
| `GET /`        | index: latest edition's cover, then every past edition  |
| `GET /digest.pdf` | the current edition (built on demand, then cached)   |
| `GET /preview.png` | page 1 rasterized at 180 DPI                        |
| `GET /digest/{key}.pdf` | one archived edition, by cache key             |
| `GET /digest/{key}.png` | its cover, rendered on first request           |
| `GET /archive.json` | the archive as JSON (key, date, article count)     |
| `GET /sources` | JSON list of configured sources + latest `fetched_at`   |
| `GET /healthz` | liveness probe (returns `ok`)                           |
| `GET /readyz`  | readiness probe — pings the store and parses the config |
| `POST /ingest` | manual kick of the gather → summarize → rewrite cycle   |

## The archive

Every build drops a small JSON sidecar next to its PDF in the cache
directory, and `GET /` lists those sidecars newest-first: the latest edition
with its cover, then every earlier one as a row with its date, article count
and per-source breakdown. Nothing is ever deleted — an edition stays
downloadable at `/digest/{key}.pdf` for as long as its file is on disk, and
those URLs are immutable (the key *is* the content hash), so they cache
forever.

PDFs that predate the sidecars still appear, described by what the
filesystem knows: mtime for the date, size for the size, no article count.

At roughly 500 KB per edition and one or two editions a day, the cache grows
by about 350 MB a year. There is no automatic pruning; `rm` the old
`{key}.pdf` and `{key}.json` pairs when you want the space back.

## Configuring sources

Sources live in [`sources.toml`](sources.toml) — that's the exact file used
to produce [the sample PDF](sample-2026-06-04.pdf). Open it, copy a block,
edit, restart the container, refresh `/digest.pdf`.

The order of `[[source]]` blocks in the file is the order they'll appear in
the PDF — sources at the top come first. World news, quote of the day, and
the "Did you know…" nuggets are not configured here — they're cover
decorations, fetched fresh on every render.

### `kind = "hn"` — Hacker News via the Algolia search API

Ranks stories by points within a time window. No URL needed; the API is
hardcoded.

| field          | type | default | meaning |
|----------------|------|---------|---------|
| `name`         | string | required | display label (also the contents-page heading) |
| `kind`         | string | required | must be `"hn"` |
| `limit`        | int  | `10`     | how many top stories to keep |
| `since_hours`  | int  | `48`     | only consider stories submitted in the last N hours |
| `min_points`   | int  | `50`     | story must have at least this many points to qualify |

```toml
[[source]]
name        = "Hacker News"
kind        = "hn"
limit       = 10
since_hours = 48
min_points  = 100
```

### `kind = "rss"` — any Atom/RSS feed

Parsed with [feedparser](https://feedparser.readthedocs.io/), so it accepts
RSS 0.9/1.0/2.0 and Atom 1.0 — every blog and most news sites work.

| field         | type   | default  | meaning |
|---------------|--------|----------|---------|
| `name`        | string | required | display label (also the contents-page heading) |
| `kind`        | string | required | must be `"rss"` |
| `url`         | string | required | feed URL |
| `limit`       | int    | `20`     | take at most N most-recent items |
| `since_hours` | int    | unset    | skip articles published more than N hours ago (uses the feed's `published`/`updated` date; articles with no date are always kept) |

```toml
[[source]]
name        = "Quanta Magazine"
kind        = "rss"
url         = "https://www.quantamagazine.org/feed/"
limit       = 8
since_hours = 168   # one week
```

### Per-source ordering and limits in practice

The `limit` is applied **twice**, on purpose:

- At **fetch** time: gather doesn't pull more than `limit` items from the
  feed (saves bandwidth and trafilatura time).
- At **render** time: even if the store accumulates more than `limit` items
  for a source across multiple ingests (it will — items don't get deleted),
  only the latest `limit` per source make it into a given PDF.

So if you want Quanta to have at most 8 articles in the issue, regardless of
how many they've published this week → set `limit = 8`. If you want Hacker
News to show only the top 5 by points in the last 24h → set `limit = 5,
since_hours = 24`.

> **On the totals.** Adding up every `limit` in `sources.toml` gives you the
> maximum article count per issue. Aim for **30–60 articles** for a
> comfortable 30–60 minute read. Claude's summaries are dense; volume isn't
> quality. An empty section on a slow day is cleaner than padding.

## Scheduling ingests

Two modes; pick whichever fits your routine. Set the env var in `.env`.

### Every N hours (default)

```bash
# .env
INGEST_INTERVAL_SECONDS=14400   # 4 hours (the default)
```

### Cron-style fixed times — "morning and evening edition"

```bash
# .env
INGEST_SCHEDULE=07:00,18:00     # comma-separated HH:MM
INGEST_TIMEZONE=Europe/London   # any IANA tz; default UTC
```

If both are set, `INGEST_SCHEDULE` wins. Either way the run ends with a
built PDF, so hitting `/digest.pdf` between scheduled runs returns the cached
file instantly.

You can also kick a manual ingest any time:

```bash
curl -X POST http://localhost:8000/ingest
```

## Delivery — push the PDF wherever you want

A built-in hook fires after every successful ingest. Point
`POST_INGEST_HOOK` at any executable on the container's filesystem (drop
the script into your `./data/hooks/` directory so it survives rebuilds via
the bind mount). The hook receives the freshly-built PDF path as its first
argument.

```bash
# .env
POST_INGEST_HOOK=/data/hooks/push-to-remarkable.sh
POST_INGEST_HOOK_TIMEOUT=300    # optional; default 300s
```

Hook failures are non-fatal — a broken hook logs an error but doesn't
crash the ingest loop.

### Sample: push to a reMarkable 2 over WiFi

Drop this in `./data/hooks/push-to-remarkable.sh` and `chmod +x` it:

```bash
#!/usr/bin/env bash
# Push the latest issue to a reMarkable 2 via SSH.
# Usage: push-to-remarkable.sh <pdf-path>
set -euo pipefail

PDF="$1"
REMARKABLE="root@10.11.99.1"            # adjust to your device's IP
SSH_KEY=/data/hooks/remarkable_id_ed25519

scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new \
    "$PDF" "$REMARKABLE:/home/root/theinternetdaily.pdf"

# Refresh the UI so the file appears immediately.
ssh -i "$SSH_KEY" "$REMARKABLE" 'systemctl restart xochitl'
```

Generate a passwordless key (`ssh-keygen -t ed25519 -f
data/hooks/remarkable_id_ed25519 -N ""`), add the `.pub` to the
reMarkable's `/home/root/.ssh/authorized_keys` once, and from then on
every ingest pushes the new paper to your device.

The same pattern works for Kindle (`scp` over USB networking), a network
printer (`lp -d tid "$PDF"`), an email (`mutt -a "$PDF"`), or
anything else you can script.

## Tests

Modest, no-network pytest suite covering the render tokenizer, the
`since_hours` window, the batch parsers, the web/scheduling/hook behaviour,
and an end-to-end ingest against a fake LLM backend:

```bash
uv run pytest
```

`tests/test_e2e_offline.py` runs the whole pipeline — gather → extract →
summarize → rewrite → edition → LaTeX — with HTTP served by
`httpx.MockTransport` and the model replaced by `tid.testing.FakeBackend`,
so it needs no network, no GPU and no API key. The xelatex step runs too, but
only where xelatex is installed.

### Measured on a 20 GB card

Reference box: NVIDIA RTX 4000 SFF Ada (20 GB, **70 W**), 20 cores, 62 GB RAM.
vLLM 0.26.0 serving `google/gemma-4-12B-it-qat-w4a16-ct` (4-bit QAT,
compressed-tensors) at `--max-model-len 16384 --max-num-seqs 4
--kv-cache-dtype fp8`. 44 articles from the sources in this repo.

| stage | wall clock | items | LLM calls | in tok | out tok | out tok/s |
|---|---|---|---|---|---|---|
| gather + extract | 61 s | 45 | — | — | — | — |
| summarize | 38 s | 44 | 11 | 29 147 | 2 252 | 59.9 |
| rewrite | 807 s | 44 | 44 | 93 147 | 66 699 | 82.6 |
| cover decorations | ~6 s | — | 1 | — | — | — |
| render (xelatex, 142 pp.) | ~7 s | 41 | — | — | — | — |

**~15 minutes for a full edition, entirely local, no API key present on the
box.** Weights take 8.3 GiB, leaving 8.9 GiB of KV cache (139 374 tokens);
peak VRAM was 18.4 GiB of 20.0, the card sat at its 70 W cap and 73 °C, and
vLLM held 4 concurrent sequences for most of the run. Engine start is ~53 s
(40 s of it compilation), so keep the server up between editions.

Quality on that run: 0 empty summaries, 0 empty bodies, 0 `tex_body` failures,
median summary 31 words (the prompt caps it at 40, and nothing exceeded it).
No article lost math — 41 of 44 came back with exactly the delimiters they went
in with, and the other 3 *gained* correct ones where `trafilatura` had
flattened them (`$\alpha$`, `$\mathbb{R}^2$`).

Two scripts cover what a test suite cannot:

```bash
# per-stage wall clock, tokens/s and peak VRAM, as a markdown table
uv run python scripts/benchmark.py --backend vllm --state data/bench.db

# score what an ingest produced — or run the same articles through two
# backends side by side — including the markup render.py depends on
# (code fences, inline backticks, math delimiters)
uv run python scripts/quality_diff.py --state data/bench.db --columns store
uv run python scripts/quality_diff.py --columns anthropic,vllm --limit 20
```

## Local development

You don't have to use Docker — the CLI works directly. The project is
managed with [uv](https://docs.astral.sh/uv/); it reads `.python-version`,
fetches CPython 3.14 if you don't have it, and creates `.venv` from
`uv.lock` on first run:

```bash
uv sync
export ANTHROPIC_API_KEY=sk-ant-...   # or: export LLM_BACKEND=ollama OLLAMA_HOST=...

uv run tid gather       # fetch + extract
uv run tid summarize    # LLM pass 1 (batched)
uv run tid rewrite      # LLM pass 2 (batched)
uv run tid render       # xelatex → PDF
# or all of the above in sequence:
uv run tid build
```

Dependency changes go through `pyproject.toml` — `uv add <pkg>` / `uv remove
<pkg>` to edit and relock in one step, `uv lock --upgrade` to refresh pins.
Commit `uv.lock`: the Docker build installs with `--frozen` and fails if the
lockfile has drifted from `pyproject.toml`.

Requirements: [uv](https://docs.astral.sh/uv/getting-started/installation/)
(no system Python needed — uv provisions 3.14 itself), `xelatex` (TeX Live
with `texlive-xetex`, `texlive-latex-extra`, `lmodern`), `pdftoppm` (poppler).

## Customizing the typography

Everything visual lives in one file: [`tid/template.tex.j2`](tid/template.tex.j2).

- Page size: `paperwidth=157mm, paperheight=210mm` (tuned for reMarkable Pro)
- Body font: Latin Modern Roman 10pt
- Two-column body for any article over 2000 characters; single-column
  otherwise
- First-line paragraph indent instead of vertical `\parskip` (classic
  magazine convention)
- Microtype protrusion + expansion
- Letter-spacing on small-caps source labels via fontspec's `LetterSpace`

Customize whatever you like — the Jinja delimiters are LaTeX-safe
(`((* ... *))` for blocks, `((( ... )))` for variables) so your `{`, `}` and
`\` don't fight each other.

## Cost

**With Ollama:** free — all inference runs locally.

**With Anthropic (Claude Haiku 4.5, default):** roughly per ingest cycle
with ~50 articles:

- Summarize: 6 batched calls (~8 articles each)
- Rewrite: 6 batched calls
- World-news compress: 1 call

Order-of-magnitude: a few cents to a few tens of cents per cycle depending on
article lengths. At 6 cycles/day that's well under $1/day. Going to Sonnet or
Opus multiplies the bill ~10–30×.

Set a spend cap at
https://console.anthropic.com/settings/billing → Spend limits — the run-loop
can't surprise you above whatever you set.

## Privacy

- All data lives on your machine (`./data/state.db` + `./data/archive/cache/`).
- With `LLM_BACKEND=anthropic`: article text is sent to the Anthropic API
  for summarization and rewriting. That's the only outbound destination for
  content (besides fetching the feeds themselves).
- With `LLM_BACKEND=ollama`: nothing leaves your machine. All inference
  runs locally.
- No analytics, no telemetry, no third-party scripts in the landing page.

## Project layout

```
theinternetdaily/
├── tid/
│   ├── fetch.py          # HN Algolia + RSS feedparser
│   ├── extract.py        # trafilatura
│   ├── llm.py            # LLMBackend protocol: Anthropic / vLLM / Ollama
│   ├── testing.py        # FakeBackend, so CI needs no GPU or API key
│   ├── summarize.py      # summarization prompts + batching
│   ├── rewrite.py        # rewrite prompts + batching
│   ├── wiki.py           # World news / Quote / DYK / tech feeds
│   ├── config.py         # env-derived configuration (read per call)
│   ├── jobs.py           # units of work a queue can run
│   ├── worker.py         # arq worker entry point (optional)
│   ├── store/            # storage protocol + backends
│   │   ├── base.py       #   Store protocol + ArticleRow
│   │   ├── sqlite.py     #   default backend, no extra services
│   │   └── mongo.py      #   optional: tid[mongo]
│   ├── queue/            # job queue protocol + backends
│   │   ├── base.py       #   JobQueue protocol
│   │   ├── local.py      #   default: in-process asyncio
│   │   └── arq_queue.py  #   optional: tid[redis]
│   ├── render.py         # Jinja + xelatex
│   ├── preview.py        # PDF → PNG via pdftoppm
│   ├── cache.py          # On-disk cache by content hash
│   ├── archive.py        # Every edition ever built, for the index page
│   ├── cli.py            # tid command
│   ├── web.py            # FastAPI + APScheduler (AsyncIOScheduler)
│   └── template.tex.j2   # the magazine
├── scripts/
│   ├── benchmark.py      # per-stage timing, tokens/s, peak VRAM
│   └── quality_diff.py   # quality report per backend, incl. markup checks
├── sources.toml          # configured feeds
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── docker-compose.vllm.yml   # optional local-GPU vLLM overlay
├── docker-compose.mongo.yml  # optional MongoDB overlay
├── docker-compose.redis.yml  # optional Redis + arq worker overlay
└── data/                 # gitignored — your SQLite + cached PDFs
```

## Contributing

Open an issue first if you're planning something non-trivial — happy to talk
about direction. The codebase is small enough that you can read it end to
end in an hour.

## License

MIT — see [LICENSE](LICENSE).

## Why "The Internet Daily"

The vibe is: an old-fashioned daily paper, not a feed. The internet, set in
type and delivered once a day. You read it, then you put it down.
