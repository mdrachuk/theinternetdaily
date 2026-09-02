---
name: deliver
description: Take a task, issue or PR from description to production in this repo — implement it, verify it, open a PR, merge it, and redeploy the running stack. Use when the user says /deliver, or asks to "ship", "deliver", or "implement and deploy" something end to end.
---

# Deliver

One task → merged on `main` → running on this box. Six phases, in order. Do not
skip a phase; if one cannot be completed, stop there and say so plainly.

The repo is **The Internet Daily** (`tid`): a Python 3.14 / FastAPI news
aggregator that publishes a daily paper as a website. `github.com/mdrachuk/theinternetdaily`.

**Two checkouts, two jobs — never confuse them:**

| Path | Role |
| --- | --- |
| the checkout you are in (often a `worktree/…` branch under `~/.herdr/worktrees/`) | where you write code, run tests, commit, push |
| `~/theinternetdaily` | the **deployment** checkout — the running containers are built from it. Only phase 6 touches it, only on `main`, only via absolute paths. |

Phase 6 describes the maintainer's own box (Compose + a local vLLM GPU). If you
run this stack somewhere else, phases 0–5 hold as written and phase 6 is the part
to rewrite for your host.

## Phase 0 — resolve the task

The argument is one of three things:

- **A plain description** ("photos are cropped on the front page") — that is the task.
- **A PR number or URL** — `gh pr view <n> --json title,body,comments,headRefName,state,mergeable`.
  If it is open with review comments, the task is *address the comments and land it*:
  check out that branch (`gh pr checkout <n>`) and skip branch creation in phase 1.
- **An issue URL** — GitHub issues are **disabled** on `mdrachuk/theinternetdaily`, so
  an issue link almost always points at the upstream `marcj/papernews` or elsewhere.
  Fetch it (`gh issue view <url> --json title,body,comments`) and treat the body as the spec.

Then **read the repo before planning**. `README.md` is unusually complete (~900
lines) and is the design document: it explains the pipeline (gather → summarize →
rewrite → topics → render), the store/queue protocols, the templates, and the
directory map. Find the two or three files the task actually lives in
(`tid/edition.py`, `tid/site.py`, `tid/topics.py`, `tid/templates/*`, `tid/store/*`
are the usual suspects) and read them whole. Small codebase — read, don't guess.

State the plan in a sentence or two before editing. Ask the user only if two
readings of the task would produce materially different code.

## Phase 1 — branch

If already on a non-`main` branch in this worktree (herdr hands you
`worktree/<name>`), just use it — PR #5 shipped from exactly such a branch.
Otherwise branch from an up-to-date `main`:

```bash
git fetch origin && git checkout -b <type>/<short-slug> origin/main
```

`<type>` matches the commit type: `feat/`, `fix/`, `chore/`.

## Phase 2 — implement

- Match the surrounding code: `from __future__ import annotations`, modern type
  syntax (`str | None`), small pure functions, docstrings that say *why*.
- Comments here explain intent and history, not mechanics. Keep that voice —
  the codebase reads like prose, and the README/comments are part of the product.
- Config goes through `tid/config.py` (env with sensible defaults); every LLM call
  goes through the `LLMBackend` protocol in `tid/llm.py` — never call a provider
  SDK directly from a feature module.
- New behaviour needs a test. Tests are offline by construction: use
  `tid.testing.FakeBackend` and the parametrized `store` fixture in
  `tests/conftest.py` (SQLite always; Mongo only with `TID_TEST_MONGO_URI`).
- If the change alters behaviour a user would notice, update the matching
  README section in the same commit.

## Phase 3 — verify

Verification is not optional and "the tests pass" is rarely the whole of it.

```bash
uv run pytest -q        # whole suite, offline, ~3s — always run it
```

Then prove the actual change, choosing what fits:

- **Pipeline / store / edition logic** — drive the CLI against a scratch store so
  the real store is never touched:
  ```bash
  uv run tid --state <your scratchpad>/verify.db --backend anthropic status
  ```
  (`gather` needs network; `summarize`/`rewrite`/`topics` need a real backend —
  prefer a test over a live run.)
