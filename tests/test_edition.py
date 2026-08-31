"""The front-page layout, and the HTML it turns into.

`tid.edition` is pure, so these need no store, no clock and no network — the
whole point of keeping the layout out of the route handlers.
"""
from __future__ import annotations

import pytest

from tid import edition as ed
from tid import icons, site


def article(n: int, section="Tech", source=None, medium="read",
            summary="A two-sentence lede.", words=400, image=None,
            day=12) -> dict:
    return {
        "id": f"{n:016x}",
        "title": f"Headline {n}",
        "url": f"https://example.test/{n}",
        "source": source or f"Source {n % 3}",
        "section": section,
        "medium": medium,
        "summary": summary,
        "text": "word " * words,
        "image": image,
        "iso_date": f"2026-08-{day:02d}",
        "date": "Aug 12, 2026",
    }


def build(articles, key="k" * 24):
    return ed.build(articles, key=key, date="2026-08-30",
                    built_at="2026-08-30T06:00:00+00:00")


# --- what goes where ------------------------------------------------------

def test_every_article_appears_exactly_once():
    """The one invariant the layout must never break: skimming the top of each
    section for the front page must not duplicate or drop anything."""
    arts = [article(i, section=f"S{i % 5}") for i in range(60)]
    e = build(arts)
    ids = [i.id for i in e.items]
    assert len(ids) == len(set(ids)) == 60


def test_lead_is_the_first_section_top_with_a_dek():
    arts = [
        article(0, section="First", summary=""),
        article(1, section="First"),
        article(2, section="Second"),
    ]
    e = build(arts)
    assert e.lead.id == article(1)["id"], "a dekless story cannot carry the lead"


def test_lead_falls_back_when_nothing_has_a_dek():
    """A store where summarizing has not run yet still gets a front page."""
    e = build([article(i, summary="") for i in range(4)])
    assert e.lead is not None
    assert e.lead.dek == ""


def test_side_stories_come_from_other_sections():
    arts = ([article(i, section="Tech") for i in range(6)]
            + [article(10, section="World"), article(11, section="Ideas")])
    e = build(arts)
    assert {i.section for i in e.side} == {"World", "Ideas"}


def test_side_falls_back_to_the_lead_section_when_alone():
    e = build([article(i, section="Only") for i in range(5)])
    assert len(e.side) == 2
    assert all(i.section == "Only" for i in e.side)


def test_front_page_takes_four_sections_and_the_rest_go_below():
    arts = [article(i, section=f"S{i % 6}") for i in range(60)]
    e = build(arts)
    assert len(e.sections) == ed.FRONT_SECTIONS
    assert {s.name for s in e.below} >= {s.name for s in e.sections}
    assert all(len(s.rest) <= ed.FRONT_REST for s in e.sections)


def test_a_section_within_a_column_is_ordered_newest_first():
    arts = [article(0, day=10), article(1, day=14), article(2, day=12)]
    e = build(arts)
    assert e.lead.id == article(1)["id"]


def test_at_most_one_extra_photograph_on_the_front_page():
    arts = [article(i, section=f"S{i % 5}", image=f"https://img.test/{i}.jpg")
            for i in range(40)]
    e = build(arts)
    assert sum(1 for s in e.sections if s.image) <= 1


def test_an_empty_store_lays_out_without_raising():
    e = build([])
    assert e.lead is None and e.total == 0 and e.sections == []


def test_untitled_articles_are_dropped():
    e = build([article(0), dict(article(1), title="  ")])
    assert e.total == 1


# --- reading time ---------------------------------------------------------

@pytest.mark.parametrize("words,expected", [
    (0, ""), (30, ""), (230, "1 min"), (2760, "12 min"),
])
def test_reading_time(words, expected):
    assert ed.reading_time("word " * words) == expected


def test_meta_falls_back_to_the_date_when_there_is_no_body():
    e = build([article(0, words=0)])
    assert e.lead.meta == "Aug 12, 2026"


# --- filtering ------------------------------------------------------------

def test_filtering_relays_out_rather_than_hiding():
    """A column whose top story is a video must not lose its heading when the
    reader asks for text only — the whole edition is laid out again."""
    arts = ([article(i, section="Tech", medium="watch") for i in range(3)]
            + [article(i + 10, section="Tech") for i in range(4)])
    e = ed.filtered(build(arts), "read")
    assert e.total == 4
    assert all(i.medium == "read" for i in e.items)
    assert e.lead is not None


