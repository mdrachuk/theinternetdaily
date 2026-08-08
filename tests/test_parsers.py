"""Batch-protocol parser regressions.

Both stages send N articles in one call and match the replies back up by
position or id. When that mapping goes wrong the failure is silent and ugly:
article 3 gets article 5's body. These fixtures pin the behaviour for good
replies, for both wire formats, and — the part that matters with a 12B model —
for the many ways a reply can be malformed.

The contract, for either parser:
  * always returns exactly n items
  * never raises
  * an item the model got wrong comes back as "" (the caller counts it as an
    error and leaves the article pending, rather than storing nonsense)
"""
from __future__ import annotations

import json

import pytest

from papernews.rewrite import parse_rewrites
from papernews.summarize import parse_summaries

BODY = "First paragraph.\n\nSecond paragraph."


def _markers(*bodies: tuple[int, str]) -> str:
    return "\n\n".join(
        f"=== ARTICLE {i} START ===\n{b}\n=== ARTICLE {i} END ==="
        for i, b in bodies
    )


# --- summaries: the good paths -------------------------------------------

def test_numbered_lines():
    assert parse_summaries("0. one\n1. two", 2) == ["one", "two"]


def test_numbered_lines_out_of_order_are_placed_by_id():
    assert parse_summaries("1. two\n0. one", 2) == ["one", "two"]


def test_numbered_lines_tolerate_a_paren_and_stray_whitespace():
    assert parse_summaries("  0)   one  \n\n 1.  two", 2) == ["one", "two"]


def test_json_protocol():
    reply = json.dumps({"summaries": [{"id": 0, "summary": "one"},
                                      {"id": 1, "summary": "two"}]})
    assert parse_summaries(reply, 2) == ["one", "two"]


def test_json_wrapped_in_prose_or_a_fence():
    reply = ("Sure, here you go:\n```json\n"
             + json.dumps({"summaries": [{"id": 0, "summary": "one"}]})
             + "\n```\nHope that helps!")
    assert parse_summaries(reply, 1) == ["one"]


# --- summaries: the bad paths --------------------------------------------

@pytest.mark.parametrize("reply", [
    "",
    "   \n\n  ",
    "I cannot summarize this content.",
    "one\ntwo",                       # forgot the numbers entirely
    "{",                              # truncated JSON
    '{"summaries": null}',
    '{"summaries": [{"id": "x", "summary": "one"}]}',   # id not an integer
    '{"summaries": [{"id": 0}]}',                       # missing the summary
    '{"summaries": [{"id": 0, "summary": 42}]}',        # wrong type
    "[]",                                               # a list, not an object
])
def test_malformed_summary_replies_yield_blanks_not_exceptions(reply):
    assert parse_summaries(reply, 2) == ["", ""]


def test_out_of_range_ids_are_dropped_not_mapped_onto_other_articles():
    reply = json.dumps({"summaries": [{"id": 7, "summary": "stray"},
                                      {"id": 0, "summary": "mine"}]})
    assert parse_summaries(reply, 2) == ["mine", ""]


def test_a_partial_batch_keeps_the_good_items():
    """One bad item must not cost the whole batch — the rest are stored and
    only the blank one stays pending."""
    assert parse_summaries("0. one\ngarbage line\n2. three", 3) == [
        "one", "", "three"
    ]


def test_json_takes_precedence_but_falls_back_when_it_is_empty():
    """A schema-shaped reply with nothing usable in it must not shadow a
    perfectly good line-format reply in the same response."""
    reply = '{"summaries": []}\n0. one\n1. two'
    assert parse_summaries(reply, 2) == ["one", "two"]


# --- rewrites: the good paths --------------------------------------------

def test_markers_preserve_internal_blank_lines():
    assert parse_rewrites(_markers((0, BODY)), 1) == [BODY]


def test_markers_for_a_multi_article_batch():
    out = parse_rewrites(_markers((0, "a"), (1, "b")), 2)
    assert out == ["a", "b"]


def test_rewrite_json_protocol_keeps_newlines():
    reply = json.dumps({"articles": [{"id": 0, "body": BODY}]})
    assert parse_rewrites(reply, 1) == [BODY]


def test_rewrite_json_keeps_a_fenced_code_block_intact():
    """render.py depends on this exactly: a fence that loses its newlines
    stops being a code block and the PDF is wrong."""
    body = "Run it:\n\n```bash\ncd /tmp\nls -la\n```\n\nDone."
    reply = json.dumps({"articles": [{"id": 0, "body": body}]})
    assert parse_rewrites(reply, 1) == [body]


def test_rewrite_json_passes_math_delimiters_through_untouched():
    body = "The area scales like $r^2$, and $$e^{i\\pi} + 1 = 0$$ still holds."
    reply = json.dumps({"articles": [{"id": 0, "body": body}]})
    assert parse_rewrites(reply, 1) == [body]


# --- rewrites: the bad paths ---------------------------------------------

@pytest.mark.parametrize("reply", [
    "",
    "Here is the rewritten article: hello",          # no markers at all
    "=== ARTICLE 0 START ===\nunterminated",         # missing END
    "=== ARTICLE 0 START ===\nbody\n=== ARTICLE 1 END ===",  # mismatched ids
    '{"articles": [{"id": 0}]}',
    '{"articles": "nope"}',
    "{ truncated",
])
def test_malformed_rewrite_replies_yield_blanks_not_exceptions(reply):
    assert parse_rewrites(reply, 1) == [""]


def test_one_missing_article_in_a_batch_leaves_only_that_one_pending():
    out = parse_rewrites(_markers((0, "a"), (2, "c")), 3)
    assert out == ["a", "", "c"]


def test_marker_ids_beyond_the_batch_are_ignored():
    assert parse_rewrites(_markers((9, "stray")), 1) == [""]
