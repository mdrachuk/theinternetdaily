"""The edition view-model: articles in, a laid-out newspaper out.

`cli.collect_current_edition` hands over a flat list of articles in
sources.toml order. That is the right shape for a linear PDF and the wrong
shape for a front page, which needs to know which story is the lead, which
three go beside it, and which columns carry the rest.

Everything here is pure: no store, no network, no clock beyond what is passed
in. The layout of an edition is therefore reproducible from its snapshot,
which is what lets an archived edition still render months later.

Vocabulary, borrowed from the page it produces:

  lead      the one story across the top-left, with a dek and an image
  side      two stories stacked beside the lead, headline only
  sections  the front-page columns, one per topic in the edition
  below     the continuation of every section, further down the same page

An article appears exactly once. The front page skims the top off each
section; `below` carries whatever the skim left behind.

What a column *is* comes from the topic stage (`tid.topics`), which reads the
edition and names its own sections: `topic` is the column, `topic_order` is
where that column sits, and `main_rank` says which of its stories carry it. An
article the stage never filed — because it did not run, or could not place the
piece — falls back to its `sources.toml` section, and a paper where that is
true of every article is laid out exactly as it was before topics existed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# How many section columns the front page carries. Past this the whole section
# drops below the fold — four narrow columns is already the limit of what reads
# as a newspaper rather than a directory.
FRONT_SECTIONS = 4

# Headlines under each front-page column's top story.
FRONT_REST = 3

# Words per minute for the "12 min" byline. On the slow side of the usual
# 200–250 range: these are rewritten full texts, not listicles.
WPM = 230

MEDIA = ("read", "watch", "listen")


@dataclass
class Item:
    """One article, as the page needs it."""
    id: str
    title: str
    url: str
    source: str
    section: str
    medium: str = "read"
    dek: str = ""
    meta: str = ""
    date: str = ""
    iso_date: str = ""
    image: str | None = None
    body: str = ""
    topic: str = ""                 # the edition's own filing; "" = unfiled
    topic_order: int | None = None  # where that topic sits in the edition
    main_rank: int | None = None    # 0 = the topic's lead story; None = not main

    @property
    def group(self) -> str:
        """The column this article appears under, and its heading.

        The topic when the stage filed it, the `sources.toml` section when it
        did not. One property rather than an overwritten `section` so a
        snapshot keeps both: re-filing a source in `sources.toml` still moves
        the articles topics never reached.
        """
        return self.topic or self.section or self.source

    @property
    def is_main(self) -> bool:
        return self.main_rank is not None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Section:
    """A front-page column, or its continuation below the fold."""
    name: str
    top: Item
    rest: list[Item] = field(default_factory=list)
    image: str | None = None

    @property
    def items(self) -> list[Item]:
        return [self.top, *self.rest]


@dataclass
class Edition:
    key: str
    date: str                     # ISO date the edition was assembled for
    built_at: str                 # ISO-8601 UTC timestamp
    lead: Item | None = None
    side: list[Item] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    below: list[Section] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)

    @property
    def items(self) -> list[Item]:
        """Every article in the edition, once each, in reading order."""
        out: list[Item] = []
        if self.lead:
            out.append(self.lead)
        out += self.side
        for sec in self.sections:
            out += sec.items
        for sec in self.below:
            out += sec.items
        return out

    @property
    def total(self) -> int:
        return len(self.items)

    def find(self, article_id: str) -> Item | None:
        return next((i for i in self.items if i.id == article_id), None)

    def media_present(self) -> list[str]:
        """Which of read/watch/listen this edition actually contains.

        The filter row only offers what is there: a paper with nothing to watch
        should not show a Watch chip that empties the page.
        """
        have = {i.medium for i in self.items}
        return [m for m in MEDIA if m in have]


# --- deriving one article's display fields --------------------------------

def reading_time(text: str) -> str:
    """"12 min", from a word count. Empty when there is no body to time."""
    words = len(text.split())
    if words < 60:
        return ""
    return f"{max(1, round(words / WPM))} min"


def _to_int(value: Any) -> int | None:
    """An optional rank from a store row or a snapshot. Junk reads as absent —
    a bad `main_rank` must demote an article, never crash the page."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_item(a: dict) -> Item:
    body = a.get("text") or ""
    return Item(
        id=a.get("id") or "",
        title=a.get("title") or "",
        url=a.get("url") or "",
        source=a.get("source") or "",
        section=a.get("section") or a.get("source") or "Elsewhere",
        medium=a.get("medium") or "read",
        # The summary is the dek. When summarizing failed the headline has to
        # carry the story on its own, which is what the design does for every
        # item below the top of a column anyway.
        dek=(a.get("summary") or "").strip(),
        meta=reading_time(body) or (a.get("date") or ""),
        date=a.get("date") or "",
        iso_date=a.get("iso_date") or "",
        image=a.get("image") or None,
        body=body,
        topic=(a.get("topic") or "").strip(),
        topic_order=_to_int(a.get("topic_order")),
        main_rank=_to_int(a.get("main_rank")),
    )


