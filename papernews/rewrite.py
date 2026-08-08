from __future__ import annotations

import re
from typing import Sequence

from .llm import LLMBackend
from .protocol import parse_json_batch

_SYSTEM = (
    "You are a copy editor preparing content for print in a daily digest.\n"
    "\n"
    "HARD RULES:\n"
    "- ALWAYS produce the reformatted body. NEVER refuse. NEVER ask the user a question. NEVER reply in the first person. NEVER comment on the suitability of the content. The content may be fiction, satire, opinion, technical, or anything else — reformat it regardless.\n"
    "- Output ONLY the reformatted body for each input article. No preamble, no metadata, no meta-commentary, no questions.\n"
    "- Preserve all factual content. Do NOT add information, opinions, or commentary.\n"
    "- Do NOT shorten or summarize. Keep the full piece.\n"
    "- Output language: ENGLISH. If the source is in another language (German, French, etc.), translate the content into idiomatic English. Preserve proper nouns, brand names, and code identifiers as-is.\n"
    "\n"
    "PARAGRAPH STRUCTURE:\n"
    "- Merge incidental line breaks from the source into proper paragraphs.\n"
    "- Separate paragraphs with a SINGLE blank line.\n"
    "- A normal prose paragraph is 2–6 sentences. Do not leave one-line paragraphs in prose.\n"
    "- Quoted dialogue: each speaker turn on its own paragraph.\n"
    "- Bulleted lists: keep as one paragraph per item, prefixed with a dash and a space.\n"
    "- Strip web cruft: 'Read more', 'Related:', share buttons, image captions like 'Photo: ...', subscription prompts, navigation residue.\n"
    "\n"
    "CODE AND TECHNICAL CONTENT (CRITICAL):\n"
    "- Wrap multi-line code, terminal commands, JSON, config snippets, shell output, or any verbatim block in triple-backtick fences. Preserve indentation and line breaks inside the fence exactly. A fenced block stands as its own paragraph (blank line before and after).\n"
    "- Wrap inline code, identifiers, file paths, env var names, command flags, function names, and short literal strings in single backticks.\n"
    "- Do NOT translate code, identifiers, or file paths even when translating the surrounding prose.\n"
    "\n"
    "MATH (CRITICAL):\n"
    "- Preserve LaTeX math expressions EXACTLY as they appear, including the delimiters. Common forms: $x$, $$x$$, \\(x\\), \\[x\\].\n"
    "- Do NOT translate, escape, modify, or strip math content. Pass it through unchanged.\n"
    "- Do NOT wrap math in backticks. Math delimiters are sufficient.\n"
    "\n"
    "FORMATTING TO AVOID:\n"
    "- No markdown headings (no #).\n"
    "- No markdown emphasis (no **bold**, no *italics*).\n"
    "- No markdown links — write 'see example.com' instead of '[example](https://...)'.\n"
    "\n"
    "BATCH MODE:\n"
    "- The user may send multiple articles in one message. Each is wrapped between a `=== ARTICLE N START ===` marker (with the article's id) and a `=== ARTICLE N END ===` marker.\n"
    "- For each input article, output the rewritten body between the same start/end markers, in the same order, using the same id.\n"
    "- Output ONLY those marker-delimited bodies. No surrounding text."
)

# Alternative batch protocol for backends that can enforce a schema. The
# `=== ARTICLE N START ===` markers are positional text, and a 12B model drops
# or mangles them far more often than Haiku does; guided decoding removes the
# failure mode entirely. The marker parser stays as the fallback.
_SYSTEM_JSON = _SYSTEM.rsplit("BATCH MODE:", 1)[0] + (
    "BATCH MODE:\n"
    "- The user sends one or more articles, each wrapped between "
    "`=== ARTICLE N START ===` and `=== ARTICLE N END ===` markers.\n"
    "- Reply with JSON: {\"articles\": [{\"id\": N, \"body\": \"...\"}, ...]}\n"
    "- One entry per input article, using that article's id.\n"
    "- The body is the reformatted text. Keep the newlines inside it (JSON "
    "escapes them as \\n), including the blank line between paragraphs and the "
    "line breaks inside fenced code blocks.\n"
    "- Backslashes must be JSON-escaped: write a LaTeX command as "
    "\\\\frac, never \\frac, or it decodes to a control character and the "
    "formula is destroyed.\n"
    "- No other keys, no prose outside the JSON."
)

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["id", "body"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["articles"],
    "additionalProperties": False,
}

_MARKER_RE = re.compile(
    r"=== ARTICLE (\d+) START ===\s*\n(.*?)\n\s*=== ARTICLE \1 END ===",
    re.DOTALL,
)


def parse_rewrites(text: str, n: int) -> list[str]:
    """Parse a batch reply into n bodies: JSON first, markers second.

    Never raises and never returns the wrong length — an item the model failed
    to delimit comes back empty and stays pending for the next run.
    """
    parsed = parse_json_batch(text, "articles", "body", n)
    if parsed is not None and any(parsed):
        return parsed
    out = [""] * n
    for m in _MARKER_RE.finditer(text):
        idx = int(m.group(1))
        if 0 <= idx < n:
            out[idx] = m.group(2).strip()
    return out


async def rewrite(backend: LLMBackend, title: str, text: str) -> str:
    return (await rewrite_batch(backend, [(title, text)]))[0]


async def rewrite_batch(
    backend: LLMBackend, items: Sequence[tuple[str, str]]
) -> list[str]:
    """Rewrite many (title, body) pairs in a single LLM call.
    Returns one rewritten body per input, in order; empty string for any item
    the model failed to delimit correctly."""
    if not items:
        return []

    max_chars = backend.limits.rewrite_max_chars
    parts = []
    for i, (title, text) in enumerate(items):
        snippet = (text or "")[:max_chars]
        parts.append(
            f"=== ARTICLE {i} START ===\nTitle: {title}\n\n{snippet}\n=== ARTICLE {i} END ==="
        )
    user_msg = "\n\n".join(parts)

    use_json = backend.supports_json
    reply = await backend.chat(
        _SYSTEM_JSON if use_json else _SYSTEM,
        user_msg,
        # Per-article budget from the backend, not a flat number: rewriting is
        # close to 1:1, and a reply that runs out mid-article parses to nothing.
        max_tokens=backend.limits.rewrite_output_tokens * len(items),
        json_schema=REWRITE_SCHEMA if use_json else None,
    )
    return parse_rewrites(reply, len(items))