def test_filtering_by_an_unknown_medium_is_a_no_op():
    e = build([article(i) for i in range(5)])
    assert ed.filtered(e, "smell").total == 5
    assert ed.filtered(e, None).total == 5


def test_media_present_only_reports_what_is_there():
    arts = [article(0), article(1, medium="listen")]
    assert build(arts).media_present() == ["read", "listen"]


# --- snapshots ------------------------------------------------------------

def test_snapshot_round_trip_preserves_the_paper():
    arts = [article(i, section=f"S{i % 4}", image="https://i.test/a.jpg")
            for i in range(30)]
    before = build(arts)
    after = ed.from_snapshot(ed.to_snapshot(before))
    assert [i.id for i in after.items] == [i.id for i in before.items]
    assert after.lead.dek == before.lead.dek
    assert after.lead.body == before.lead.body
    assert after.key == before.key and after.date == before.date


# --- rendering ------------------------------------------------------------

def test_rendered_edition_carries_the_headlines_and_the_deks():
    e = build([article(i, section=f"S{i % 3}") for i in range(20)])
    html = site.render_edition(e)
    assert "<title>The Internet Daily" in html
    for item in e.items:
        assert item.title in html
    assert e.lead.dek in html
    assert f"/e/{e.key}/a/{e.lead.id}" in html, "preview must link to the text"


def test_a_bands_photograph_is_printed_with_the_story_it_belongs_to():
    """A band's picture is its top story's. Printed at the end of the stacked
    headlines it flowed to the foot of the last column and read as belonging to
    whatever story ended up above it."""
    arts = [article(i, section=f"S{i % 6}",
                    image=f"https://img.test/{i}.jpg" if i % 6 == 5 else None)
            for i in range(30)]
    e = build(arts)
    banded = [s for s in e.below if s.image]
    assert banded, "nothing below the fold to check"
    assert all(s.image == s.top.image for s in banded)

    bands = [b.partition("</section>")[0]
             for b in site.render_edition(e).split('<section class="band">')[1:]]
    shown = [b for b in bands if 'class="photo"' in b]
    assert len(shown) == len(banded)
    for band, html in zip(banded, shown):
        head, _, stacked = html.partition('class="stacks"')
        assert band.image in head, "the photo belongs beside its own headline"
        assert 'class="photo"' not in stacked


def test_headline_text_is_escaped_not_injected():
    nasty = dict(article(0), title="<script>alert(1)</script>",
                 summary="a & b <b>")
    html = site.render_edition(build([nasty]))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "a &amp; b" in html


def test_medium_glyphs_survive_escaping():
    """The glyphs are inline SVG built by us, so they must reach the page as
    markup — while everything around them is still escaped."""
    e = build([article(0, medium="listen")])
    html = site.render_edition(e)
    assert "<svg viewBox" in html
    assert icons.glyph("read") == "", "a written article gets no glyph"
    assert icons.filter_glyph("read") != "", "the filter chip needs one"


def test_filter_chips_appear_only_when_there_is_a_choice():
    text_only = site.render_edition(build([article(i) for i in range(4)]))
    assert text_only.count('class="chip') == 1

    mixed = site.render_edition(build(
        [article(0), article(1, medium="watch"), article(2, medium="listen")]
    ))
    assert mixed.count('class="chip') == 4


def test_empty_edition_says_so_instead_of_rendering_a_blank_paper():
    html = site.render_edition(build([]))
    assert "Nothing filed yet" in html


def test_article_page_shows_the_body_as_paragraphs():
    item = build([article(0)]).lead
    item.body = "First para.\n\nSecond para.\n\n```\ncode & <b>\n```"
    html = site.render_article(item, "k" * 24, "/e/" + "k" * 24)
    assert "<p>First para.</p>" in html
    assert "<pre><code>code &amp; &lt;b&gt;" in html
    assert "MathJax" not in html, "no maths in this one, so no script"


def test_article_page_loads_mathjax_only_when_there_is_maths():
    item = build([article(0)]).lead
    item.body = "The bound is $O(n \\log n)$ in the worst case."
    html = site.render_article(item, "k", "/")
    assert "MathJax" in html
    assert "$O(n \\log n)$" in html, "the source stays legible if it never loads"


def test_source_marks_point_at_this_origin_not_at_google():
    html = site.render_edition(build([article(0)]))
    assert "/icon/example.test.png" in html
    assert "google.com" not in html
