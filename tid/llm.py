"""LLM backends.

`LLMBackend` is the seam most likely to be swapped — for a local model, for
routing, for per-plan quotas — so it is a protocol with explicit
implementations rather than a function that reads an env var. Nothing is
selected at import time: the CLI (or a downstream consumer) constructs a
backend and passes it in.

Backends carry their own batching limits. This is not a detail: the same
prompt that is comfortable for Haiku (8 articles × 16 000 chars, asking for
32 768 output tokens) is roughly 60k tokens and blows straight past a local
model's context window.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchLimits:
    """How much work one call to this backend can carry.

    `rewrite_output_tokens` is the budget for *one* article; the rewrite stage
    multiplies it by the batch size. Rewriting is close to 1:1, so it has to be
    at least as large as the input — a reply that runs out of budget mid-string
    is unparseable, and the article silently stays pending while the next run
    repeats the same expensive call.

    `max_output_tokens` is the hard per-request ceiling, so an oversized ask is
    clamped rather than rejected by the server.

    `max_concurrent` is the app-side default for in-flight batches. For vLLM it
    should match `--max-num-seqs`: the server does its own continuous batching,
    so oversubscribing past that only queues.
    """
    summarize_batch: int
    summarize_max_chars: int
    summary_output_tokens: int
    rewrite_batch: int
    rewrite_max_chars: int
    rewrite_output_tokens: int
    max_concurrent: int
    max_output_tokens: int


@dataclass
class Usage:
    """Token accounting, for the step-5 benchmark and for anyone metering."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


@runtime_checkable
class LLMBackend(Protocol):
    """One text-in/text-out model, plus what the caller needs to size batches."""

    name: str
    model: str
    limits: BatchLimits
    supports_json: bool
    usage: Usage

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """Single-shot completion. Always streams under the hood — large
        rewrite batches can exceed a non-streaming deadline, and a slow local
        model benefits from bytes-flowing keepalive through any proxy.

        `json_schema` asks for structured output. A backend that reports
        `supports_json = False` may ignore it; callers must cope either way.
        """
        ...

    async def aclose(self) -> None: ...


# --- Anthropic ------------------------------------------------------------

ANTHROPIC_LIMITS = BatchLimits(
    summarize_batch=8,
    summarize_max_chars=4000,
    summary_output_tokens=300,
    rewrite_batch=8,
    rewrite_max_chars=16000,
    rewrite_output_tokens=4096,
    max_concurrent=6,
    # 8 x 4096: Haiku's own output limit is far higher, so the ceiling only
    # exists to keep a mistake from turning into a 64k request.
    max_output_tokens=32768,
)


