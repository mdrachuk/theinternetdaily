from __future__ import annotations

import json
import os


def _backend_name() -> str:
    """Read the backend per call, not at import time — importing this module
    must not freeze a process-wide choice (see plan step 6)."""
    return os.environ.get("LLM_BACKEND", "anthropic").lower()


async def chat(system: str, user: str, max_tokens: int) -> str:
    """Single-shot chat. Always streams under the hood — large rewrite batches
    can exceed the API's non-streaming deadline, and a slow local model
    benefits from bytes-flowing keepalive through any reverse proxy."""
    if _backend_name() == "ollama":
        return await _ollama(system, user, max_tokens)
    return await _anthropic(system, user, max_tokens)


async def _anthropic(system: str, user: str, max_tokens: int) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    async with client:
        async with client.messages.stream(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            final = await stream.get_final_message()
    return final.content[0].text


async def _ollama(system: str, user: str, max_tokens: int) -> str:
    import httpx

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "mistral")
    timeout = float(os.environ.get("OLLAMA_TIMEOUT", "1800"))
    parts: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{host}/api/chat",
            json={
                "model": model,
                "stream": True,
                "options": {"num_predict": max_tokens},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if msg := chunk.get("message"):
                    parts.append(msg.get("content", ""))
                if chunk.get("done"):
                    break
    return "".join(parts)