def _sorted_group(rows: list[Item]) -> list[Item]:
    """One column's articles: the main stories first, then the rest by date.

    The mains are the topic stage's answer to "what is this section about
    today", and the layout's job is to put them where a reader looks first —
    the top of the column, and through `_take_lead`/`_take_side` the front
    page. Everything else keeps the newest-first order it always had, so an
    unlabelled column is ordered exactly as it was before topics existed.
    """
    mains = sorted((r for r in rows if r.is_main), key=lambda i: i.main_rank)
    rest = sorted(
        (r for r in rows if not r.is_main),
        key=lambda i: i.iso_date, reverse=True,
    )
    return mains + rest


def _group_order(rows: list[Item], appearance: int) -> tuple[int, int, int]:
    """Sort key for a whole column.

    Topics come first, in the order the topic stage put them: it ranked them by
    significance, and that ranking is what decides which column carries the
    lead. Unlabelled columns keep sources.toml order behind them, which is
    where a paper with no topics at all stays.
    """
    orders = [r.topic_order for r in rows if r.topic_order is not None]
    if orders:
        return (0, min(orders), appearance)
    return (1, 0, appearance)


def _group_by_topic(items: Iterable[Item]) -> list[tuple[str, list[Item]]]:
    """Column name → its articles, mains first and then newest first.

    Columns are ordered by the topic stage's ranking, falling back to
    first-appearance (that is, sources.toml) order for anything it did not
    file. Within a column, sources interleave — two feeds that both filed on
    the same topic read as one column, not as two lists.
    """
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(it.group, []).append(it)
    ordered = sorted(
        enumerate(groups.items()),
        key=lambda pair: _group_order(pair[1][1], pair[0]),
    )
    return [(name, _sorted_group(rows)) for _, (name, rows) in ordered]


def _take_lead(groups: list[tuple[str, list[Item]]]) -> tuple[Item | None, str]:
    """The front-page lead, and the section it came out of.

    The top of the first section that has a dek — which, once the topic stage
    has run, is the main story of the most significant topic. A lead runs with
    a summary underneath it, and a headline alone in that slot reads like a
    mistake.
    Falling back to the very first article keeps a dekless edition — a fresh
    store where summarizing has not run yet — from having no lead at all.
    """
    for name, rows in groups:
        for i, row in enumerate(rows):
            if row.dek:
                return rows.pop(i), name
    for name, rows in groups:
        if rows:
            return rows.pop(0), name
    return None, ""


def _take_side(
    groups: list[tuple[str, list[Item]]], lead_section: str, n: int = 2
) -> list[Item]:
    """The stories stacked beside the lead: the top of each *other* section.

    One from each section rather than n from one is what makes the top of the
    page a summary of the whole paper. The lead's own section is skipped while
    there is anywhere else to look, for the same reason.
    """
    out: list[Item] = []
    for skip_lead_section in (True, False):
        for name, rows in groups:
            if len(out) >= n:
                return out
            if skip_lead_section and name == lead_section:
                continue
            while rows and len(out) < n:
                out.append(rows.pop(0))
                if skip_lead_section:
                    break  # one per section on the first pass
    return out


