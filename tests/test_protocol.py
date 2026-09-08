"""The JSON-vs-LaTeX escaping hazard.

Witness (a real xelatex failure from a Gemma run): the model replies

    {"body": "the law $L(N, D) = A \\frac{N^{-\\alpha}}{D^{-\\beta}} + E$"}

which is *valid JSON* — `\\f` is the escape for a form feed. json.loads returns
`A <FF>rac{...}`, the LaTeX reaches xelatex as `A ^^Lrac{...}`, and the build
dies with `! Missing $ inserted`. Nothing upstream notices, because nothing
upstream is wrong.

These tests pin the repair, and equally pin what it must *not* touch: real
newlines separate paragraphs and real tabs indent code blocks.
"""
from __future__ import annotations

import json

from tid.protocol import parse_json_batch, repair_latex_escapes


def test_form_feed_becomes_frac_again():
    decoded = json.loads(r'{"b": "$L = A \frac{N}{D}$"}')["b"]
    assert "\f" in decoded, "precondition: JSON really does decode \\f"
    assert repair_latex_escapes(decoded) == "$L = A \\frac{N}{D}$"


def test_backspace_becomes_a_latex_b_command():
    decoded = json.loads(r'{"b": "$\beta$ and \begin{align}"}')["b"]
    assert repair_latex_escapes(decoded) == "$\\beta$ and \\begin{align}"


def test_a_command_that_is_not_a_json_escape_makes_the_reply_unparseable():
    """`\v` is not a legal JSON escape, so `\vec` cannot decode to something
    subtly wrong — the whole reply fails to parse and the caller falls back to
    the text protocol. That is why guided decoding matters: it forces the model
    to double the backslash for everything except the five escapes below."""
    assert parse_json_batch(r'{"articles":[{"id":0,"body":"$\vec{x}$"}]}',
                            "articles", "body", 1) is None


def test_lone_carriage_return_becomes_a_latex_r_command():
    decoded = json.loads(r'{"b": "$\rho$ and \right)"}')["b"]
    assert repair_latex_escapes(decoded) == "$\\rho$ and \\right)"


def test_crlf_line_endings_are_left_alone():
    """A CR that is part of CRLF is a line ending, not a mangled \\rho."""
    assert repair_latex_escapes("line one\r\nline two") == "line one\r\nline two"


def test_midword_tab_becomes_a_latex_t_command():
    decoded = json.loads(r'{"b": "$\theta$ and $x \times y$"}')["b"]
    assert repair_latex_escapes(decoded) == "$\\theta$ and $x \\times y$"


def test_indentation_tabs_survive():
    """Code blocks arrive fenced; turning their indentation into \\t would
    wreck them."""
    body = "```python\ndef f():\n\treturn 1\n```"
    assert repair_latex_escapes(body) == body


def test_paragraph_breaks_survive():
    body = "First paragraph.\n\nSecond paragraph."
    assert repair_latex_escapes(body) == body


def test_correctly_escaped_latex_is_untouched():
    """The model that does it right must not be punished for it."""
    decoded = json.loads(r'{"b": "$A \\frac{N}{D}$"}')["b"]
    assert decoded == "$A \\frac{N}{D}$"
    assert repair_latex_escapes(decoded) == "$A \\frac{N}{D}$"


# --- the batch reader ------------------------------------------------------

def test_parse_json_batch_places_items_by_id():
    reply = '{"articles":[{"id":1,"body":"two"},{"id":0,"body":"one"}]}'
    assert parse_json_batch(reply, "articles", "body", 2) == ["one", "two"]


def test_parse_json_batch_repairs_while_parsing():
    reply = r'{"articles":[{"id":0,"body":"$\frac{a}{b}$"}]}'
    assert parse_json_batch(reply, "articles", "body", 1) == ["$\\frac{a}{b}$"]


def test_parse_json_batch_returns_none_for_non_json():
    assert parse_json_batch("0. a numbered line", "articles", "body", 1) is None


def test_parse_json_batch_drops_rather_than_shifts_bad_ids():
    """A body landing under the wrong headline is worse than a missing one."""
    reply = '{"articles":[{"id":9,"body":"stray"},{"id":0,"body":"mine"}]}'
    assert parse_json_batch(reply, "articles", "body", 2) == ["mine", ""]


# --- the mirror-image mistake: over-escaping --------------------------------

def test_a_printed_backslash_n_becomes_a_line_break():
    """Seen in a real edition: "…certain thresholds.\\n Side note: these…"
    printed literally, mid-paragraph, in the PDF."""
    body = "certain thresholds.\\n Side note: these early works…"
    assert repair_latex_escapes(body) == "certain thresholds.\n Side note: these early works…"


def test_latex_commands_starting_with_n_are_not_line_breaks():
    body = "$\\nabla f$, $x \\neq y$, and \\newline stay put"
    assert repair_latex_escapes(body) == body


def test_escape_sequences_inside_fenced_code_are_left_alone():
    """`printf("hello\\n")` means exactly what it says."""
    body = 'Run:\n\n```c\nprintf("hello\\n");\n```\n\nDone.'
    assert repair_latex_escapes(body) == body


def test_escape_sequences_inside_inline_code_are_left_alone():
    body = 'The separator is `\\n` in that format.'
    assert repair_latex_escapes(body) == body


# --- truncated batches -----------------------------------------------------

def test_a_truncated_array_keeps_the_items_that_closed():
    """Witness: one runaway summary in a batch of 4 ran the reply into the
    output cap, and all four articles were lost. The three that finished are
    right there in the text."""
    reply = (
        '{"summaries": [{"id": 0, "summary": "first"}, '
        '{"id": 1, "summary": "second"}, '
        '{"id": 2, "summary": "third"}, '
        '{"id": 3, "summary": "fourth and then it keeps going and go'
    )
    assert parse_json_batch(reply, "summaries", "summary", 4) == [
        "first", "second", "third", "",
    ]


def test_salvage_does_not_kick_in_for_complete_replies():
    reply = '{"summaries": [{"id": 0, "summary": "only"}]}'
    assert parse_json_batch(reply, "summaries", "summary", 1) == ["only"]


def test_a_reply_truncated_before_any_item_closes_is_still_none():
    """Nothing usable means fall back to the text protocol, not invent data."""
    assert parse_json_batch('{"summaries": [{"id": 0, "summ',
                            "summaries", "summary", 2) is None


def test_salvaged_items_are_still_repaired():
    reply = r'{"articles": [{"id": 0, "body": "$\frac{a}{b}$"}, {"id": 1, "bo'
    assert parse_json_batch(reply, "articles", "body", 2) == ["$\\frac{a}{b}$", ""]
