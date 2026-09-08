"""The topic stage: naming an edition's sections, filing into them, and
choosing what leads each one.

Three kinds of test, in that order: the parsers (which are what stands between
a model's reply and the front page), the stage over a fake backend, and the
layout the result produces.
"""
from __future__ import annotations

import json

import pytest

from tid import edition as ed
from tid import topics as tp
from tid.cli import cmd_topics, collect_current_edition
from tid.store import ArticleRow, SqliteStore, url_hash
from tid.testing import FAKE_TOPICS, FakeBackend

TOPICS = [tp.Topic("Chip Wars", "semiconductors, fabs, export controls"),
          tp.Topic("Ukraine", "the war and its diplomacy"),
          tp.Topic("Elsewhere", "everything else")]


# --- parsing the topic set ------------------------------------------------

def test_topics_parse_from_json():
    reply = json.dumps({"topics": [
        {"name": "Chip Wars", "blurb": "fabs"},
        {"name": "Ukraine", "blurb": "the war"},
    ]})
    assert [t.name for t in tp.parse_topics(reply)] == ["Chip Wars", "Ukraine"]
    assert tp.parse_topics(reply)[0].blurb == "fabs"


def test_topics_parse_from_numbered_lines():
    """The protocol an unguided backend answers in."""
    reply = "1. Chip Wars — semiconductors and fabs\n2. Ukraine — the war\n"
    got = tp.parse_topics(reply)
    assert [t.name for t in got] == ["Chip Wars", "Ukraine"]
    assert got[0].blurb == "semiconductors and fabs"


def test_a_hyphenated_name_is_not_split_at_its_hyphen():
    """The blurb separator is a dash between words. "Non-Fiction" is a name."""
    got = tp.parse_topics("1. Non-Fiction — books and essays\n")
    assert got[0] == tp.Topic("Non-Fiction", "books and essays")


def test_topics_survive_a_code_fence_and_preamble():
    reply = 'Here you go:\n```json\n{"topics": [{"name": "Ukraine"}]}\n```'
    assert [t.name for t in tp.parse_topics(reply)] == ["Ukraine"]


def test_a_refusal_names_no_topics():
    """The whole stage turns off rather than filing under a sentence."""
    assert tp.parse_topics("I'm sorry, I can't help with that.") == []


def test_a_sentence_is_not_a_topic_name():
    """A section head is 1-3 words. Anything longer is the model explaining
    itself, and it would run across the top of a column."""
    reply = ("1. Ukraine — the war\n"
             "2. This is a very long line that is clearly not a section head\n")
    assert [t.name for t in tp.parse_topics(reply)] == ["Ukraine"]


def test_repeated_topics_collapse_keeping_the_first():
    reply = json.dumps({"topics": [
        {"name": "Ukraine"}, {"name": "ukraine"}, {"name": "Chip Wars"},
    ]})
    assert [t.name for t in tp.parse_topics(reply)] == ["Ukraine", "Chip Wars"]


def test_the_topic_set_is_capped():
    reply = json.dumps({"topics": [{"name": f"Topic {i}"} for i in range(20)]})
    assert len(tp.parse_topics(reply)) == tp.MAX_TOPICS


# --- standing sections ----------------------------------------------------

STANDING = [tp.Topic("World", "international news"),
            tp.Topic("Science & Maths", "research and mathematics"),
            tp.Topic("Apple", "")]


def test_standing_topics_come_from_the_config_tables(tmp_path):
    cfg = tmp_path / "sources.toml"
    cfg.write_text(
        '[[topic]]\nname = "World"\nblurb = "international news"\n'
        '[[topic]]\nname = "  Science   &  Maths "\n'
        '[[source]]\nname = "Feed"\nsection = "S"\n'
    )
    from tid import config
    got = tp.standing_topics(config.load_topics(cfg))
    assert got == [tp.Topic("World", "international news"),
                   tp.Topic("Science & Maths", "")]
    assert config.load_sources(cfg) == [{"name": "Feed", "section": "S"}]


def test_a_config_without_topics_has_no_standing_sections(tmp_path):
    cfg = tmp_path / "sources.toml"
    cfg.write_text('[[source]]\nname = "Feed"\n')
    from tid import config
    assert tp.standing_topics(config.load_topics(cfg)) == []


def test_a_standing_topic_needs_a_name():
    with pytest.raises(ValueError):
        tp.standing_topics([{"blurb": "no name"}])


