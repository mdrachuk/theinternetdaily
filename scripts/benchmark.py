#!/usr/bin/env python
"""Benchmark one LLM backend on a real ingest.

    uv run python scripts/benchmark.py --backend vllm --state data/bench.db
    uv run python scripts/benchmark.py --backend anthropic --skip-gather

Reports per-stage wall clock, tokens/s and peak VRAM as a markdown table, ready
to paste into the README. Nothing here is imported by the package.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from tid.cli import (
    cmd_gather,
    cmd_summarize,
    cmd_topics,
    collect_current_edition,
    gather_decorations,
)
from tid.config import load_sources, load_topics
from tid.http import client_context
from tid.llm import make_backend
from tid.render import build_pdf
from tid.store import open_store
from tid.topics import standing_topics


class VramSampler:
    """Polls nvidia-smi for peak used VRAM. A no-op where it isn't available,
    which keeps this script usable against a hosted backend."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.peak_mib = 0
        self.samples = 0
        self.available = shutil.which("nvidia-smi") is not None
        self._task: asyncio.Task | None = None

    def _read(self) -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return None
        if out.returncode != 0:
            return None
        try:
            return max(int(line.strip()) for line in out.stdout.splitlines()
                       if line.strip())
        except ValueError:
            return None

    async def _loop(self) -> None:
        while True:
            value = await asyncio.to_thread(self._read)
            if value is None:
                self.available = False
                return
            self.peak_mib = max(self.peak_mib, value)
            self.samples += 1
            await asyncio.sleep(self.interval)

    @asynccontextmanager
    async def running(self):
        if self.available:
            self._task = asyncio.create_task(self._loop())
        try:
            yield self
        finally:
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass


class Stage:
    def __init__(self, name: str, usage):
        self.name = name
        self._usage = usage

    async def __aenter__(self):
        self.t0 = time.perf_counter()
        self.calls0 = self._usage.calls
        self.in0 = self._usage.input_tokens
        self.out0 = self._usage.output_tokens
        return self

    async def __aexit__(self, *exc):
        self.seconds = time.perf_counter() - self.t0
        self.calls = self._usage.calls - self.calls0
        self.input_tokens = self._usage.input_tokens - self.in0
        self.output_tokens = self._usage.output_tokens - self.out0
        return False

    @property
    def output_tps(self) -> float:
        return self.output_tokens / self.seconds if self.seconds else 0.0

    def row(self, items: int | None = None) -> str:
        tps = f"{self.output_tps:,.1f}" if self.output_tokens else "—"
        return (
            f"| {self.name} | {self.seconds:,.1f} s | "
            f"{items if items is not None else '—'} | {self.calls or '—'} | "
            f"{self.input_tokens or '—'} | {self.output_tokens or '—'} | {tps} |"
        )


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", default=None,
                   help="anthropic | vllm | ollama (default: $LLM_BACKEND)")
    p.add_argument("--state", type=Path, default=Path("data/bench.db"),
                   help="store to use; a scratch file keeps your real one clean")
    p.add_argument("--config", type=Path, default=Path("sources.toml"))
    p.add_argument("--out", type=Path, default=Path("data/bench"))
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--skip-gather", action="store_true",
                   help="reuse whatever is already in the store")
    p.add_argument("--skip-render", action="store_true")
    args = p.parse_args()

    sources = load_sources(args.config)
    standing = standing_topics(load_topics(args.config))
    store = open_store(str(args.state))
    backend = make_backend(args.backend)
    sampler = VramSampler()

    print(f"backend : {backend.name} ({backend.model})")
    print(f"limits  : {backend.limits}")
    print(f"store   : {args.state}")
    print()

    rows: list[str] = []
    try:
        async with sampler.running(), client_context() as client:
            if not args.skip_gather:
                async with Stage("gather + extract", backend.usage) as st:
                    await cmd_gather(client, store, sources)
                rows.append(st.row((await store.counts())["total"]))

            pending_summary = len(await store.pending_summary())
            async with Stage("summarize", backend.usage) as st:
                await cmd_summarize(store, backend, args.workers)
            rows.append(st.row(pending_summary))

            # Counted before the stage runs, like the others: this is the set
            # of articles the topic passes will read, file and rank.
            pending_topics = len(await store.pending_render())
            async with Stage("topics", backend.usage) as st:
                await cmd_topics(store, backend, args.workers, standing)
            rows.append(st.row(pending_topics))

            articles = await collect_current_edition(store, sources)
            if not args.skip_render:
                async with Stage("decorations", backend.usage) as st:
                    decorations = await gather_decorations(client, backend)
                rows.append(st.row())

                async with Stage("render (xelatex)", backend.usage) as st:
                    args.out.mkdir(parents=True, exist_ok=True)
                    pdf = await build_pdf(
                        time.strftime("%Y-%m-%d"), articles, args.out,
                        decorations=decorations,
                    )
                rows.append(st.row(len(articles)))
                print(f"\nPDF: {pdf} ({pdf.stat().st_size / 1e6:.1f} MB)")
    finally:
        await backend.aclose()
        await store.close()

    print()
    print(f"### {backend.name} — {backend.model}")
    print()
    print("| stage | wall clock | items | calls | in tok | out tok | out tok/s |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        print(row)
    print()
    print(f"- articles in edition : {len(articles)}")
    print(f"- total LLM calls     : {backend.usage.calls}")
    print(f"- total tokens        : {backend.usage.input_tokens} in / "
          f"{backend.usage.output_tokens} out")
    if sampler.available and sampler.peak_mib:
        print(f"- peak VRAM           : {sampler.peak_mib} MiB "
              f"({sampler.samples} samples)")
    else:
        print("- peak VRAM           : n/a (no working nvidia-smi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
