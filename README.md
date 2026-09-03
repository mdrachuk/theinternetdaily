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
text**, not just summaries — and lays the result out as one consistently
typeset newspaper: a front page with a lead story, section columns, and every
article continued below the fold. Each headline has a page of its own carrying
the whole rewritten text. Every article is *in* the paper; you read entirely
within it, no clicking through, no opening tabs.

A side benefit I didn't expect to like but very much do: one place to read
the day's news instead of five tabs being refreshed all day. One or two
editions per day, no more.

Designed for an e-ink reader like the reMarkable, but it reads just as well in
any browser. The paper is one self-contained HTML page per view — the
stylesheet is inlined, source marks are cached and served locally — so it
holds up on a device that wakes its radio once and then goes back to sleep.

> **PDFs are paused.** Earlier versions of this rendered a LaTeX PDF and
> nothing else. `tid render` still typesets one from the same store (see
> [Still want a PDF?](#still-want-a-pdf)), but nothing does it automatically
> and no route serves one.

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
$EDITOR sources.toml     # add/remove RSS/HN entries, set gather windows

# 4) (Optional) Tweak the look
$EDITOR tid/templates/style.css

# 5) Build + run
docker compose up --build -d

# Open http://localhost:8000
# The first edition assembles on demand. Background ingest runs every 4h.
```

Everything you'd normally want to change is in **two files**:

- **`sources.toml`** — which feeds, how many items per feed, which section
  each one files under, in what order; and the paper's standing sections
  (`[[topic]]`), which every edition's topic set starts from. Two source
  kinds today: `kind = "hn"` (Hacker News, top-by-points via the Algolia API)
  and `kind = "rss"` (any Atom/RSS feed via feedparser).
- **`tid/templates/`** — the paper itself. `style.css` is every colour,
  size and breakpoint; `edition.html`, `article.html` and `sources.html` are
  the three pages. Edit, restart the container, refresh.

Optional but useful:

- **`tid/summarize.py`** + **`tid/rewrite.py`** — the LLM
  system prompts. When using Anthropic, change `ANTHROPIC_MODEL` to
  `claude-sonnet-4-6` for fancier rewrites at ~10× the cost; adjust
  `_SYSTEM` to change the editorial voice (e.g. disable the
  auto-translate-to-English rule).
- **`tid/wiki.py`** — what goes into the World news block and the
  Quote-of-the-day source.

### Reading it on a reMarkable

Open `http://your-machine:8000` in the device's browser and bookmark it. The
page is one request — the CSS and the eight lines of script are inlined, the
source marks are served from this host, and nothing else is fetched — so it
loads on one radio wake and stays readable when the connection drops.

Arrow keys (or the page buttons) move between editions; `Esc` closes a
preview.

#### Still want a PDF?

The LaTeX renderer is untouched, it is just no longer wired to anything
automatic:

```bash
uv run tid render --out ./out          # or: docker compose exec tid tid render
```

**👉 [`sample-2026-06-04.pdf` is a real day's output.](sample-2026-06-04.pdf)**

[![Cover page: title, quote of the day, world news, table of contents](assets/cover.png)](sample-2026-06-04.pdf)
[![A typical two-column article page, set in Latin Modern](assets/article.png)](sample-2026-06-04.pdf)

From there the old routes apply — upload it at `my.remarkable.com`, the
mobile app, the USB Web Interface at `http://10.11.99.1`, or
[`rmapi`](https://github.com/ddvk/rmapi):

```bash
rmapi put out/*.pdf /TheInternetDaily
```
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

Then visit `http://localhost:8000` — that is the paper. The first edition
assembles on demand (a store read and a JSON write, so it is instant); the
articles in it appear as ingest works through them, which takes a few minutes
on a cold store.

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

**The front page** (`/`) — a masthead, then:

- **The lead**: the main story of the day's most significant topic, with its
  dek and its image.
- **Two stories beside it**, one from each of the next topics, so the top of
  the page is a summary of the whole paper.
- **Topic columns** (up to four): each one's main story with a dek, then three
  more headlines.
- **Below the fold**, every topic again, carrying everything the front page
  did not have room for: a big story on the left, the rest in newspaper
  columns beside it.

The columns are not fixed. They are named for each edition by reading it —
see [Topics](#topics-the-sections-are-named-per-edition).

Every headline is a link to the source, and every one has a **Preview** that
opens the dek and the opening lines in a drawer, plus a link to the article's
own page.

**The article page** (`/e/{key}/a/{id}`) — the whole rewritten body, set in
one measure with generous leading. Code fences and inline backticks come
through in monospace; TeX (`$x = y$`, `$$\int f$$`, `\(...\)`, `\[...\]`)
renders through MathJax, which is loaded *only* on pages that contain some.
All non-English source content is translated to English during the rewrite
step; you can disable that in the prompt if you don't want it.

**The subscriptions page** (`/sources`) — every feed with its fallback
section, its medium, how far back it gathers and how much it contributed to
the latest edition. It is a view of `sources.toml`, not an editor for it.

**The archive** — every edition ever assembled stays at its own URL, exactly
as it was published.

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
           ▼                     │
       ┌─────────┐               │
       │ topics  │ ─── LLM       │
       └───┬─────┘               │
           ▼
       SQLite store (state.db)
           │
           ▼
       ┌──────────┐
       │ assemble │ ── tid/edition.py
       └────┬─────┘
            ▼
   archive/cache/<hash>.json   ── the snapshot
            │
            ▼
       ┌──────────┐
       │  render  │ ── tid/site.py + templates/
       └────┬─────┘
            ▼
        the paper
```

Six stages. The first five are idempotent and resumable; the sixth is
pure and runs per request:

1. **gather** — pulls new items from each source, runs `trafilatura` to
   extract the article body, stores the raw text. Pure I/O — no LLM cost.
2. **summarize** — batches up to 8 articles per LLM call and produces a
   ≤40-word two-sentence summary for each (used as the lede in the front
   matter and in the contents listing).
3. **rewrite** — batches up to 8 articles per LLM call and produces a
   clean, properly-paragraphed, translated-to-English version of each
   article body for the renderer. Preserves code fences and `$math$` exactly.
4. **topics** — reads the whole set of articles that will be in the next
   edition and decides what its sections are. Three passes:
   one call names the topics, one call per article files it under the most
   specific one, one call per topic picks that topic's main stories. See
   [Topics](#topics-the-sections-are-named-per-edition).
5. **assemble** — pulls every ready article no previous edition carried, from
   every source, writes them to a snapshot keyed by a hash of "what's in the
   store" + "what's in sources.toml", and stamps `rendered_at` on each one so
   the next edition knows to skip it. This is cheap: a store read and a JSON
   write, no LLM and no typesetting, which is why it happens inline on a cache
   miss rather than through the queue.
6. **lay out** — `tid/edition.py` turns that flat list into a front page:
   which story leads, which two go beside it, what each column holds, what
   continues below. It is pure and it runs per request, so the layout is
   *not* stored — an improvement to the front page reaches editions published
   last month too.

A background `APScheduler` job (`AsyncIOScheduler`, on the app's event loop)
runs steps 1–4 every 4 hours (configurable). Each ingest ends by assembling
the edition and caching its source marks, so the archive entry exists — and
the paper loads without a single outbound request — whether or not anyone
visits.

## HTTP endpoints

| route                     | what it does                                       |
|---------------------------|----------------------------------------------------|
| `GET /`                   | the current edition                                |
| `GET /e/{key}`            | one edition from the archive, exactly as published |
| `GET /e/{key}/a/{id}`     | one article's full rewritten text                  |
| `GET /a/{id}`             | the same, resolved in the current edition          |
| `GET /sources`            | the subscription list                              |
| `GET /sources.json`       | the same, as JSON                                  |
| `GET /archive.json`       | every edition as JSON (key, date, counts, sources) |
| `GET /icon/{domain}.png`  | a source mark, fetched once and cached on disk     |
| `GET /healthz`            | liveness probe (returns `ok`)                      |
| `GET /readyz`             | readiness probe — pings the store, parses config   |
| `POST /ingest`            | manual kick of gather → summarize → rewrite → topics |

Both edition routes take `?m=read`, `?m=watch` or `?m=listen` to show one
medium only; the whole edition is laid out again from what is left, so a
column never loses its top story to a filter.

The old `/digest.pdf`, `/preview.png` and `/digest/{key}.pdf` routes redirect
to `/`.

## The archive

Every assembled edition writes a snapshot — `archive/cache/{key}.json` — with
the articles it was built from, bodies included. That is what makes yesterday's
paper still yesterday's paper: the store keeps moving underneath it (a
source is re-filed, a rewrite lands, a gather brings in fifty more stories),
and without a snapshot "the edition of 12 August" would quietly become
"whatever the store would produce for 12 August today".

Which articles an edition carried is not recorded in the snapshot: the store
stamps `rendered_at` on each one as it is published. "Has this run yet" is
therefore a fact about the article, not a window that has to be reconstructed
— which matters because one gather stamps every row it writes with the same
`fetched_at` second, while those rows finish summarizing and rewriting at very
different times. A cutoff of "newer than the last edition" would silently drop
every article still in the pipeline when that edition went out.

Nothing is ever deleted. An edition stays at `/e/{key}` for as long as its
file is on disk, and the prev/next arrows in the nav walk the archive
newest-first.

Editions from the PDF era still list in `/archive.json` with
`"has_items": false` — their sidecars recorded a count but not the articles,
so nothing can render them as a page. Saying so beats hiding them.

At roughly 300–800 KB per snapshot and one or two editions a day, the cache
grows by a few hundred MB a year. There is no automatic pruning; `rm` the old
`{key}.json` files when you want the space back. Cached source marks live in
`archive/cache/icons/` and are a few KB each.

## Configuring sources

Sources live in [`sources.toml`](sources.toml). Open it, copy a block, edit,
restart the container, refresh.

The order of `[[source]]` blocks decides which sources are gathered, and — as
a fallback — the shape of the paper. Normally the columns are named per
edition by the [topics stage](#topics-the-sections-are-named-per-edition);
`section` is what the layout falls back to for an article that stage never
filed, and what the whole paper falls back to on a day it cannot run.

Two fields shape the layout rather than the contents, and both are optional:

| field     | default       | meaning |
|-----------|---------------|---------|
| `section` | the source name | the column this source falls back to when the topics stage did not file its articles. Sources sharing a section share a column and interleave by date, so "The Guardian" and "Kyiv Independent" both under `section = "World"` read as one column, not two lists. |
| `medium`  | `"read"`      | `read`, `watch` or `listen`. Drives the glyph in the byline and the filter row in the nav. |

Note `medium` is not `kind`: `kind` is *how we fetch it* (`rss`/`hn`),
`medium` is *what you do with it*.

World news, quote of the day, and the "Did you know…" nuggets are not
configured here — they are PDF cover decorations, fetched fresh by
`tid render` and unused by the site.

### `kind = "hn"` — Hacker News via the Algolia search API

Ranks stories by points within a time window. No URL needed; the API is
hardcoded.

| field          | type | default | meaning |
|----------------|------|---------|---------|
| `name`         | string | required | display label, shown in every byline |
| `kind`         | string | required | must be `"hn"` |
| `section`      | string | the name | fallback column, when the topics stage does not file it |
| `medium`       | string | `"read"` | `read`, `watch` or `listen` |
| `since_hours`  | int  | `48`     | only consider stories submitted in the last N hours |
| `min_points`   | int  | `50`     | story must have at least this many points to qualify |

```toml
[[source]]
name        = "Hacker News"
kind        = "hn"
since_hours = 48
min_points  = 100
```

Every story clearing `since_hours` and `min_points` is taken. Those two are
the dial: raise `min_points` for a quieter paper, don't reach for a count.

### `kind = "rss"` — any Atom/RSS feed

Parsed with [feedparser](https://feedparser.readthedocs.io/), so it accepts
RSS 0.9/1.0/2.0 and Atom 1.0 — every blog and most news sites work.

| field         | type   | default  | meaning |
|---------------|--------|----------|---------|
| `name`        | string | required | display label, shown in every byline |
| `kind`        | string | required | must be `"rss"` |
| `url`         | string | required | feed URL |
| `section`     | string | the name | fallback column, when the topics stage does not file it |
| `medium`      | string | `"read"` | `read`, `watch` or `listen` |
| `since_hours` | int    | unset    | don't gather articles published more than N hours ago (uses the feed's `published`/`updated` date; articles with no date are always kept) |

```toml
[[source]]
name        = "Quanta Magazine"
kind        = "rss"
section     = "Science & Maths"
url         = "https://www.quantamagazine.org/feed/"
since_hours = 168   # one week
```

### How big an edition gets

There is no per-source cap. Every article a feed has filed since the last sync
is fetched, rewritten and given its own page, and the front page carries all of
them — the top of each section above the fold, the rest below it.

Two things bound an edition, and neither is a count:

- **`since_hours`, at gather time.** How far back into a feed one fetch
  reaches. It exists so that pointing at an archive feed doesn't walk back
  through five years on the first run; it is not a way to keep the paper
  short. Leave it unset and you take whatever the feed is currently carrying.
- **Publication state, at assemble time.** Every article an edition carries is
  stamped `rendered_at`, and the next edition takes what is still unstamped.
  So an article runs in exactly one edition: the first one published after it
  is *finished* — summarized **and** rewritten — which is not the same as the
  first one after it was gathered. Rewriting a whole feed through a local
  model takes longer than one edition's worth of patience, and a story still
  in the queue when the paper goes out simply runs in the next one rather than
  going out with raw scraped text it would never get to replace.

The one exception is an install with no published history at all — a fresh
setup, or an archive that has been cleared. The store never deletes rows, so
"everything unpublished" would be months of accumulated articles on one front
page. That first edition runs a 30-hour window instead
(`archive.NO_HISTORY_WINDOW`), and retires everything older than it: marked
published without being carried, so the backlog is excluded rather than
deferred to the following edition.

If a new edition comes out empty — you edited `sources.toml`, which moves the
edition key without gathering anything — the front page keeps showing the last
one rather than going blank. Section and medium are re-read from
`sources.toml` on every render, so re-filing a source into another column
shows up immediately — for the articles the topics stage did not file, which
carry their own column name and are not moved by a config edit.

> **On the reading time.** **30–60 articles** is a comfortable 30–60 minute
> read, and about what fills a front page and its continuation without either
> looking thin. Claude's summaries are dense; volume isn't quality. An empty
> section on a slow day is cleaner than padding.

## Topics: the sections are named per edition

Sections used to be pinned to sources: every MacRumors story under "Apple",
forever, because `sources.toml` said so. That is a subscription list wearing
an editor's hat. A real front page is organised by what happened today — when
three feeds all file on the same outage, the paper should carry one column
about the outage, not three columns named after the feeds.

So the last stage of every ingest reads the edition and names its own sections
(`tid/topics.py`). Three passes, each one a separate kind of judgement:

1. **Name the topics** — *one* call carrying every headline and summary in the
   edition. It comes back with 3–8 topics, ordered by significance, each a
   1–3 word section head with a one-line blurb. One call and not one per
   article on purpose: "what are today's sections" is a judgement about the
   whole paper, and an article-at-a-time process would invent a new topic for
   every story and never notice that two of them are the same.
2. **File each article** — one call per article, carrying that article and the
   whole topic list, choosing the single **most specific** topic it belongs to.
   Deliberately not batched: batching makes a model consistent with its
   neighbours in the batch rather than with the list. On a backend with guided
   decoding (vLLM, Ollama) the reply is an enum over the topic names, so an
   invented topic is impossible rather than merely discouraged.
3. **Pick the mains** — one call per topic, carrying that topic's articles,
   naming which of them carry the section and in what order. One main for a
   small topic, up to three for a large one. This is what the layout hangs on:
   the lead, the two stories beside it and the top of each column are mains,
   and everything else follows by date underneath them.

The result is stored per article (`topic`, `topic_order`, `main_rank`) and
travels into the edition snapshot, so an archived edition still reads as the
paper it was even after the store has moved on.

### Standing sections

A paper usually has sections that exist whether or not the day was big in
them. Give the stage those as `[[topic]]` tables in `sources.toml`:

```toml
[[topic]]
name = "World"
blurb = "international news, politics, conflict, diplomacy"

[[topic]]
name = "Machine Learning"
blurb = "AI research, models, and the industry around them"
```

They are the baseline of every edition's topic set. The naming call is shown
them and asked to rank them among the rest and add the day's own topics on
top — for a story cluster they do not cover, or one big enough to demand a
head of its own, inside a standing section's territory or outside it. Since
filing picks the *most specific* topic, a standing "World" still yields a
"Ukraine" column on a day the war dominates, with the rest of the world's news
under "World".

Whatever the model answers, the final set honours the config: a standing
section it named is restored to its configured spelling and blurb, one it left
out is appended, and the day's own topics are capped at what `MAX_TOPICS`
leaves room for (never fewer than two). A standing section nothing was filed
under simply has no column that day. The one thing the config cannot do is
rescue a naming call that failed outright — no usable topics still means the
`sources.toml` fallback layout, because a paper filed into standing sections by
a model that could not read the day is not the paper the editor asked for.
Leave out every `[[topic]]` and the stage names all the sections itself, as
before.

**It is re-run over the whole unpublished set every ingest**, not just over
new articles. A topic set describes *one* edition; a story that was gathered
yesterday and still has not been published belongs under this paper's
sections, under this paper's names.

**Every pass degrades to nothing rather than to something wrong.** No topics
named, an unparseable reply, a topic the model made up — the article simply
keeps no topic and the layout files it under its `sources.toml` section
exactly as before. A paper laid out the old way is a much smaller failure than
a paper whose columns are mislabelled.

Run it on its own against whatever is in the store:

```bash
docker compose exec tid tid topics
```

**What it costs.** One call for the edition, one per article, one per topic —
so an edition of 50 articles is ~59 calls, but all of them are small: a
headline and a 40-word summary in, a topic name out. The bodies never leave
the store. On Haiku that is a fraction of what the rewrite stage costs for the
same edition. On a local 12B model the topic-naming prompt is the one that
scales with the size of the edition, so `BatchLimits.topic_max_articles` caps
it at the 60 newest headlines (`VLLM_LIMITS`) to stay inside a 16k window.

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

If both are set, `INGEST_SCHEDULE` wins. Either way the run ends with the
edition assembled and its source marks cached, so the paper loads instantly
between scheduled runs.

You can also kick a manual ingest any time:

```bash
curl -X POST http://localhost:8000/ingest
```

## Delivery — do something with each new edition

A built-in hook fires after every successful ingest. Point
`POST_INGEST_HOOK` at any executable on the container's filesystem (drop
the script into your `./data/hooks/` directory so it survives rebuilds via
the bind mount). The hook receives the path of the freshly-written **edition
snapshot** as its first argument — JSON, with every article's title, source,
URL, summary and full rewritten body.

```bash
# .env
POST_INGEST_HOOK=/data/hooks/push-to-remarkable.sh
POST_INGEST_HOOK_TIMEOUT=300    # optional; default 300s
```

Hook failures are non-fatal — a broken hook logs an error but doesn't
crash the ingest loop.

### Sample: typeset the new edition and push it to a reMarkable 2

Drop this in `./data/hooks/push-to-remarkable.sh` and `chmod +x` it:

```bash
#!/usr/bin/env bash
# Typeset the edition that just landed and push it to a reMarkable 2 via SSH.
# Usage: push-to-remarkable.sh <snapshot-json-path>
set -euo pipefail

REMARKABLE="root@10.11.99.1"            # adjust to your device's IP
SSH_KEY=/data/hooks/remarkable_id_ed25519

# $1 is the snapshot; the PDF comes from the same store it was built from.
PDF=$(tid render --out /tmp/tid | tail -1)

scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new \
    "$PDF" "$REMARKABLE:/home/root/theinternetdaily.pdf"

# Refresh the UI so the file appears immediately.
ssh -i "$SSH_KEY" "$REMARKABLE" 'systemctl restart xochitl'
```

Generate a passwordless key (`ssh-keygen -t ed25519 -f
data/hooks/remarkable_id_ed25519 -N ""`), add the `.pub` to the
reMarkable's `/home/root/.ssh/authorized_keys` once, and from then on
every ingest pushes the new paper to your device.

The snapshot itself is often all you need, though — it is the whole edition
as data. `jq -r '.items[] | "\(.source): \(.title)"' "$1"` is a table of
contents; piping it into `mail`, a static-site generator, or your own
formatter needs no LaTeX at all.

## Tests

Modest, no-network pytest suite covering the front-page layout and the HTML
it renders, the render tokenizer, the `since_hours` window, the batch parsers,
the web/scheduling/hook behaviour, source marks and feed image extraction, and
an end-to-end ingest against a fake LLM backend:

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
uv run tid topics       # LLM pass 3: name the edition's sections and file it
uv run tid render       # xelatex → PDF
# or all of the above in sequence:
uv run tid build
```

Dependency changes go through `pyproject.toml` — `uv add <pkg>` / `uv remove
<pkg>` to edit and relock in one step, `uv lock --upgrade` to refresh pins.
Commit `uv.lock`: the Docker build installs with `--frozen` and fails if the
lockfile has drifted from `pyproject.toml`.

Requirements: [uv](https://docs.astral.sh/uv/getting-started/installation/)
(no system Python needed — uv provisions 3.14 itself). The site needs nothing
else; `xelatex` (TeX Live with `texlive-xetex`, `texlive-latex-extra`,
`lmodern`) is only needed if you want `tid render` to typeset a PDF.

## Customizing the typography

Everything visual lives in [`tid/templates/style.css`](tid/templates/style.css),
inlined into every page.

- Two faces: Libre Caslon Text for reading, Chivo for the masthead, bylines
  and every uppercase label. Both fall back to a stack that keeps the page
  correctly proportioned when the webfonts never arrive — the normal case on
  an e-ink reader with no connection.
- One palette, at the top of the file as custom properties: paper `#fbfaf7`,
  ink `#16150f`, one rust accent `#9a4a26` on the top bylines, and five greys
  for rules.
- Breakpoints at 620 / 1000 / 1360 (phone / tablet / laptop / wide) decide how
  many section columns the front page carries and how many the continuation
  splits into. Sizes *between* the breakpoints come from `clamp()`, so a 900px
  window is sized for 900px rather than rounded down to the tablet.
- The columns below the fold are real CSS `columns` with a `column-rule`, not
  a server-side split: how many fit is a viewport question, and the server
  does not get to know the viewport. The stories are ruled off from each
  other, and since a column break can hand any of them the top of a column,
  the top edge of the stack is covered by a strip of paper (`.stacks::before`)
  rather than by `:first-child` — which the markup only ever gets right for
  the first column. Change `--paper` and that strip follows it.

The three pages are `edition.html`, `article.html` and `sources.html`, with the
shared headline and byline markup in `_macros.html`. If you want the *PDF*
typography instead, that is still
[`tid/template.tex.j2`](tid/template.tex.j2), unchanged.

## Cost

**With Ollama:** free — all inference runs locally.

**With Anthropic (Claude Haiku 4.5, default):** roughly per ingest cycle
with ~50 articles:

- Summarize: 6 batched calls (~8 articles each)
- Rewrite: 6 batched calls
- Topics: 1 call for the edition + 1 per article + 1 per topic (~57), all of
  them small — a headline and a 40-word summary in, a topic name out
- World-news compress: 1 call

Order-of-magnitude: a few cents to a few tens of cents per cycle depending on
article lengths. At 6 cycles/day that's well under $1/day. Going to Sonnet or
Opus multiplies the bill ~10–30×.

Set a spend cap at
https://console.anthropic.com/settings/billing → Spend limits — the run-loop
can't surprise you above whatever you set.

## Privacy

- All data lives on your machine (`./data/state.db` + `./data/archive/cache/`).
- **Source marks never leak the reader.** The favicon in each byline comes
  from Google's s2 service, but your browser never talks to it: the server
  fetches each domain's icon once — usually during ingest, before anyone has
  asked — caches it to `archive/cache/icons/`, and serves it from this origin.
  The only host contacted is Google's, once per domain, from the box.
- The one third-party request a page does make is Google Fonts, for the two
  faces. Delete the two `<link>` tags in
  [`tid/templates/layout.html`](tid/templates/layout.html) if you would rather
  not; the fallback stacks are already there.
- MathJax loads from jsDelivr, and only on article pages whose body actually
  contains TeX.
- With `LLM_BACKEND=anthropic`: article text is sent to the Anthropic API
  for summarization and rewriting. That's the only outbound destination for
  content (besides fetching the feeds themselves).
- With `LLM_BACKEND=ollama`: nothing leaves your machine. All inference
  runs locally.
- No analytics, no telemetry, no third-party scripts. The only script on the
  paper is the preview drawer, inlined, eight lines of it — filters, day
  navigation and the sources page are all plain links the server answers, so
  the whole thing works with scripting off.

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
│   ├── topics.py         # name the edition's topics, file it, pick the mains
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
│   ├── edition.py        # articles in, a laid-out front page out (pure)
│   ├── site.py           # Jinja env + the filters the templates need
│   ├── icons.py          # cached source marks + the medium glyphs
│   ├── templates/        # the paper
│   │   ├── style.css     #   every colour, size and breakpoint
│   │   ├── layout.html   #   masthead + head
│   │   ├── edition.html  #   the front page and its continuation
│   │   ├── article.html  #   one article, in full
│   │   ├── sources.html  #   the subscription list
│   │   ├── _macros.html  #   headline + byline markup
│   │   └── app.js        #   the preview drawer, and nothing else
│   ├── cache.py          # the edition key: which edition is this?
│   ├── archive.py        # every edition ever assembled, as snapshots
│   ├── cli.py            # tid command
│   ├── web.py            # FastAPI + APScheduler (AsyncIOScheduler)
│   ├── render.py         # Jinja + xelatex — only `tid render` uses it now
│   └── template.tex.j2   # the PDF, for `tid render`
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
└── data/                 # gitignored — your SQLite + edition snapshots
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
