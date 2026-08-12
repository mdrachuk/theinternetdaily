from __future__ import annotations

import re
from typing import Sequence

from .llm import LLMBackend
from .protocol import parse_json_batch

_SYSTEM = (
    "You write a 2-sentence summary of a piece of content for a daily digest.\n"
    "\n"
    "HARD RULES:\n"
    "- ALWAYS output a summary. NEVER refuse. NEVER ask the user a question. NEVER reply in the first person. NEVER comment on the suitability of the content.\n"
    "- The piece may be a news article, blog post, Show HN, discussion thread, fiction, satire, opinion, product launch, release notes, paper, or anything else. Summarize whatever it is. Fiction → summarize the plot. Show HN → say what the project does. Opinion → state the position.\n"
    "- Be terse and factual. State what the piece is about and the main point or takeaway. No filler. No 'this article discusses', 'the author argues', 'the piece explores'.\n"
    "- Hard cap: 40 words across the 2 sentences.\n"
    "- Output language: ENGLISH, regardless of the source language. Translate if needed.\n"
    "- Output ONLY the summary text. No preamble, no quotes, no markdown, no questions, no meta-commentary.\n"
    "\n"
    "BATCH MODE:\n"
    "- The user may send multiple articles in one message, each wrapped in a numbered <article id=\"N\"> block.\n"
    "- For each article, emit one summary on its own line, prefixed with `N. ` (the article's id and a period).\n"
    "- Output ONLY those summary lines, in the same order as the input. No surrounding text."
)

# Alternative instruction tail used when the backend can enforce a schema.
# Positional text markers are what a 12B model breaks most often, so where
# guided decoding is available the protocol becomes JSON and the markers
# survive only as the fallback parser.
_SYSTEM_JSON = _SYSTEM.rsplit("BATCH MODE:", 1)[0] + (
    "BATCH MODE:\n"
    "- The user sends one or more articles, each wrapped in a numbered "
    "<article id=\"N\"> block.\n"
    "- Reply with JSON: {\"summaries\": [{\"id\": N, \"summary\": \"...\"}, ...]}\n"
    "- One entry per input article, using that article's id. No other keys, no "
    "prose outside the JSON."
)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["summaries"],
    "additionalProperties": False,
}

_LINE_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.*\S)\s*$")


def parse_summaries(text: str, n: int) -> list[str]:
    """Parse a batch reply into n summaries, JSON first, numbered lines second.

    Never raises and never returns the wrong length: an item the model failed
    to label comes back as an empty string, which the caller counts as an error
    and leaves pending for the next run.
    """
    parsed = parse_json_batch(text, "summaries", "summary", n)
    if parsed is not None and any(parsed):
        return parsed
    out = [""] * n
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        if 0 <= idx < n:
            out[idx] = m.group(2)
    return out


async def summarize(backend: LLMBackend, title: str, text: str) -> str:
    return (await summarize_batch(backend, [(title, text)]))[0]


async def summarize_batch(
    backend: LLMBackend, items: Sequence[tuple[str, str]]
) -> list[str]:
    """Summarize many (title, body) pairs in a single LLM call.
    Returns one summary per input, in order. Falls back to an empty string for
    any item the model failed to label correctly."""
    if not items:
        return []

    max_chars = backend.limits.summarize_max_chars
    parts = []
    for i, (title, text) in enumerate(items):
        snippet = (text or "")[:max_chars]
        parts.append(
            f"<article id=\"{i}\">\n<title>{title}</title>\n<body>\n{snippet}\n</body>\n</article>"
        )
    user_msg = "\n\n".join(parts)

    use_json = backend.supports_json
    reply = await backend.chat(
        _SYSTEM_JSON if use_json else _SYSTEM,
        user_msg,
        max_tokens=backend.limits.summary_output_tokens * len(items),
        json_schema=SUMMARY_SCHEMA if use_json else None,
    )
    return parse_summaries(reply.strip(), len(items))