- **Site / templates / layout** — render the page in-process and inspect the HTML
  (`tid.web.create_app` + `httpx.ASGITransport`, as `tests/test_web_features.py`
  does), or serve it: `uv run uvicorn --factory tid.web:create_app --port 8123`
  against a copy of `~/theinternetdaily/data/state.db`. Never bind 8000 —
  that is production.
- **Rendering (PDF)** — `uv run tid render --out ./out` needs xelatex, which is
  only in the container: `docker compose exec tid tid render`.

Report what you ran and what it showed. If something fails, fix it and re-verify.

## Phase 4 — commit and open the PR

Commits follow conventional-commit types with a **descriptive, human subject** —
look at the log for the register:

```
fix(site): print the whole photograph, at the size the feed actually offers
feat(topics): name an edition's sections by reading it, not by pinning them to sources
```

Lowercase after the colon, no trailing period, imperative-ish, says what changed
*for the reader of the paper*. Body: why, and what it means for anyone running
the stack. Footer (required):

```
Co-Authored-By: <the model you are> <noreply@anthropic.com>
Claude-Session: <this session's URL>
```

Use the attribution the running session was given — never copy a session URL out
of an older commit.

Then:

```bash
git push -u origin HEAD
gh pr create --base main --title "<same subject>" --body "$(cat <<'EOF'
## What
...
## Why
...
## Verification
- `uv run pytest -q` — 274 passed
- <the change-specific check and what it showed>
EOF
)"
```

End the PR body with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

<this session's URL>
```

## Phase 5 — merge

**Confirm with the user before this phase**, showing the PR URL and a one-line
diffstat — merging to `main` and redeploying are the irreversible half of the
job. Skip the pause only if they already said to run the whole thing unattended.

```bash
gh pr merge <n> --squash --delete-branch
```

Squash is the house style (see the `(#7)`…`(#10)` subjects on `main`). If the
merge is blocked, report why instead of forcing it.

## Phase 6 — deploy

Production is plain Docker Compose in `~/theinternetdaily`, published on
`127.0.0.1:8000`, ingesting daily at 08:00 Europe/Warsaw.

**The overlay is mandatory.** `.env` sets `LLM_BACKEND=vllm`, but only
`docker-compose.vllm.yml` sets `VLLM_BASE_URL`. Bring the stack up without it and
every LLM call fails with *"Request URL is missing an 'http://' or 'https://'
protocol"* and no edition publishes — that happened on 2026-08-31.

**Only rebuild the `tid` service.** `tid-vllm` holds a ~12B model in GPU memory
and takes minutes to come back; naming the service leaves it untouched.

```bash
git -C ~/theinternetdaily status --porcelain   # must be empty
git -C ~/theinternetdaily checkout main
git -C ~/theinternetdaily pull --ff-only origin main

docker compose --project-directory ~/theinternetdaily \
  -f ~/theinternetdaily/docker-compose.yml \
  -f ~/theinternetdaily/docker-compose.vllm.yml \
  up -d --build tid
```

If the deployment checkout is dirty, stop and show the diff — do not stash
(the stash stack is shared with every worktree) and do not discard it.

Verify the deploy, all four:

```bash
docker compose --project-directory ~/theinternetdaily \
  -f ~/theinternetdaily/docker-compose.yml \
  -f ~/theinternetdaily/docker-compose.vllm.yml ps
docker inspect tid --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
                                        # MUST list both compose files
curl -s http://127.0.0.1:8000/healthz   # ok
curl -s http://127.0.0.1:8000/readyz    # {"store":"ok","config":"ok"}
docker logs --since 2m tid 2>&1 | tail -30   # no tracebacks
```

Plus a check that the shipped change is actually live — usually
`curl -s http://127.0.0.1:8000/ | grep …` for the markup you changed.

If the change affects how articles are processed and you want it applied to
pending items now rather than at 08:00:
`curl -X POST http://127.0.0.1:8000/ingest` (items are retried; nothing is lost).

**Rollback**, if verification fails: revert on `main`, then redeploy with the
same overlay command.

```bash
git -C ~/theinternetdaily revert --no-edit <sha> && git -C ~/theinternetdaily push origin main
```

## Phase 7 — report

Close with: what shipped, the PR link, the merge commit, what verification showed
(test count + the change-specific check), and the deploy's health output. Say
explicitly if anything was left out. If the delivery turned up a durable,
non-obvious operational fact, write it to memory.
