"""Test doubles, shipped so that CI — here and downstream — needs no GPU, no
API key and no network.

    from papernews.testing import FakeBackend
    backend = FakeBackend()
    await cmd_ingest(client, store, backend, sources)
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .llm import BatchLimits, Usage

FAKE_LIMITS = BatchLimits(
    summarize_batch=4,
    summarize_max_chars=2000,
    rewrite_batch=2,
    rewrite_max_chars=4000,
    rewrite_output_tokens=2048,
    max_concurrent=2,
    max_output_tokens=4096,
)

_ARTICLE_ID_RE = re.compile(r"<article id=\"(\d+)\">")
_MARKER_ID_RE = re.compile(r"=== ARTICLE (\d+) START ===")


class FakeBackend:
    """A deterministic LLMBackend.

    Answers in whichever protocol the prompt asked for, so it exercises the
    real parsers rather than bypassing them. Every prompt is recorded in
    `calls`, and `respond` can be replaced to simulate a malformed reply,
    a refusal, or a timeout.
    """

    name = "fake"
    model = "fake-1"

    def __init__(
        self,
        limits: BatchLimits = FAKE_LIMITS,
        supports_json: bool = False,
        respond: Callable[[str, str], str] | None = None,
    ):
        self.limits = limits
        self.supports_json = supports_json
        self.usage = Usage()
        self.calls: list[dict[str, Any]] = []
        self._respond = respond

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append({
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "json_schema": json_schema,
        })
        self.usage.add(len(user) // 4, 64)
        if self._respond is not None:
            return self._respond(system, user)
        if _ARTICLE_ID_RE.search(user):
            return self._summaries(user, json_schema is not None)
        if _MARKER_ID_RE.search(user):
            return self._rewrites(user, json_schema is not None)
        # The world-news bullets: one numbered line per input line.
        lines = [ln for ln in user.splitlines() if ln.strip()]
        return "\n".join(f"{i + 1}. bullet {i}" for i in range(len(lines)))

    def _summaries(self, user: str, as_json: bool) -> str:
        ids = [int(i) for i in _ARTICLE_ID_RE.findall(user)]
        if as_json:
            return json.dumps(
                {"summaries": [{"id": i, "summary": f"Summary of article {i}."}
                               for i in ids]}
            )
        return "\n".join(f"{i}. Summary of article {i}." for i in ids)

    def _rewrites(self, user: str, as_json: bool) -> str:
        ids = [int(i) for i in _MARKER_ID_RE.findall(user)]
        body = "Rewritten paragraph one.\n\nRewritten paragraph two."
        if as_json:
            return json.dumps(
                {"articles": [{"id": i, "body": body} for i in ids]}
            )
        return "\n\n".join(
            f"=== ARTICLE {i} START ===\n{body}\n=== ARTICLE {i} END ==="
            for i in ids
        )

    async def aclose(self) -> None:
        return None
