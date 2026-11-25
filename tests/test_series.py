"""Recurring headlines: telling a desk's daily series from a story.

The rules are pure and tested on headlines taken from the store this was
calibrated on — the cases that must be caught, and the look-alikes that must
not be. Then the store read they run on, the topic stage that keeps series out
of the mains, and the layout that keeps them off the front.
"""
from __future__ import annotations

from datetime import date, timedelta

from tid import edition as ed
from tid import series as sr
from tid import sources
from tid.cli import cmd_topics, collect_current_edition
from tid.store import ArticleRow, SqliteStore, url_hash
from tid.testing import FakeBackend

TODAY = "2026-11-25"


def days_back(n: int) -> list[str]:
    """The `n` days before TODAY, newest first."""
    d = date.fromisoformat(TODAY)
    return [(d - timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def daily(template: str, n: int = 10) -> list[tuple[str, str]]:
    """A headline the source filed on each of the last `n` days; `{}` is the
    day's number."""
    return [(template.format(1000 + i), d) for i, d in enumerate(days_back(n))]


def filler(n: int = 10) -> list[tuple[str, str]]:
    """One unremarkable headline on each of the last `n` days, so a kicker's
    share of the days has something to be a share of."""
    return [(f"Ordinary story number {i} about something", d)
            for i, d in enumerate(days_back(n))]


# --- the kicker and the template -----------------------------------------

def test_the_kicker_is_what_comes_before_the_first_colon_or_full_stop():
    assert sr.kicker("Ukraine war latest: Russia strikes 3 bridges") == \
        "Ukraine war latest"
    assert sr.kicker("Война. 1736-й день. США больше не будут…") == "Война"
    assert sr.kicker("The Guide #270: Grand Theft Auto VI is everywhere") == \
        "The Guide #270"


def test_a_headline_without_a_short_opening_has_no_kicker():
    assert sr.kicker("Russia breaks yearly troop losses record") is None
    # Six words before the colon is a sentence, not the name of a series.
    assert sr.kicker("Why the people of this town voted: a report") is None


def test_masking_makes_todays_tally_and_yesterdays_compare_equal():
    assert sr.mask("General Staff: Russia has lost 1,641,489 troops") == \
        sr.mask("General Staff: Russia has lost 1,637,170 troops")
    assert sr.mask("Apple Releases iOS 26.2") == sr.mask("Apple Releases iOS 27.1")


# --- what is a series ---------------------------------------------------

def test_a_daily_tally_is_a_series_by_its_template():
    history = daily("General Staff: Russia has lost 1,63{},000 troops in Ukraine")
    assert sr.is_series(
        "General Staff: Russia has lost 1,641,489 troops in Ukraine",
        TODAY, history,
    )


def test_a_kicker_on_most_days_once_a_day_is_a_series():
    history = filler() + [
        (f"Ukraine war latest: story {i}", d)
        for i, d in enumerate(days_back(8))
    ]
    assert sr.is_series("Ukraine war latest: Crimean bridge collapses",
                        TODAY, history)


def test_a_numbered_kicker_is_a_series_from_its_second_issue():
    history = filler() + [("The Guide #269: the week in culture", days_back(7)[-1])]
    assert sr.is_series("The Guide #270: Grand Theft Auto VI is everywhere",
                        TODAY, history)


def test_a_label_on_a_third_of_the_days_is_not_a_series():
    """"Exclusive:" — the stories a reader most wants on the front."""
    history = filler(12) + [
        (f"Exclusive: scoop {i}", d) for i, d in enumerate(days_back(12)[::3])
    ]
    assert not sr.is_series("Exclusive: the minister knew", TODAY, history)


def test_a_label_on_many_stories_a_day_is_not_a_series():
    """"Show HN:" opens a headline most days, several times over."""
    history = filler() + [
        (f"Show HN: project {d} {i}", d)
        for d in days_back(10) for i in range(3)
    ]
    assert not sr.is_series("Show HN: a C# without a runtime", TODAY, history)


def test_a_label_the_source_type_names_is_never_a_series():
    """"Ask HN:" runs about once a day on a quiet week — indistinguishable
    from a briefing by its record, so the HN type says what it is."""
    history = filler() + [(f"Ask HN: question {i}", d)
                          for i, d in enumerate(days_back(8))]
    title = "Ask HN: What to do after two years unemployed?"
    assert sr.is_series(title, TODAY, history)
    assert not sr.is_series(title, TODAY, history,
                            labels=sources.post_labels("hn"))


def test_a_series_needs_a_history_first():
    assert not sr.is_series("Ukraine war latest: talks begin", TODAY, [])
    # Two earlier days is not yet a pattern, whatever share of them it is.
    history = [(f"Ukraine war latest: {d}", d) for d in days_back(2)]
    assert not sr.is_series("Ukraine war latest: talks begin", TODAY, history)


def test_only_the_window_before_the_day_counts():
    """A format the desk dropped a month ago does not mark today's story, and
    today's own headlines are not its history."""
    old = date.fromisoformat(TODAY) - timedelta(days=sr.WINDOW_DAYS + 5)
    stale = [(f"Ukraine war latest: {i}", (old - timedelta(days=i)).isoformat())
             for i in range(10)]
    same_day = [(f"Ukraine war latest: earlier {i}", TODAY) for i in range(5)]
    assert not sr.is_series("Ukraine war latest: talks", TODAY, stale + same_day)


# --- the store read ------------------------------------------------------

def _row(n: int, title: str, day: str, source: str = "Feed",
         kind: str = "rss", **kw) -> ArticleRow:
    url = f"http://example.invalid/{source}/{n}"
    return ArticleRow(
        id=url_hash(url), url=url, title=title, source=source,
        text="raw text", summary=f"Summary {n}.", published=day,
        kind=kind, **kw,
    )


async def test_titles_since_reads_one_sources_window(store):
    await store.upsert_rows([
        _row(1, "Old", "2026-10-01"),
        _row(2, "Recent", "2026-11-20"),
        _row(3, "Elsewhere", "2026-11-20", source="Other"),
        # Published or not, it is history.
        _row(4, "Already out", "2026-11-21", rendered_at="2026-11-21"),
    ])
    got = await store.titles_since("Feed", "2026-11-11")
    assert sorted(t for t, _ in got) == ["Already out", "Recent"]
    assert all(d >= "2026-11-11" for _, d in got)


async def test_series_ids_marks_only_the_series(store):
    rows = [_row(i, f"General Staff: Russia has lost {i},000 troops", d,
                 rendered_at=d)
            for i, d in enumerate(days_back(6))]
    tally = _row(100, "General Staff: Russia has lost 9,000 troops", TODAY)
    story = _row(101, "Crimean bridge collapses", TODAY)
    await store.upsert_rows([*rows, tally, story])
    assert await sr.series_ids(store, [tally, story]) == {tally.id}


# --- the topic stage -----------------------------------------------------

async def _store_with_a_tally(tmp_path) -> tuple[SqliteStore, ArticleRow]:
    """Six days of the tally, already published; today's tally, and stories
    beside it, waiting for the next paper."""
    store = SqliteStore(tmp_path / "state.db")
    past = [_row(i, f"General Staff: Russia has lost {i},000 troops", d,
                 rendered_at=d)
            for i, d in enumerate(days_back(6))]
    tally = _row(100, "General Staff: Russia has lost 9,000 troops", TODAY)
    # The fake files by the parity of a title's length: these share the
    # tally's, so all five land in one topic. The tally is the newest, and so
    # first in the order the stage hands the model.
    today = [_row(200 + i, f"Bridge story no {i}", "2026-11-24")
             for i in range(4)]
    assert all(len(r.title) % 2 == len(tally.title) % 2 for r in today)
    await store.upsert_rows([*past, tally, *today])
    return store, tally


async def test_a_series_is_never_chosen_as_a_main_story(tmp_path):
    store, tally = await _store_with_a_tally(tmp_path)
    backend = FakeBackend()
    try:
        await cmd_topics(store, backend)
        rows = {r.id: r for r in await store.pending_render()}
    finally:
        await store.close()

    assert len({r.topic for r in rows.values()}) == 1
    assert rows[tally.id].main_rank is None
    # The fake picks the first posting it is shown, and would have picked the
    # tally; the main slot went to a story instead.
    assert any(r.main_rank == 0 for r in rows.values())
    asked = [c["user"] for c in backend.calls if "Choose" in c["user"]]
    assert asked and all("General Staff" not in u for u in asked)


async def test_the_edition_marks_its_series(tmp_path):
    store, tally = await _store_with_a_tally(tmp_path)
    try:
        articles = await collect_current_edition(
            store, [{"name": "Feed", "section": "News"}]
        )
    finally:
        await store.close()
    marked = {a["id"]: a["series"] for a in articles}
    assert marked[tally.id] is True
    assert sum(marked.values()) == 1


# --- the layout ----------------------------------------------------------

def _article(n: int, section: str, *, series=False, main_rank=None,
             day=20) -> dict:
    return {
        "id": f"{n:016x}", "title": f"Headline {n}",
        "url": f"https://example.test/{n}", "source": "Feed",
        "section": section, "summary": "A lede.", "text": "word " * 300,
        "iso_date": f"2026-11-{day:02d}", "series": series,
        "topic": section, "topic_order": 0 if section == "War" else 1,
        "main_rank": main_rank,
    }


def _build(arts):
    return ed.build(arts, key="k" * 24, date=TODAY, built_at="")


def test_a_series_does_not_lead_even_as_the_newest_story():
    e = _build([
        _article(1, "War", series=True, day=25),
        _article(2, "War", day=20),
        _article(3, "Tech", day=21),
    ])
    assert e.lead.id == f"{2:016x}"


def test_a_series_given_a_main_rank_is_still_not_main():
    """A snapshot from before series were recognised may carry one."""
    e = _build([
        _article(1, "War", series=True, main_rank=0, day=25),
        _article(2, "War", day=20),
        _article(3, "Tech", day=21),
    ])
    assert e.lead.id == f"{2:016x}"


def test_series_sort_to_the_foot_of_their_column():
    arts = [_article(1, "War", day=20)]
    arts += [_article(10 + i, "Tech", series=(i == 0), day=25 - i)
             for i in range(4)]
    e = _build(arts)
    tech = next(s for s in e.sections if s.name == "Tech")
    assert tech.items[-1].id == f"{10:016x}"
    assert not tech.top.series


def test_a_section_of_nothing_but_series_gives_up_the_lead():
    e = _build([
        _article(1, "War", series=True, day=25),
        _article(2, "Tech", day=20),
    ])
    assert e.lead.id == f"{2:016x}"
    # It still runs.
    assert e.find(f"{1:016x}") is not None


def test_the_series_mark_survives_a_snapshot():
    before = _build([_article(1, "War", series=True), _article(2, "Tech")])
    after = ed.from_snapshot(ed.to_snapshot(before))
    assert after.find(f"{1:016x}").series
    assert not after.find(f"{2:016x}").series
