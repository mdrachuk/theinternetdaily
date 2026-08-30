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
  sections  the front-page columns, one per `section` in sources.toml
  below     the continuation of every section, further down the same page

An article appears exactly once. The front page skims the top off each
section; `below` carries whatever the skim left behind.
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
    )


def _group_by_section(items: Iterable[Item]) -> list[tuple[str, list[Item]]]:
    """Section name → its articles, newest first, in first-appearance order.

    Insertion order is sources.toml order, so the first section on the page is
    the first one configured. Within a section, sources interleave by date —
    two feeds filed under "World" read as one column, not as two lists.
    """
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(it.section, []).append(it)
    return [
        (name, sorted(rows, key=lambda i: i.iso_date, reverse=True))
        for name, rows in groups.items()
    ]


def _take_lead(groups: list[tuple[str, list[Item]]]) -> tuple[Item | None, str]:
    """The front-page lead, and the section it came out of.

    The top of the first section that has a dek: a lead runs with a summary
    underneath it, and a headline alone in that slot reads like a mistake.
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
    groups = _group_by_section(items)

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