def test_repeated_standing_topics_collapse():
    got = tp.standing_topics([{"name": "World"}, {"name": "world"}])
    assert got == [tp.Topic("World")]


def test_the_merge_keeps_the_models_order_and_restores_the_config_names():
    """The model ranks; the config spells. A standing section it named is the
    configured one — name and blurb — wherever it put it."""
    proposed = [tp.Topic("Chip Wars", "fabs"),
                tp.Topic("world", "the model's own blurb"),
                tp.Topic("apple", "the model's blurb, kept: the config has none")]
    got = tp.merge_topics(proposed, STANDING)
    assert got == [
        tp.Topic("Chip Wars", "fabs"),
        tp.Topic("World", "international news"),
        tp.Topic("Apple", "the model's blurb, kept: the config has none"),
        tp.Topic("Science & Maths", "research and mathematics"),
    ], "and the one the model forgot is appended, not lost"


def test_the_merge_caps_the_days_own_topics_but_never_a_standing_one():
    standing = [tp.Topic(f"Standing {i}") for i in range(tp.MAX_TOPICS)]
    proposed = [tp.Topic(f"Own {i}") for i in range(6)] + standing[:2]
    got = tp.merge_topics(proposed, standing)
    own = [t for t in got if t.name.startswith("Own")]
    assert len(own) == tp.MIN_OWN_TOPICS, (
        "a full page of standing sections still leaves room for a couple of "
        "the day's own"
    )
    assert [t for t in got if t.name.startswith("Standing")] == standing[:2] + standing[2:]


def test_how_many_topics_of_its_own_an_edition_may_add():
    assert tp.own_topic_cap(0) == tp.MAX_TOPICS
    assert tp.own_topic_cap(3) == tp.MAX_TOPICS - 3
    assert tp.own_topic_cap(tp.MAX_TOPICS + 4) == tp.MIN_OWN_TOPICS


@pytest.mark.parametrize("supports_json", [False, True])
async def test_the_naming_call_is_shown_the_standing_sections(supports_json):
    backend = FakeBackend(supports_json=supports_json)
    got = await tp.propose_topics(
        backend, [("A headline", "a summary")], STANDING
    )
    (call,) = backend.calls
    assert "<standing>" in call["user"]
    assert "<topic>World — international news</topic>" in call["user"]
    assert "STANDING sections" in call["system"]
    assert f"at most {tp.own_topic_cap(3)} topics of the day's own" in call["system"]
    # The fake names its two topics; the config's three are still all there.
    assert [t.name for t in got] == [*FAKE_TOPICS, "World", "Science & Maths", "Apple"]


async def test_without_standing_sections_the_prompt_is_unchanged():
    backend = FakeBackend()
    await tp.propose_topics(backend, [("A headline", "a summary")])
    (call,) = backend.calls
    assert "<standing>" not in call["user"]
    assert "STANDING" not in call["system"]
    assert f"between {tp.MIN_TOPICS} and {tp.MAX_TOPICS} topics" in call["system"]


async def test_a_failed_naming_call_is_not_rescued_by_the_config():
    """No usable topics still turns the stage off. Filing the day into standing
    sections by a model that could not read it is not what the editor asked."""
    refuser = FakeBackend(respond=lambda system, user: "I cannot help.")
    assert await tp.propose_topics(refuser, [("A", "b")], STANDING) == []


# --- parsing one article's filing ----------------------------------------

@pytest.mark.parametrize("reply", [
    '{"topic": "Ukraine"}',
    "Ukraine",
    "  ukraine  ",
    '"Ukraine"',
    "- Ukraine",
    "This story belongs under Ukraine.",
])
def test_a_topic_is_recognised_however_the_model_wraps_it(reply):
    assert tp.parse_label(reply, TOPICS) == "Ukraine"


def test_an_invented_topic_is_dropped():
    """An off-list answer must leave the article unfiled, not mislabelled: an
    unfiled article falls back to its sources.toml section, which is right,
    where a guessed topic would be wrong."""
    assert tp.parse_label("Sports", TOPICS) == ""


def test_the_longest_matching_name_wins_inside_a_sentence():
    topics = [tp.Topic("Apple"), tp.Topic("Apple Silicon")]
    assert tp.parse_label("Filed under Apple Silicon.", topics) == "Apple Silicon"


def test_the_label_schema_admits_only_the_named_topics():
    """Where the backend can enforce a schema, an invented topic stops being
    possible rather than being caught afterwards."""
    schema = tp.label_schema(TOPICS)
    assert schema["properties"]["topic"]["enum"] == [t.name for t in TOPICS]