def build(
    articles: list[dict],
    key: str,
    date: str,
    built_at: str,
) -> Edition:
    """Lay a flat article list out as a front page plus its continuation."""
    items = [_to_item(a) for a in articles if (a.get("title") or "").strip()]
    groups = _group_by_topic(items)

    lead, lead_section = _take_lead(groups)
    side = _take_side(groups, lead_section)
    lead_image = lead.image if lead else None

    sections: list[Section] = []
    below: list[Section] = []
    front_image_used = False
    for i, (name, rows) in enumerate(groups):
        if not rows:
            continue
        if i < FRONT_SECTIONS:
            top, rest = rows[0], rows[1:1 + FRONT_REST]
            # One photograph on the front page besides the lead's. More turns
            # the top of the paper into a contact sheet.
            image = None
            if not front_image_used and top.image and top.image != lead_image:
                image, front_image_used = top.image, True
            sections.append(Section(name=name, top=top, rest=rest, image=image))
            rows = rows[1 + FRONT_REST:]
        if rows:
            # The band's photograph is its top story's, and the page prints the
            # two together. Any other article's would be a picture with no
            # caption sitting under somebody else's headline.
            below.append(Section(
                name=name, top=rows[0], rest=rows[1:],
                image=rows[0].image,
            ))

    counts: dict[str, int] = {}
    for it in items:
        counts[it.source] = counts.get(it.source, 0) + 1

    return Edition(
        key=key, date=date, built_at=built_at,
        lead=lead, side=side, sections=sections, below=below,
        sources=dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    )


def filtered(edition: Edition, medium: str | None) -> Edition:
    """The same edition with only one medium's articles in it.

    Re-laid out rather than hidden: dropping every video from a column would
    otherwise leave the column's top story missing and the rest orphaned under
    a heading. Cheap enough to do per request — it is a list comprehension and
    a regroup, no I/O.
    """
    if not medium or medium not in MEDIA:
        return edition
    kept = [i.as_dict() | {"text": i.body, "summary": i.dek}
            for i in edition.items if i.medium == medium]
    return build(kept, edition.key, edition.date, edition.built_at)


def refiled(edition: Edition, sources: list[dict]) -> Edition:
    """The same edition with `section` and `medium` taken from sources.toml
    as it reads *now*.

    Those two are layout, not content: re-filing a source into another column
    should show up the moment the file is saved, not after the next gather.
    Snapshots bake in the values that were live when they were written, so the
    fix is to re-apply them on the way out — the articles are untouched, and
    `build` re-runs anyway on every render.

    An article the topic stage filed is not moved by this: its column is the
    topic, and `section` is only the fallback underneath it. Editing
    sources.toml re-columns exactly the articles topics never reached.
    """
    by_source = {s.get("name"): s for s in sources}
    kept = []
    for i in edition.items:
        src = by_source.get(i.source) or {}
        kept.append(i.as_dict() | {
            "text": i.body,
            "summary": i.dek,
            "section": src.get("section") or i.source,
            "medium": src.get("medium") or i.medium,
        })
    return build(kept, edition.key, edition.date, edition.built_at)


# --- snapshots ------------------------------------------------------------
#
# An edition is stored as the articles it was built from, not as its layout:
# re-running `build` is microseconds, and it means a layout improvement
# reaches every archived edition instead of only the ones built after it.

def to_snapshot(edition: Edition) -> dict[str, Any]:
    return {
        "key": edition.key,
        "date": edition.date,
        "built_at": edition.built_at,
        "articles": edition.total,
        "sources": edition.sources,
        "items": [i.as_dict() for i in edition.items],
    }


def from_snapshot(data: dict[str, Any]) -> Edition:
    items = [
        # `build` speaks the collect_current_edition dialect, where the body is
        # `text` and the dek is `summary`; a snapshot is written in the Item
        # dialect, where they are `body` and `dek`. Accept either, so a
        # hand-written snapshot and one this package produced both load.
        dict(i,
             text=i.get("body") or i.get("text") or "",
             summary=i.get("dek") or i.get("summary") or "")
        for i in data.get("items") or []
    ]
    return build(
        items,
        key=str(data.get("key") or ""),
        date=str(data.get("date") or ""),
        built_at=str(data.get("built_at") or ""),
    )
