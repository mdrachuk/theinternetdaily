"""Title normalization for cross-source duplicate detection.

The old `[^a-z0-9]` normalizer threw away every non-ASCII character, so a
Cyrillic headline collapsed to "" or to whatever Latin fragment it contained.
One such row in the store, and every later all-Cyrillic headline from that
source matched it and was never ingested. These tests pin the multilingual
behaviour and the empty-title guard.
"""
from __future__ import annotations

from tid.store import norm_title


def test_latin_titles_normalize_as_before():
    assert norm_title("Hello, World!") == "hello world"
    assert norm_title("  Spaced -- out ... title ") == "spaced out title"


def test_cyrillic_title_keeps_its_letters():
    assert norm_title("Путин подписал закон") == "путин подписал закон"


def test_two_different_cyrillic_titles_differ():
    assert norm_title("Суд арестовал журналиста") != norm_title("Суд оправдал журналиста")


def test_cyrillic_title_with_a_latin_word_is_not_reduced_to_that_word():
    assert norm_title("HBO покажет сериал") != norm_title("HBO")
    assert norm_title("HBO покажет сериал") == "hbo покажет сериал"


def test_case_folding_is_unicode_aware():
    assert norm_title("ПРИВЕТ") == norm_title("привет")
    assert norm_title("STRASSE") == norm_title("Straße")


def test_compatibility_forms_fold_together():
    assert norm_title("１６５２") == norm_title("1652")       # full-width digits
    assert norm_title("ﬁne") == norm_title("fine")           # ligature


def test_cjk_and_arabic_survive():
    assert norm_title("東京で地震") == "東京で地震"
    assert norm_title("مرحبا بالعالم") == "مرحبا بالعالم"


def test_unicode_punctuation_is_a_separator():
    assert norm_title("«Медуза» — новости") == "медуза новости"
    assert norm_title("Don’t stop") == "don t stop"


def test_a_title_of_only_punctuation_is_empty():
    assert norm_title("— … —") == ""
    assert norm_title("") == ""
