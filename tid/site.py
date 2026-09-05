"""The site: Jinja templates plus the handful of filters they need.

This is the whole presentation layer. It takes an `edition.Edition` and gives
back a string of HTML; it never touches the store, the queue or the network,
so a page can be rendered in a test with three dictionaries.

The templates live beside this module in `templates/`, and the stylesheet and
script are `{% include %}`d into every page rather than linked. One request per
page is the right trade for a reader on an e-ink device that wakes its radio
per fetch — and it means a saved page is still the whole design.
"""
from __future__ import annotations

import functools
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
from markupsafe import Markup

from . import icons, sources
from .edition import Edition, Item
from .sources import Link

TEMPLATES = Path(__file__).parent / "templates"

# Words of the body shown in the preview drawer. Enough to tell whether the
# piece is worth opening, short enough to keep the attribute small on a page
# carrying a hundred of them.
EXCERPT_WORDS = 45

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")
_TEX_RE = re.compile(r"\$[^$\n]+\$|\$\$.+?\$\$|\\\(.+?\\\)|\\\[.+?\\\]", re.DOTALL)


# --- filters --------------------------------------------------------------

def human_date(iso: str) -> str:
    """"Sunday 30 August 2026". The day name is the point: it is how a reader
    tells one edition from the next in the nav."""
    try:
        return datetime.fromisoformat(iso).strftime("%A %-d %B %Y")
    except ValueError:
        return iso or "undated edition"


def short_date(iso: str) -> str:
    """"Sat 29" — the prev/next labels, which have one line to fit in."""
    try:
        return datetime.fromisoformat(iso).strftime("%a %-d")
    except ValueError:
        return iso or "—"


def human_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M UTC")
    except ValueError:
        return ""


def excerpt(body: str, words: int = EXCERPT_WORDS) -> str:
    """The opening of an article, for the preview drawer."""
    parts = (body or "").split()
    if not parts:
        return ""
    text = " ".join(parts[:words])
    return text + ("…" if len(parts) > words else "")


def read_url(item: Item, edition_key: str = "") -> str:
    """Where the full rewritten text of an article lives."""
    if not item.id:
        return ""
    return f"/e/{edition_key}/a/{item.id}" if edition_key else f"/a/{item.id}"


def glyph(medium: str) -> Markup:
    return Markup(icons.glyph(medium))


def filter_glyph(medium: str) -> Markup:
    return Markup(icons.filter_glyph(medium))


def icon(url: str) -> str:
    return icons.icon_url(url)


def links(item: Item) -> list[Link]:
    """The byline's chips: where this article sends the reader.

    The source type decides. A feed article has one, its source; a Hacker
    News story has two — the discussion, then the site it points at — and
    the template prints an arrow between them.
    """
    return sources.links_for(item)


def has_math(body: str) -> bool:
    return bool(_TEX_RE.search(body or ""))


def article_html(body: str) -> Markup:
    r"""The rewritten body as HTML: paragraphs, code blocks, inline code.

    Deliberately not a Markdown renderer. The rewrite stage emits plain prose
    with fenced code and TeX in it — that is the whole vocabulary — and running
    it through a full Markdown parser would start interpreting the prose
    instead (a line beginning "1980. " becoming an ordered list, an underscore
    in a filename becoming emphasis).

    TeX passes through untouched, for MathJax on the page or for the reader's
    own eyes if it never loads.
    """
    blocks: list[str] = []

    def stash(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\x00CODE{len(blocks) - 1}\x00"

    text = _FENCE_RE.sub(stash, body or "")

    out: list[str] = []
    for para in re.split(r"\n{1,}", text):
        para = para.strip()
        if not para:
            continue
        m = re.fullmatch(r"\x00CODE(\d+)\x00", para)
        if m:
            out.append(f"<pre><code>{html.escape(blocks[int(m.group(1))])}</code></pre>")
            continue
        safe = html.escape(para)
        safe = _INLINE_RE.sub(
            lambda mm: f"<code>{mm.group(1)}</code>", safe
        )
        # A fence that shared a paragraph with prose: expand it in place.
        safe = re.sub(
            r"\x00CODE(\d+)\x00",
            lambda mm: f"<code>{html.escape(blocks[int(mm.group(1))])}</code>",
            safe,
        )
        out.append(f"<p>{safe}</p>")
    return Markup("\n".join(out))


# --- environment ----------------------------------------------------------

@functools.cache
def env() -> jinja2.Environment:
    e = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        autoescape=jinja2.select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    e.filters.update(
        human_date=human_date,
        short_date=short_date,
        human_time=human_time,
        excerpt=excerpt,
        read_url=read_url,
        glyph=glyph,
        filter_glyph=filter_glyph,
        icon=icon,
        links=links,
        article_html=article_html,
    )
    return e


# --- pages ----------------------------------------------------------------

# The filter row. `None` is the "All" chip; the rest appear only when the
# edition actually holds that medium.
_FILTER_LABELS = {"read": "Read", "watch": "Watch", "listen": "Listen"}


def _filters(edition: Edition, active: str | None, base: str) -> list[dict]:
    present = edition.media_present()
    chips = [{"label": "All", "medium": "", "title": "Everything",
              "on": not active, "href": base}]
    # A single medium is not a choice: an all-text paper shows no Read chip.
    if len(present) > 1:
        for m in present:
            chips.append({
                "label": "", "medium": m, "title": _FILTER_LABELS[m],
                "on": active == m, "href": f"{base}?m={m}",
            })
    return chips


def render_edition(
    edition: Edition,
    *,
    prev: dict | None = None,
    next: dict | None = None,
    medium: str | None = None,
    base: str = "/",
    empty_note: str = "",
) -> str:
    """`base` is this edition's own URL — where the filter chips point. It is
    not derivable from the key: an edition with nothing in it is not
    snapshotted, so `/e/{key}` would 404 and only `/` can show it."""
    return env().get_template("edition.html").render(
        edition=edition,
        prev=prev,
        next=next,
        filters=_filters(edition, medium, base),
        empty_note=empty_note or (
            "The first ingest has not finished. It gathers, summarizes and "
            "rewrites every source in sources.toml, which takes a few minutes."
        ),
    )


def render_article(item: Item, edition_key: str, back: str) -> str:
    return env().get_template("article.html").render(
        item=item, back=back, needs_math=has_math(item.body),
    )


def render_sources(
    sources: list[dict[str, Any]],
    counts: dict[str, int],
    fetched_at: str = "",
) -> str:
    seen: list[str] = []
    for s in sources:
        if s["section"] not in seen:
            seen.append(s["section"])
    return env().get_template("sources.html").render(
        sources=sources, sections=seen, counts=counts, fetched_at=fetched_at,
    )
