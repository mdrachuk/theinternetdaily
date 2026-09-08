"""Batch-protocol parser regressions.

The summary stage sends N articles in one call and matches the replies back
up by position or id. When that mapping goes wrong the failure is silent and
ugly: article 3 gets article 5's summary. These fixtures pin the behaviour for
good replies, for both wire formats, and — the part that matters with a 12B
model — for the many ways a reply can be malformed.

The contract:
  * always returns exactly n items
  * never raises
  * an item the model got wrong comes back as "" (the caller counts it as an
    error and leaves the article pending, rather than storing nonsense)
"""
from __future__ import annotations

import json

import pytest

from tid.summarize import parse_summaries


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

