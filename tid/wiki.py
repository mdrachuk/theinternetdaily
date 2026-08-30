"""Wikipedia/Wikiquote helpers for tid.

- Daily 'Current events' portal URL for a top news section
- Quote of the day (Wikiquote) for the cover
- 'Did you know ...' nuggets (Wikipedia Main Page) for the cover

Every network call takes the caller's `httpx.AsyncClient`.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Optional

import httpx
import trafilatura

from .extract import fetch_html
from .llm import LLMBackend


# --- Current events -------------------------------------------------------

def current_events_url(d: date | None = None) -> str:
    """URL of the Wikipedia Current Events daily portal page."""
    if d is None:
        d = date.today()
    return f"https://en.wikipedia.org/wiki/Portal:Current_events/{d.strftime('%Y_%B_%-d')}"


def current_events_title(d: date | None = None) -> str:
    if d is None:
        d = date.today()
    return f"World news — {d.strftime('%B %-d, %Y')}"


# --- Quote of the day -----------------------------------------------------

# The QOTD page uses {{Wikiquote:Quote of the day/Template | quote = ... | author = ...}}
_QUOTE_FIELD_RE = re.compile(r"\|\s*quote\s*=\s*(?:<!--.*?-->)?\s*(.+?)(?=\n\s*\|\s*\w+\s*=|\n*\}\})", re.IGNORECASE | re.DOTALL)
_AUTHOR_FIELD_RE = re.compile(r"\|\s*author\s*=\s*(.+?)(?=\n\s*\|\s*\w+\s*=|\n*\}\})", re.IGNORECASE | re.DOTALL)


def _strip_wiki(s: str) -> str:
    """Strip basic wikitext to plain text."""
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)   # [[a|b]] → b
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)              # [[a]] → a
    s = re.sub(r"'''([^']+)'''", r"\1", s)                  # '''bold''' → bold
    s = re.sub(r"''([^']+)''", r"\1", s)                    # ''italics'' → italics
    # Tags → space so `moon<br/>Under` doesn't glue into `moonUnder`.
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # The cover template wraps the quote in `` … '' itself; trim any leading
    # or trailing quotation marks from the source so we don't get doubled.
    return s.strip("\"'“”‘’").strip()


async def fetch_quote_of_day(
    client: httpx.AsyncClient,
    max_words: int = 40,
    days_back: int = 14,
) -> Optional[tuple[str, str]]:
    """Return (quote_text, attribution) or None. Searches back up to
    days_back days for a quote of at most max_words words — Wikiquote
    sometimes picks very long quotes which don't fit on the cover."""
    for delta in range(days_back):
        d = date.today() - timedelta(days=delta)
        page = f"Wikiquote:Quote_of_the_day/{d.strftime('%B_%-d,_%Y')}"
        try:
            r = await client.get(
                "https://en.wikiquote.org/w/api.php",
                params={
                    "action": "parse",
                    "page": page,
                    "format": "json",
                    "prop": "wikitext",
                },
                timeout=15,
            )
            data = r.json()
            wikitext = data.get("parse", {}).get("wikitext", {}).get("*", "")
            if not wikitext:
                continue
            q = _QUOTE_FIELD_RE.search(wikitext)
            a = _AUTHOR_FIELD_RE.search(wikitext)
            if not q:
                continue
            quote = _strip_wiki(q.group(1))
            author = _strip_wiki(a.group(1)) if a else ""
            wc = len(quote.split())
            if wc < 3 or wc > max_words:
                continue
            return quote, author
        except Exception:
            continue
    return None


# --- World news (today's Current events portal) --------------------------

_NAV_NOISE = {"Appearance", "Main Page"}


_TRAILING_SOURCE_RE = re.compile(r"\s*\(([^()]{1,40})\)\s*$")


def _split_source(body: str) -> tuple[str, str | None]:
    """Split a Wikipedia bullet into (text, source) where source is the
    content of a trailing parenthetical citation, if any."""
    m = _TRAILING_SOURCE_RE.search(body)
    if not m:
        return body.strip(), None
    return body[: m.start()].strip().rstrip(".") + ".", m.group(1).strip()


def _bullets_from_text(text: str) -> list[dict]:
    items: list[dict] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s in _NAV_NOISE:
            continue
        if s.startswith("- "):
            body = s[2:].strip()
            if not (body.endswith(")") or len(body) > 60):
                continue
            text_, source = _split_source(body)
            items.append({"text": text_, "source": source})
    return items


def _extract_plain(html: str) -> str:
    return trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    ) or ""


async def _parse_current_events_day(
    client: httpx.AsyncClient, url: str
) -> list[dict]:
    """Extract news bullets from one day's portal page.
    Returns [{"text": ..., "source": ...}, ...]; source may be None."""
    try:
        html = await fetch_html(client, url)
        if not html:
            return []
        text = await asyncio.to_thread(_extract_plain, html)
    except Exception:
        return []
    return _bullets_from_text(text)


_TECH_FEEDS = [
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
]