# --- parsing the main stories --------------------------------------------

def test_main_ids_parse_from_json_and_from_bare_integers():
    assert tp.parse_main('{"main": [3, 0]}', n=5, want=2) == [3, 0]
    assert tp.parse_main("3, 0", n=5, want=2) == [3, 0]


def test_main_ids_out_of_range_or_repeated_are_dropped():
    """A made-up id must not silently promote an unrelated article."""
    assert tp.parse_main('{"main": [9, 1, 1, 0]}', n=3, want=3) == [1, 0]


def test_main_selection_stops_at_what_was_asked_for():
    assert tp.parse_main("0, 1, 2, 3, 4", n=5, want=2) == [0, 1]


def test_how_many_mains_a_topic_gets():
    """A column is a top story and a few headlines: a fourth main has nowhere
    to be main in."""
    assert tp.mains_for(1) == 1
    assert tp.mains_for(3) == 1
    assert tp.mains_for(8) == 2
    assert tp.mains_for(40) == 3


# --- the stage, end to end ------------------------------------------------

async def _seeded_store(tmp_path, n=8):
    """A store holding `n` articles that are ready and unpublished — which is
    exactly the set the next edition will carry."""
    store = SqliteStore(tmp_path / "state.db")
    rows = []
    for i in range(n):
        url = f"http://example.invalid/{i}"
        rows.append(ArticleRow(
            id=url_hash(url), url=url,
            # Titles of differing length, because that is what the fake files on.
            title="Story " + "x" * i,
            source="Feed", text="raw text", summary=f"Summary {i}.",
            published=f"2026-08-{10 + i:02d}",
        ))
    await store.upsert_rows(rows)
    return store


SOURCES = [{"name": "Feed", "section": "Configured Section"}]


@pytest.mark.parametrize("supports_json", [False, True])
async def test_the_stage_files_every_article_under_a_named_topic(
    tmp_path, supports_json
):
    store = await _seeded_store(tmp_path)
    backend = FakeBackend(supports_json=supports_json)
    try:
        assert await cmd_topics(store, backend) == 0
        rows = await store.pending_render()
    finally:
        await store.close()

    assert all(r.topic in FAKE_TOPICS for r in rows)
    # And the topic set is ordered: the first topic named is order 0.
    orders = {r.topic: r.topic_order for r in rows}
    assert orders[FAKE_TOPICS[0]] == 0
    assert orders[FAKE_TOPICS[1]] == 1


async def test_each_topic_gets_main_stories_ranked_from_zero(tmp_path):
    store = await _seeded_store(tmp_path)
    backend = FakeBackend()
    try:
        await cmd_topics(store, backend)
        rows = await store.pending_render()
    finally:
        await store.close()

    for topic in FAKE_TOPICS:
        group = [r for r in rows if r.topic == topic]
        ranks = sorted(r.main_rank for r in group if r.main_rank is not None)
        assert ranks == list(range(tp.mains_for(len(group))))


async def test_every_standing_section_can_be_filed_into(tmp_path):
    """The point of configuring them: whatever the model names, the filing
    pass is offered every standing section, and the rows land under the
    configured names."""
    store = await _seeded_store(tmp_path, n=12)
    backend = FakeBackend()
    try:
        assert await cmd_topics(store, backend, standing=STANDING) == 0
        rows = await store.pending_render()
    finally:
        await store.close()
    label_calls = [c for c in backend.calls if "<headline>" in c["user"]]
    assert label_calls
    for c in label_calls:
        for t in STANDING:
            assert f"<topic>{t.line()}</topic>" in c["user"]
    names = {r.topic for r in rows}
    assert names <= set(FAKE_TOPICS) | {t.name for t in STANDING}
    # The fake spreads filings across the whole list, so a standing section
    # actually receives articles — proof the enum was the merged set.
    assert names & {t.name for t in STANDING}
    orders = {r.topic: r.topic_order for r in rows}
    assert orders[FAKE_TOPICS[0]] == 0, "the model's ranking is still the order"


async def test_the_cli_reads_standing_sections_from_the_config(tmp_path):
    """`tid topics --config` picks the standing sections up from the same file
    as the sources."""
    from tid.cli import _load_topics
    cfg = tmp_path / "sources.toml"
    cfg.write_text(
        '[[topic]]\nname = "World"\nblurb = "abroad"\n'
        '[[source]]\nname = "Feed"\nsection = "S"\n'
    )
    assert _load_topics(cfg) == [tp.Topic("World", "abroad")]


