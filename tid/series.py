"""Recurring headlines: the series a desk files every day, kept off the front.

Some sources open a large share of their output with the same words. Kyiv
Independent posts "General Staff: Russia has lost 1,641,489 troops in Ukraine
since Feb. 24, 2022" every morning with one number changed; Meduza files
"Война. 1736-й день. …" daily; the Guardian runs a "Ukraine war briefing: …"
and a "Cop31 live: …". Each of those is a fine article. As a *headline* it is
furniture: the reader has seen it yesterday and will see it tomorrow, and the
topic stage — which only sees today's paper — kept putting it in the lead,
because a daily roundup of the most significant subject reads as the most
significant story.

So a series is never a main story and never leads. It still runs, under its
topic, after the stories that are not series (`tid.edition`). The judgement
is made from the source's own recent headlines, not by the model, which has no
memory of yesterday's paper:

  template   the whole headline, with every number masked, matches one the
             same source filed on an earlier day ("General Staff: …",
             "Apple Releases Safari Technology Preview 257 …");
  numbered   the kicker — the words before the first ":" or ". " — carries a
             number, and an earlier day had the same kicker with another one
             ("The Guide #270:", "NFL week 11:");
  daily      the kicker opens the source's headlines on at least half the days
             it published anything lately, about once on each of them
             ("Ukraine war latest:", "Война.").

The "about once" is what tells a desk's series from a label many different
stories carry: "Show HN:" opens several headlines a day. A label that is used
once a day all the same — "Ask HN:" on a quiet week — is named by the source
type (`post_labels`), because no count tells it from a briefing. "Exclusive:"
or a wire credit ("Reuters:") shows up on a third of the days at most and is
left alone; so is a columnist's byline, which sits at the end of a headline
where no kicker is read.

A series needs a history to be one: the first "Ukraine war briefing" a store
ever sees is just a story.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Sequence

from .store.base import ArticleRow, Store

# How far back a headline is compared. Two weeks holds every daily series
# several times over and a weekly one twice, without letting a format a desk
# dropped a month ago still mark today's story.
WINDOW_DAYS = 14

# A kicker on fewer days than this is not a pattern yet, whatever the share.
MIN_DAYS = 3

# "About once a day": the uses of a kicker over the days it appears on. A
# briefing is 1.0; "Show HN:" runs at two or three.
MAX_PER_DAY = 1.25

# The kicker: up to the first ": " or ". ", short enough to be a series name
# rather than a sentence that happens to contain a colon.
_KICKER_RE = re.compile(r"^(.{1,60}?)(?::|\.)\s")
_KICKER_WORDS = 5

# A number, with its thousands separators and decimal point: "1,641,489" and
# "26.2" are one number each, so they mask to one token.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def mask(text: str) -> str:
    """The headline as a template: case-folded, punctuation gone, and every
    number replaced by `#`, so today's tally and yesterday's compare equal."""
    folded = _NUMBER_RE.sub("#", unicodedata.normalize("NFKC", text).casefold())
    return " ".join(
        "".join(c if c.isalnum() or c == "#" else " " for c in folded).split()
    )


def kicker(title: str) -> str | None:
    """The words a headline opens with before its first ":" or ". ", or None
    when it has none, or they run too long to be the name of anything."""
    m = _KICKER_RE.match(title.strip())
    if not m:
        return None
    k = m.group(1).strip()
    if not k or len(k.split()) > _KICKER_WORDS:
        return None
    return k


def is_series(
    title: str,
    day: str,
    history: Iterable[tuple[str, str]],
    labels: Iterable[str] = (),
) -> bool:
    """Is `title`, filed on `day`, one more instance of something its source
    files every day?

    `history` is the same source's headlines as `(title, date)` pairs; only
    those from the `WINDOW_DAYS` before `day` count, so the caller may pass
    more. `labels` are kickers the source type says name a kind of post, not
    a series.
    """
    try:
        lo = (date.fromisoformat(day[:10]) - timedelta(days=WINDOW_DAYS)).isoformat()
    except ValueError:
        return False
    day = day[:10]
    past = [(t, d[:10]) for t, d in history if lo <= d[:10] < day]
    if not past:
        return False

    template = mask(title)
    if template and any(mask(t) == template for t, _ in past):
        return True

    k = kicker(title)
    if k is None:
        return False
    key = mask(k)
    if not key or key in {mask(label) for label in labels}:
        return False
    same = [(t, d) for t, d in past if (pk := kicker(t)) and mask(pk) == key]
    if not same:
        return False

    if any(c.isdigit() for c in k) and any(kicker(t) != k for t, _ in same):
        return True

    days = {d for _, d in past}
    on = {d for _, d in same}
    return (
        len(on) >= MIN_DAYS
        and 2 * len(on) >= len(days)
        and len(same) <= MAX_PER_DAY * len(on)
    )


async def series_ids(
    store: Store,
    rows: Sequence[ArticleRow],
    labels_for=lambda kind: (),
) -> set[str]:
    """The ids among `rows` whose headlines are series.

    One store read per source, over the window before the oldest of its rows.
    `labels_for(kind)` gives a source type's `post_labels`; `tid.sources`
    supplies it, and the default is none.
    """
    by_source: dict[str, list[ArticleRow]] = defaultdict(list)
    for r in rows:
        by_source[r.source].append(r)
    out: set[str] = set()
    for source, group in by_source.items():
        days = [r.sort_date[:10] for r in group]
        try:
            since = (
                date.fromisoformat(min(days)) - timedelta(days=WINDOW_DAYS)
            ).isoformat()
        except ValueError:
            continue
        history = await store.titles_since(source, since)
        for r in group:
            if is_series(r.title, r.sort_date, history, labels_for(r.kind)):
                out.add(r.id)
    return out