class AnthropicBackend:
    """Hosted Claude. Fast, and the reference for the step-5 quality diff."""

    name = "anthropic"
    supports_json = False  # the marker protocols are what this model is tuned on

    def __init__(
        self,
        model: str | None = None,
        limits: BatchLimits = ANTHROPIC_LIMITS,
        client: Any | None = None,
    ):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
        self.limits = limits
        self.usage = Usage()
        self._client = client
        self._owns_client = client is None

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic()
        return self._client

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        client = self._get_client()
        async with client.messages.stream(
            model=self.model,
            max_tokens=min(max_tokens, self.limits.max_output_tokens),
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            final = await stream.get_final_message()
        u = getattr(final, "usage", None)
        self.usage.add(
            getattr(u, "input_tokens", 0) or 0,
            getattr(u, "output_tokens", 0) or 0,
        )
        return final.content[0].text

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
            self._client = None


# --- vLLM (OpenAI-compatible) --------------------------------------------

# Sized for a 12B model on a 20 GB card with --max-model-len 16384. Rewrite
# batches of 1 keep one bad reply from taking several articles down with it.
#
# max_output_tokens is the number that bit us: rewriting is close to 1:1, so
# 12 000 input chars (~3 000 tokens) needs at least that many tokens back, and
# JSON escaping adds more. At 4096 the longest articles were truncated
# mid-string and the whole reply became unparseable. 8192 leaves the request
# well inside a 16k window (~700 system + ~3 000 article + 8 192 output).
VLLM_LIMITS = BatchLimits(
    summarize_batch=4,
    summarize_max_chars=3000,
    # A 40-word summary needs ~60 tokens; the rest is headroom for a model that
    # starts rambling. One that runs past even this truncates the reply, which
    # is why parse_json_batch salvages the items that did close.
    summary_output_tokens=512,
    rewrite_batch=1,
    rewrite_max_chars=12000,
    rewrite_output_tokens=8192,
    max_concurrent=4,
    max_output_tokens=8192,
)


class VLLMBackend:
    """A local vLLM server over its OpenAI-compatible API.

    Deliberately raw httpx rather than the `openai` SDK: the two endpoints used
    here are a POST and an SSE stream, and a self-hoster should not have to
    install an SDK for that.
    """

    name = "vllm"
    supports_json = True  # guided decoding via response_format=json_schema

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        limits: BatchLimits | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = (
            base_url or os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
        ).rstrip("/")
        self.model = model or os.environ.get("VLLM_MODEL", "google/gemma-3-12b-it")
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("VLLM_TIMEOUT", "1800")
        )
        max_concurrent = int(os.environ.get("VLLM_MAX_CONCURRENCY", "0") or 0)
        base = limits or VLLM_LIMITS
        self.limits = (
            replace(base, max_concurrent=max_concurrent)
            if max_concurrent else base
        )
        self.usage = Usage()
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            # include_usage adds a final chunk carrying prompt/completion
            # counts — that is where the benchmark numbers come from.
            "stream_options": {"include_usage": True},
            "max_tokens": min(max_tokens, self.limits.max_output_tokens),
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_schema is not None:
            # vLLM implements OpenAI's response_format with guided decoding, so
            # the reply is structurally valid by construction rather than by
            # hope. The regex parsers stay as the fallback path.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "tid_batch",
                    "schema": json_schema,
                    "strict": True,
                },
            }

        parts: list[str] = []
        finish_reason: str | None = None
        client = self._get_client()
        async with client.stream(
            "POST", f"{self.base_url}/chat/completions", json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"vllm {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                chunk = json.loads(data)
                if usage := chunk.get("usage"):
                    self.usage.add(
                        usage.get("prompt_tokens", 0) or 0,
                        usage.get("completion_tokens", 0) or 0,
                    )
                for choice in chunk.get("choices", []):
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        if finish_reason == "length":
            # Worth saying out loud: a truncated reply is usually unparseable,
            # so the article silently stays pending and the next run repeats
            # the same expensive call with the same result.
            log.warning(
                "vllm reply hit the %d-token output cap (finish_reason=length); "
                "the batch protocol will not parse. Lower the stage's input "
                "limit or raise max_output_tokens.",
                min(max_tokens, self.limits.max_output_tokens),
            )
        return "".join(parts)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


# --- Ollama ---------------------------------------------------------------

# Kept on purpose: it is the easiest local option for anyone without a GPU,
# which is a large share of self-hosters.
OLLAMA_LIMITS = BatchLimits(
    summarize_batch=2,
    summarize_max_chars=2500,
    summary_output_tokens=512,
    rewrite_batch=1,
    rewrite_max_chars=8000,
    rewrite_output_tokens=6144,
    max_concurrent=1,
    max_output_tokens=6144,
)


class OllamaBackend:
    name = "ollama"
    supports_json = True  # `format` accepts a JSON schema

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        limits: BatchLimits = OLLAMA_LIMITS,
        client: httpx.AsyncClient | None = None,
    ):
        self.host = (
            host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        ).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL", "mistral")
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("OLLAMA_TIMEOUT", "1800")
        )
        self.limits = limits
        self.usage = Usage()
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "options": {"num_predict": min(max_tokens, self.limits.max_output_tokens)},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_schema is not None:
            payload["format"] = json_schema
        parts: list[str] = []
        client = self._get_client()
        async with client.stream(
            "POST", f"{self.host}/api/chat", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if msg := chunk.get("message"):
                    parts.append(msg.get("content", ""))
                if chunk.get("done"):
                    self.usage.add(
                        chunk.get("prompt_eval_count", 0) or 0,
                        chunk.get("eval_count", 0) or 0,
                    )
                    break
        return "".join(parts)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


# --- construction ---------------------------------------------------------

BACKENDS = {
    "anthropic": AnthropicBackend,
    "vllm": VLLMBackend,
    "ollama": OllamaBackend,
}


def make_backend(name: str | None = None, **kwargs) -> LLMBackend:
    """Construct a backend by name. `None` reads LLM_BACKEND, default
    "anthropic". Env is only consulted here and inside the backends — never at
    import time, so one process can serve two backends at once."""
    key = (name or os.environ.get("LLM_BACKEND", "anthropic")).lower()
    try:
        cls = BACKENDS[key]
    except KeyError:
        raise ValueError(
            f"unknown LLM backend {key!r}; expected one of {sorted(BACKENDS)}"
        ) from None
    return cls(**kwargs)