async def test_one_call_names_the_topics_then_one_per_article(tmp_path):
    """The shape the pipeline promises: a single edition-wide call, then a
    call per article, then a call per topic that got anything."""
    store = await _seeded_store(tmp_path, n=6)
    backend = FakeBackend()
    try:
        await cmd_topics(store, backend)
    finally:
        await store.close()
    assert len(backend.calls) == 1 + 6 + len(FAKE_TOPICS)


async def test_a_model_that_names_no_topics_leaves_every_article_unfiled(
    tmp_path
):
    """The edition still comes out — laid out by sources.toml, as before."""
    store = await _seeded_store(tmp_path)
    refuser = FakeBackend(respond=lambda system, user: "I cannot help.")
    try:
        assert await cmd_topics(store, refuser) == 0
        rows = await store.pending_render()
    finally:
        await store.close()
    assert all(r.topic is None for r in rows)
    # One call, not one per article: naming nothing stops the stage there.
    assert len(refuser.calls) == 1


async def test_an_unfilable_article_is_unfiled_not_guessed(tmp_path):
    """A reply naming no listed topic clears the article rather than parking it
    in whichever topic came first."""
    def _respond(system: str, user: str) -> str:
        if "<candidate" in user:
            return '{"topics": [{"name": "Ukraine", "blurb": "the war"}]}'
        if "<headline>" in user:
            return "Sports"       # not on the list
        return "0"
    store = await _seeded_store(tmp_path, n=3)
    try:
        await cmd_topics(store, FakeBackend(respond=_respond))
        rows = await store.pending_render()
    finally:
        await store.close()
    assert all(r.topic is None and r.main_rank is None for r in rows)


async def test_a_stale_topic_is_cleared_when_the_next_edition_is_named(
    tmp_path
):
    """Topics describe one edition. An article that missed its paper is re-filed
    with the next one, and must not keep a name that edition never chose."""
    def _no_topics(system: str, user: str) -> str:
        return "I cannot help."

    store = await _seeded_store(tmp_path, n=3)
    try:
        await cmd_topics(store, FakeBackend())
        assert all(r.topic for r in await store.pending_render())
        await cmd_topics(store, FakeBackend(respond=_no_topics))
        # The naming call failed, so nothing was re-filed — the old names stand
        # rather than being wiped for a paper that has no sections at all.
        assert all(r.topic for r in await store.pending_render())

        # But a run that *does* name topics re-files every row.
        def _one_topic(system: str, user: str) -> str:
            if "<candidate" in user:
                return '{"topics": [{"name": "Ukraine", "blurb": "war"}]}'
            if "<headline>" in user:
                return "Ukraine"
            return "0"
        await cmd_topics(store, FakeBackend(respond=_one_topic))
        rows = await store.pending_render()
    finally:
        await store.close()
    assert {r.topic for r in rows} == {"Ukraine"}


# --- what it does to the page --------------------------------------------

async def test_the_columns_are_the_topics_not_the_sources(tmp_path):
    """The point of the whole stage: a paper whose every source is configured
    into one section still comes out with a column per topic."""
    store = await _seeded_store(tmp_path)
    backend = FakeBackend()
    try:
        await cmd_topics(store, backend)
        articles = await collect_current_edition(store, SOURCES)
    finally:
        await store.close()

    paper = ed.build(articles, key="k" * 24, date="2026-08-30", built_at="")
    columns = {sec.name for sec in paper.sections} | {sec.name for sec in paper.below}
    assert columns == set(FAKE_TOPICS)
    assert "Configured Section" not in columns
    # Nothing is lost or duplicated by grouping on a new key.
    assert paper.total == len(articles)
    assert len({i.id for i in paper.items}) == len(articles)


def _item(n, topic="", order=None, rank=None, day=10, section="Configured"):
    return {
        "id": f"{n:016x}", "title": f"Headline {n}",
        "url": f"https://example.test/{n}", "source": "Feed",
        "section": section, "summary": "A lede.", "text": "word " * 400,
        "iso_date": f"2026-08-{day:02d}", "topic": topic,
        "topic_order": order, "main_rank": rank,
    }


def _build(articles):
    return ed.build(articles, key="k" * 24, date="2026-08-30", built_at="")