# Keywords (case-insensitive, whole-word) that mark a story as Western/major.
_WESTERN_RE = re.compile(
    r"\b("
    r"United\s+States|U\.?S\.?|US|America[ns]?|"
    r"European\s+Union|EU|Europe|"
    r"United\s+Kingdom|U\.?K\.?|UK|Britain|British|England|"
    r"German[sy]?|France|French|Italy|Italian|Spain|Spanish|"
    r"Netherlands|Dutch|Canad(?:a|ian)|Australia[n]?|"
    r"Japan(?:ese)?|South\s+Korea[n]?|NATO|G7|G20"
    r")\b",
    re.IGNORECASE,
)


async def fetch_tech_headlines(
    client: httpx.AsyncClient, per_feed: int = 2, max_items: int = 5
) -> list[dict]:
    """Return up to max_items recent tech headlines with their source feed
    and the article URL. [{"text", "source", "url"}, ...]"""
    from .fetch import fetch_rss

    # All three feeds at once — they are independent and each is a slow tail.
    # These are decorations, not articles: `per_feed` below is the real bound,
    # so the whole feed coming back costs a few list items, not a fetch.
    results = await asyncio.gather(
        *(fetch_rss(client, name, url) for name, url in _TECH_FEEDS),
        return_exceptions=True,
    )

    out: list[dict] = []
    seen: set[str] = set()
    for (feed_name, _), items in zip(_TECH_FEEDS, results):
        if isinstance(items, BaseException):
            continue
        count = 0
        for item in items:
            title = item.title
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            out.append({"text": title, "source": feed_name, "url": item.url or None})
            count += 1
            if count >= per_feed:
                break
            if len(out) >= max_items:
                return out
    return out[:max_items]


async def fetch_western_news(
    client: httpx.AsyncClient, max_items: int = 2, days_back: int = 3
) -> list[dict]:
    """Pull Wikipedia Current Events bullets that mention a Western country
    or major-ally context. Returns [{"text": ..., "source": ...}, ...]"""
    from datetime import date as _date, timedelta as _td

    today = _date.today()
    seen: set[str] = set()
    out: list[dict] = []
    for delta in range(days_back):
        day = today - _td(days=delta)
        for item in await _parse_current_events_day(client, current_events_url(day)):
            if item["text"] in seen:
                continue
            seen.add(item["text"])
            if not _WESTERN_RE.search(item["text"]):
                continue
            out.append(item)
            if len(out) >= max_items:
                return out
    return out


async def fetch_world_news(client: httpx.AsyncClient) -> list[dict]:
    """5 tech headlines + up to 2 Western-relevant Wikipedia items.
    Each item: {"text": ..., "source": ...}."""
    tech, western = await asyncio.gather(
        fetch_tech_headlines(client, per_feed=2, max_items=5),
        fetch_western_news(client, max_items=2),
    )
    return tech + western


# --- News bullet summarization --------------------------------------------

_WORLD_NEWS_SYSTEM = (
    "You rewrite news bullets for a compact daily digest.\n"
    "- Output ONE short sentence per input bullet, max 18 words.\n"
    "- Preserve all key facts (who, what, where).\n"
    "- Do NOT include any source citation in the output (the source is shown separately).\n"
    "- No preamble, no quotes, no commentary.\n"
    "- Output EXACTLY one line per input bullet, in the same order, prefixed with its number and a period."
)


async def summarize_world_news(
    backend: LLMBackend, items: list[dict]
) -> list[dict]:
    """Rewrite each news item into a single short sentence (~15 words),
    preserving its source attribution. One LLM call per batch, through the
    configured backend — so a local-only install makes no Anthropic calls."""
    if not items:
        return items

    user = "\n".join(f"{i+1}. {it['text']}" for i, it in enumerate(items))
    try:
        text = await backend.chat(
            _WORLD_NEWS_SYSTEM, user, max_tokens=150 * len(items)
        )
    except Exception:
        return items

    shortened: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^\d+[.)]\s*(.*)$", s)
        shortened.append(m.group(1) if m else s)

    if len(shortened) != len(items):
        return items
    return [
        {
            "text": shortened[i],
            "source": items[i].get("source"),
            "url": items[i].get("url"),
        }
        for i in range(len(items))
    ]


# --- Did you know ---------------------------------------------------------

def _dyk_from_text(text: str, limit: int) -> list[str]:
    idx = text.find("Did you know")
    if idx < 0:
        return []
    items: list[str] = []
    for line in text[idx:].splitlines()[1:]:
        s = line.strip()
        if not s:
            if items:
                break
            continue
        if s.startswith("- "):
            body = s[2:].lstrip(". ").strip()
            # Strip leading "that " for terser display
            body = re.sub(r"^that\s+", "", body, flags=re.IGNORECASE)
            items.append(body)
        elif items:
            break
    return items[:limit]


async def fetch_did_you_know(client: httpx.AsyncClient, limit: int = 4) -> list[str]:
    """Pull 'Did you know ...' bullets from today's Wikipedia Main Page."""
    try:
        html = await fetch_html(client, "https://en.wikipedia.org/wiki/Main_Page")
        if not html:
            return []
        text = await asyncio.to_thread(
            lambda h: trafilatura.extract(h, include_comments=False) or "", html
        )
    except Exception:
        return []
    return _dyk_from_text(text, limit)
