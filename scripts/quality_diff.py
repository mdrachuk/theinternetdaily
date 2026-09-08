#!/usr/bin/env python
"""Quality report for one or more LLM backends, side by side.

    # score what an ingest already produced (no further LLM calls)
    uv run python scripts/quality_diff.py --state data/bench.db --columns store

    # run the same N articles through two backends and compare
    uv run python scripts/quality_diff.py --columns anthropic,vllm \
        --state data/bench.db --limit 20 --out data/quality-diff.md

Takes the same N articles through each column and writes a markdown report.
A column is either a backend name (which runs it) or the literal `store`,
meaning "use the summary already saved for that article" — useful after a
benchmark run, and the only option when there is no second API key.

The summary is the one piece of text the model writes: the article body is
printed as extracted, so what the report compares is the dek — is it there,
is it within the 40-word cap the prompt asks for, and does it read.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from tid.llm import make_backend
from tid.store import open_store
from tid.summarize import summarize_batch


@dataclass
class Result:
    summary: str


def read_store(rows) -> list[Result]:
    """No LLM calls: score whatever the pipeline already stored."""
    return [Result(r.summary or "") for r in rows]


async def run_backend(name: str, rows) -> list[Result]:
    backend = make_backend(name)
    try:
        items = [(r.title, r.text or "") for r in rows]
        summaries: list[str] = []
        size = backend.limits.summarize_batch
        for i in range(0, len(items), size):
            summaries += await summarize_batch(backend, items[i:i + size])
        print(f"{name}: {backend.usage.calls} calls, "
              f"{backend.usage.output_tokens} output tokens")
        return [Result(s) for s in summaries]
    finally:
        await backend.aclose()


def _score(results: list[Result]) -> dict[str, object]:
    lengths = [len(r.summary.split()) for r in results if r.summary.strip()]
    return {
        "articles": len(results),
        "empty summaries": sum(1 for r in results if not r.summary.strip()),
        "summaries over 40 words": sum(1 for n in lengths if n > 40),
        "median summary words": (sorted(lengths)[len(lengths) // 2]
                                 if lengths else 0),
    }


def report(rows, columns: list[tuple[str, list[Result]]]) -> str:
    names = [n for n, _ in columns]
    out: list[str] = [f"# Quality report: {' vs '.join(names)}", ""]

    scores = [(n, _score(r)) for n, r in columns]
    out += ["## Totals", "",
            "| metric | " + " | ".join(names) + " |",
            "|---" * (len(names) + 1) + "|"]
    for key in scores[0][1]:
        out.append(f"| {key} | "
                   + " | ".join(str(s[key]) for _, s in scores) + " |")

    out += ["", "## Article by article", ""]
    for i, row in enumerate(rows):
        out += [f"### {i}. {row.title}", "", f"<{row.url}>", ""]
        for name, res in columns:
            out += [f"**{name}:** {res[i].summary or '_(empty)_'}", ""]
    return "\n".join(out)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--columns", default="store",
                   help="comma-separated: backend names, and/or `store` to "
                        "score what the pipeline already saved")
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

    columns: list[tuple[str, list[Result]]] = []
    for name in [c.strip() for c in args.columns.split(",") if c.strip()]:
        columns.append(
            (name, read_store(rows) if name == "store"
             else await run_backend(name, rows))
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report(rows, columns), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
