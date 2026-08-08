"""LLM backend tests.

No network: the vLLM and Ollama backends are driven through
httpx.MockTransport, which is enough to pin the two things that actually break
— the streaming wire format and whether the guided-decoding request is shaped
the way the server expects.
"""
from __future__ import annotations

import json

import httpx
import pytest

from papernews.llm import (
    ANTHROPIC_LIMITS,
    VLLM_LIMITS,
    AnthropicBackend,
    LLMBackend,
    OllamaBackend,
    VLLMBackend,
    make_backend,
)
from papernews.testing import FakeBackend


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _vllm_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- the protocol ---------------------------------------------------------

def test_every_shipped_backend_satisfies_the_protocol():
    for backend in (AnthropicBackend(), VLLMBackend(), OllamaBackend(),
                    FakeBackend()):
        assert isinstance(backend, LLMBackend)


def test_make_backend_reads_the_env_but_not_at_import_time(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vllm")
    assert make_backend().name == "vllm"
    monkeypatch.setenv("LLM_BACKEND", "anthropic")
    assert make_backend().name == "anthropic"
    # An explicit name always wins over the environment.
    assert make_backend("ollama").name == "ollama"


def test_make_backend_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        make_backend("gpt-9")


def test_local_backends_ask_for_much_smaller_batches_than_anthropic():
    """The concrete bug this guards: 8 articles x 16 000 chars asking for
    4096*8 output tokens is ~60k tokens, far past a 16k max-model-len."""
    assert VLLM_LIMITS.rewrite_batch < ANTHROPIC_LIMITS.rewrite_batch
    assert VLLM_LIMITS.rewrite_max_chars < ANTHROPIC_LIMITS.rewrite_max_chars
    est_tokens = (
        VLLM_LIMITS.rewrite_batch
        * (VLLM_LIMITS.rewrite_max_chars / 4 + VLLM_LIMITS.max_output_tokens)
    )
    assert est_tokens < 16384


# --- vLLM ----------------------------------------------------------------

async def test_vllm_streams_and_concatenates_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 2}},
        ))

    backend = VLLMBackend(client=_vllm_client(handler))
    assert await backend.chat("sys", "user", max_tokens=100) == "Hello"
    assert backend.usage.calls == 1
    assert backend.usage.input_tokens == 11
    assert backend.usage.output_tokens == 2


async def test_vllm_asks_for_guided_json_when_given_a_schema():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": "{}"}}]}
        ))

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    backend = VLLMBackend(client=_vllm_client(handler))
    await backend.chat("sys", "user", max_tokens=50, json_schema=schema)

    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == schema
    assert seen["stream"] is True
    assert seen["stream_options"]["include_usage"] is True


async def test_vllm_clamps_max_tokens_to_the_backend_limit():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": "ok"}}]}
        ))

    backend = VLLMBackend(client=_vllm_client(handler))
    await backend.chat("sys", "user", max_tokens=1_000_000)
    assert seen["max_tokens"] == VLLM_LIMITS.max_output_tokens


async def test_vllm_surfaces_an_http_error_with_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "context length exceeded"})

    backend = VLLMBackend(client=_vllm_client(handler))
    with pytest.raises(RuntimeError, match="context length exceeded"):
        await backend.chat("sys", "user", max_tokens=50)


async def test_vllm_ignores_keepalive_and_malformed_lines():
    """vLLM (and any proxy in front of it) can emit blank lines and SSE
    comments; a stray one must not abort the stream."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            ": keepalive\n\n"
            "\n"
            'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            "data: \n\n"
            'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode())

    backend = VLLMBackend(client=_vllm_client(handler))
    assert await backend.chat("sys", "user", max_tokens=10) == "ab"


def test_vllm_concurrency_can_be_matched_to_max_num_seqs(monkeypatch):
    monkeypatch.setenv("VLLM_MAX_CONCURRENCY", "2")
    assert VLLMBackend().limits.max_concurrent == 2
    # Everything else keeps its default.
    assert VLLMBackend().limits.rewrite_batch == VLLM_LIMITS.rewrite_batch


# --- Ollama ---------------------------------------------------------------

async def test_ollama_streams_ndjson_and_records_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            json.dumps({"message": {"content": "one "}}) + "\n"
            + json.dumps({"message": {"content": "two"}}) + "\n"
            + json.dumps({"done": True, "prompt_eval_count": 7,
                          "eval_count": 3}) + "\n"
        )
        return httpx.Response(200, content=body.encode())

    backend = OllamaBackend(client=_vllm_client(handler))
    assert await backend.chat("sys", "user", max_tokens=10) == "one two"
    assert backend.usage.output_tokens == 3