def test_a_main_story_tops_its_column_over_a_newer_one():
    """The mains are the stage's answer to what a section is about, so they
    outrank recency — which is what still orders everything under them.

    Three topics because the front page pulls the lead and the two stories
    beside it off the top of three different columns; with fewer there is
    nothing left in a column to assert about.
    """
    paper = _build([
        _item(0, topic="Ukraine", order=0, day=10, rank=0),
        _item(1, topic="Ukraine", order=0, day=11, rank=1),
        _item(2, topic="Ukraine", order=0, day=28),
        _item(3, topic="Ukraine", order=0, day=20),
        _item(4, topic="Chip Wars", order=1, day=10, rank=0),
        _item(5, topic="Chip Wars", order=1, day=27),
        _item(6, topic="Light Relief", order=2, day=10, rank=0),
        _item(7, topic="Light Relief", order=2, day=26),
    ])
    assert paper.lead.id == _item(0)["id"], "the first topic's first main leads"
    ukraine = paper.sections[0]
    assert ukraine.name == "Ukraine"
    assert ukraine.top.id == _item(1)["id"], "the second main tops the column"
    assert [i.id for i in ukraine.rest] == [_item(2)["id"], _item(3)["id"]], (
        "and everything under the mains is still newest first"
    )


def test_the_columns_run_in_the_order_the_stage_ranked_them():
    paper = _build([
        _item(n, topic=topic, order=order, rank=0 if n % 4 == 0 else None,
              day=10 + n)
        for order, topic in enumerate(["Ukraine", "Chip Wars", "Light Relief"])
        for n in range(order * 4, order * 4 + 4)
    ])
    assert [s.name for s in paper.sections] == [
        "Ukraine", "Chip Wars", "Light Relief",
    ]
    assert paper.lead.id == _item(0)["id"], "the first topic carries the lead"


def test_an_unfiled_article_keeps_its_configured_section_behind_the_topics():
    paper = _build([
        *(_item(n, section="Apple", day=10 + n) for n in range(3)),
        *(_item(n, topic="Ukraine", order=0, rank=0 if n == 3 else None,
                day=10 + n) for n in range(3, 6)),
    ])
    assert [s.name for s in paper.sections] == ["Ukraine", "Apple"]
    assert paper.lead.id == _item(3)["id"], "a topic outranks a configured section"


def test_a_paper_with_no_topics_at_all_is_laid_out_exactly_as_before():
    """The fallback has to be the old behaviour, not an approximation of it."""
    arts = [_item(i, section=f"S{i % 3}", day=10 + i) for i in range(9)]
    assert ed.to_snapshot(_build(arts)) == ed.to_snapshot(_build(
        [{k: v for k, v in a.items()
          if k not in ("topic", "topic_order", "main_rank")} for a in arts]
    ))


def test_the_topic_survives_a_snapshot_and_a_refile():
    """An archived edition still reads as the paper it was: re-filing a source
    in sources.toml must not move an article the stage placed by topic."""
    paper = _build([_item(0, topic="Ukraine", order=0, rank=0)])
    reloaded = ed.from_snapshot(ed.to_snapshot(paper))
    assert reloaded.lead.group == "Ukraine"
    assert reloaded.lead.main_rank == 0

    refiled = ed.refiled(reloaded, [{"name": "Feed", "section": "Somewhere Else"}])
    assert refiled.lead.group == "Ukraine"
    assert refiled.lead.section == "Somewhere Else", (
        "the fallback still tracks the config, in case the topic ever goes away"
    )


# --- reaching the right store from a one-off run --------------------------

def test_the_cli_defaults_to_the_configured_store_and_config(monkeypatch):
    """`tid topics` inside the container has to reach the store the web
    process is serving from. Both are named by the environment there, and a
    literal default would quietly create an empty state.db in /app instead."""
    from tid.cli import build_parser, store_url

    monkeypatch.setenv("TID_STATE", "/data/state.db")
    monkeypatch.setenv("TID_CONFIG", "/app/sources.toml")
    args = build_parser().parse_args(["topics"])
    assert str(args.config) == "/app/sources.toml"
    assert store_url(args) == "/data/state.db"


def test_an_explicit_state_still_wins_over_the_environment(monkeypatch):
    from tid.cli import build_parser, store_url

    monkeypatch.setenv("TID_STATE", "/data/state.db")
    args = build_parser().parse_args(["--state", "other.db", "topics"])
    assert store_url(args) == "other.db"
