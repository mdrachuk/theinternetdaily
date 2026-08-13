r"""Shared parsing for the batched LLM wire protocols.

Both stages send N articles in one call and match replies back by id. Two wire
formats are supported — JSON (where the backend can enforce a schema) and
positional text markers — and this module owns the JSON half plus the one
repair it needs.

**The escaping hazard.** LaTeX and JSON disagree about backslashes, and they
disagree *silently*. A model writing

    {"body": "the law $L = A \frac{N}{D}$"}

has produced perfectly valid JSON: `\f` is the JSON escape for a form feed. It
decodes to `A <FF>rac{N}{D}`, xelatex reports `! Missing $ inserted`, and the
edition fails to build — with no error anywhere upstream. `\begin` becomes a
backspace, `\times` a tab, `\right` a carriage return.

Only five escapes are at risk: `\f`, `\b`, `\n`, `\r`, `\t`. Everything else a
LaTeX command can start with (`\alpha`, `\mathbb`, `\sum`) is not a legal JSON
escape at all, so guided decoding forces the model to write `\\` there and the
text survives intact. It is also why an unguided backend fails loudly rather
than quietly: the reply simply is not JSON, and the caller falls back to the
text protocol.

Of the five, `\f`, `\b` and a lone `\r` can never legitimately appear in an
article body, so the backslash is restored unconditionally. Tabs are ambiguous
— they indent code blocks — so one is only restored when the line already has
text on it, which indentation never does. Newlines are left alone entirely:
they carry the paragraph structure, and a mangled `\nabla` costs one wrong word
rather than a broken build.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Control characters that cannot appear in prose, mapped back to the LaTeX
# command they were before json.loads ate them.
_CONTROL_TO_BACKSLASH = {
    "\f": "\\f",   # \frac, \forall, \flat…
    "\b": "\\b",   # \begin, \beta, \bar…
}

# A lone CR (not part of CRLF) is \right, \rho, \rangle…
_LONE_CR_RE = re.compile(r"\r(?!\n)")

# The mirror-image mistake: the model *double*-escapes a line break, so the
# body carries the two literal characters `\` and `n` and the reader sees "\n"
# printed in the middle of a sentence. Only repaired when what follows is not a
# letter, so `\nabla`, `\neq` and `\newline` are left alone...
_LITERAL_NEWLINE_RE = re.compile(r"\\n(?![A-Za-z])")

# ...and only outside code, where `printf("hello\n")` means exactly what it says.
_CODE_SEGMENT_RE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)


def _repair_tabs(line: str) -> str:
    r"""Restore ``\t``-style commands without touching indentation."""
    if "\t" not in line:
        return line
    out: list[str] = []
    seen_text = False
    for ch in line:
        if ch == "\t" and seen_text:
            out.append("\\t")
            continue
        if not ch.isspace():
            seen_text = True
        out.append(ch)
    return "".join(out)


def _repair_literal_newlines(text: str) -> str:
    """Turn a printed ``\n`` back into a line break, outside code spans."""
    return "".join(
        part if i % 2 else _LITERAL_NEWLINE_RE.sub("\n", part)
        for i, part in enumerate(_CODE_SEGMENT_RE.split(text))
    )


def repair_latex_escapes(text: str) -> str:
    """Undo the two ways a model can get backslashes wrong in JSON: escapes it
    should have doubled, and line breaks it doubled when it should not have."""
    for ch, replacement in _CONTROL_TO_BACKSLASH.items():
        text = text.replace(ch, replacement)
    text = _LONE_CR_RE.sub("\\\\r", text)
    text = "\n".join(_repair_tabs(line) for line in text.split("\n"))
    return _repair_literal_newlines(text)


def _outermost_object(reply: str) -> str | None:
    """The JSON object in a reply, even if wrapped in prose or a code fence."""
    blob = reply.strip()
    if blob.startswith("{"):
        return blob
    start, end = blob.find("{"), blob.rfind("}")
    if start < 0 or end <= start:
        return None
    return blob[start:end + 1]


def _salvage_items(blob: str) -> list[Any]:
    """Pull the complete objects out of a truncated JSON array.

    A reply that runs out of output budget mid-array is not valid JSON, but the
    items before the cut are intact. Recovering them turns "one runaway item
    costs the whole batch" into "one runaway item costs one article" — which,
    with a batch of 4, is the difference between 0 and 3 usable summaries.
    """
    decoder = json.JSONDecoder()
    items: list[Any] = []
    i = blob.find("{")
    while i >= 0:
        try:
            obj, end = decoder.raw_decode(blob, i)
        except ValueError:
            i = blob.find("{", i + 1)
            continue
        if isinstance(obj, dict) and "id" in obj:
            items.append(obj)
            i = blob.find("{", end)
        else:
            i = blob.find("{", i + 1)
    return items


def parse_json_batch(
    reply: str, container: str, field: str, n: int
) -> list[str] | None:
    """Parse ``{container: [{id, field}, …]}`` into n strings, placed by id.

    Returns None when the reply is not JSON at all, so the caller can fall back
    to the text protocol. Items with a missing, out-of-range or wrongly typed
    id are dropped rather than shifted onto another article — a body landing
    under the wrong headline is worse than a missing one.
    """
    blob = _outermost_object(reply)
    if blob is None:
        return None
    try:
        data: Any = json.loads(blob)
        items = (data.get(container) or []) if isinstance(data, dict) else None
    except ValueError:
        # Truncated: keep whatever closed before the cut.
        items = _salvage_items(blob)
        if not items:
            return None
    if items is None:
        return None
    out = [""] * n
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        value = item.get(field)
        if 0 <= idx < n and isinstance(value, str):
            out[idx] = repair_latex_escapes(value).strip()
    return out
