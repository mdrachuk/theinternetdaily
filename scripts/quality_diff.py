#!/usr/bin/env python
"""Side-by-side quality diff between two LLM backends.

    uv run python scripts/quality_diff.py --a anthropic --b vllm \
        --state data/bench.db --limit 20 --out data/quality-diff.md

Takes the same N articles through both backends and writes a markdown report.
Beyond eyeballing the prose, it checks the things `papernews/render.py` depends
on, because a model that mangles them produces *broken PDFs*, not merely worse
writing:

  * fenced code blocks survive as fences (and keep their line breaks)
  * inline backtick spans survive
  * LaTeX math delimiters ($…$, $$…$$, \\(…\\), \\[…\\]) are passed through
    unchanged and stay balanced
  * the result still compiles through render.tex_body without raising
"""
from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from papernews.llm import make_backend
from papernews.render import tex_body
from papernews.rewrite import rewrite_batch
from papernews.store import open_store
from papernews.summarize import summarize_batch

_FENCE_RE = re.compile(r"```")
_INLINE_RE = re.compile(r"`[^`\n]+`")
_MATH_RE = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]|(?<![\\$])\$[^$\n]+?\$|\\\(.+?\\\)",
                      re.DOTALL)


@dataclass
class Marks:
    fences: int
    inline: int
    math: int

    @classmethod
    def of(cls, text: str) -> "Marks":
        return cls(
            fences=len(_FENCE_RE.findall(text or "")) // 2,
            inline=len(_INLINE_RE.findall(text or "")),
            math=len(_MATH_RE.findall(text or "")),
        )


@dataclass
class Result:
    summary: str
    body: str
    tex_ok: bool
    tex_error: str = ""

    @classmethod
    def build(cls, summary: str, body: str) -> "Result":
        try:
            tex_body(body)
        except Exception as e:  # noqa: BLE001 - the point is to report it
            return cls(summary, body, False, f"{type(e).__name__}: {e}")
        return cls(summary, body, True)


async def run_backend(name: str, rows) -> list[Result]:
    backend = make_backend(name)
    try:
        items = [(r.title, r.text or "") for r in rows]
        summaries: list[str] = []
        bodies: list[str] = []
        size = backend.limits.summarize_batch
        for i in range(0, len(items), size):
            summaries += await summarize_batch(backend, items[i:i + size])
        size = backend.limits.rewrite_batch
        for i in range(0, len(items), size):
            bodies += await rewrite_batch(backend, items[i:i + size])
        print(f"{name}: {backend.usage.calls} calls, "
              f"{backend.usage.output_tokens} output tokens")
        return [Result.build(s, b) for s, b in zip(summaries, bodies)]
    finally:
        await backend.aclose()


def report(rows, a_name: str, a: list[Result], b_name: str, b: list[Result]) -> str:
    out: list[str] = [f"# Quality diff: {a_name} vs {b_name}", ""]

    def score(results: list[Result]) -> dict[str, int]:
        return {
            "empty summaries": sum(1 for r in results if not r.summary.strip()),
            "empty bodies": sum(1 for r in results if not r.body.strip()),
            "tex failures": sum(1 for r in results if not r.tex_ok),
        }

    out += ["## Totals", "",
            f"| metric | {a_name} | {b_name} |", "|---|---|---|"]
    sa, sb = score(a), score(b)
    for key in sa:
        out.append(f"| {key} | {sa[key]} | {sb[key]} |")

    out += ["", "## Render-critical markup", "",
            "Counts are source → backend. A drop means the backend stripped "
            "markup `render.py` needs; math must match exactly.", "",
            f"| # | marks in source | {a_name} | {b_name} |", "|---|---|---|---|"]
    for i, row in enumerate(rows):
        src = Marks.of(row.text)
        ma, mb = Marks.of(a[i].body), Marks.of(b[i].body)
        out.append(
            f"| {i} | fences {src.fences}, inline {src.inline}, math {src.math} "
            f"| fences {ma.fences}, inline {ma.inline}, math {ma.math} "
            f"| fences {mb.fences}, inline {mb.inline}, math {mb.math} |"
        )

    out += ["", "## Article by article", ""]
    for i, row in enumerate(rows):
        out += [f"### {i}. {row.title}", "", f"<{row.url}>", ""]
        for name, res in ((a_name, a[i]), (b_name, b[i])):
            out += [f"**{name} summary:** {res.summary or '_(empty)_'}", ""]
            if not res.tex_ok:
                out += [f"> tex_body failed: `{res.tex_error}`", ""]
        out += ["<details><summary>bodies</summary>", ""]
        for name, res in ((a_name, a[i]), (b_name, b[i])):
            out += [f"#### {name}", "", "```text", (res.body or "")[:4000], "```", ""]
        out += ["</details>", ""]
    return "\n".join(out)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a", default="anthropic", help="reference backend")
    p.add_argument("--b", default="vllm", help="candidate backend")
    p.add_argument("--state", type=Path, default=Path("state.db"))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--out", type=Path, default=Path("data/quality-diff.md"))
    args = p.parse_args()

    store = open_store(str(args.state))
    try:
        rows = [r for r in await store.all_rows() if r.text][: args.limit]
    finally:
        await store.close()
    if not rows:
        print(f"no extracted articles in {args.state}; run a gather first")
        return 2
    print(f"{len(rows)} articles from {args.state}")

    a = await run_backend(args.a, rows)
    b = await run_backend(args.b, rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report(rows, args.a, a, args.b, b), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
