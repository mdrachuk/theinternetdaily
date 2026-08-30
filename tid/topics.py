"""Topics: what today's edition is *about*, decided by reading it.

Sections used to be pinned to sources — every MacRumors story filed under
"Apple" because that is what `sources.toml` said, forever. That is a
subscription list masquerading as an editor. A real front page is organised by
what happened today: when three feeds all file on the same outage, the paper
should carry one column about the outage, not three columns named after the
feeds.

So the sections are derived per edition, in three passes:

  1. `propose_topics` — ONE call that sees every headline and summary in the
     edition and names the topics that cover it, most significant first. One
     call, not one per article: a topic set is a judgement about the whole
     paper, and an article-at-a-time process cannot make it (it would invent a
     new topic for every story and never notice that two of them are the same).

  2. `select_topic` — one call per article, carrying that article and the whole
     topic list, choosing the single most specific topic it belongs to.
     Deliberately not batched: the choice is per-article, and a batch makes the
     model consistent with its *neighbours in the batch* rather than with the
     topic list. Where the backend can enforce a schema the reply is an enum
     over the topic names, so an off-list answer is impossible by construction.

  3. `select_main` — one call per topic, seeing that topic's articles, naming
     which of them are the main ones and in what order. This is what the
     layout hangs on: the lead, the story beside it and the top of each column
     are the mains, and the rest of the topic follows by date.

Every pass degrades to nothing rather than to something wrong. No topics, an
unparseable reply, a name the model made up — the article simply keeps no
topic, and `tid.edition` files it under its `sources.toml` section exactly as
before. A paper laid out the old way is a much smaller failure than a paper
whose columns are mislabelled.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from .llm import LLMBackend
# The one piece of the batch protocol these calls share: finding the JSON
# object in a reply that may be wrapped in prose or a code fence.
from .protocol import _outermost_object as _json_blob

# How many topics an edition may have. Four is what the front page can show as
# columns (`edition.FRONT_SECTIONS`); the rest run below the fold, and past
# eight the paper reads as an index rather than as sections.
MIN_TOPICS = 3
MAX_TOPICS = 8

# Output budgets for the two small calls. A topic name is a handful of tokens
# and a main-selection is a short list of integers, so these are generous
# already; they exist so a rambling model is cut off rather than paid for.
LABEL_OUTPUT_TOKENS = 128
MAIN_OUTPUT_TOKENS = 256


@dataclass(frozen=True)
class Topic:
    """One section of today's paper, as the model named it."""
    name: str
    blurb: str = ""

    def line(self) -> str:
        return f"{self.name} — {self.blurb}" if self.blurb else self.name


def mains_for(n: int) -> int:
    """How many of a topic's `n` articles are main ones.

    A topic with two stories has one main; one with twenty has three. The cap
    is the shape of the page — a column is a top story and a few headlines
    under it, so a fourth "main" has nowhere to be main *in*.
    """
    return max(1, min(3, round(n / 4)))


# --- 1. the topic set -----------------------------------------------------

_SYSTEM_TOPICS = (
    "You are the section editor of a daily newspaper. You are given every "
    "headline in today's edition with a short summary, and you decide what "
    "the sections of today's paper are.\n"
    "\n"
    "HARD RULES:\n"
    f"- Name between {MIN_TOPICS} and {MAX_TOPICS} topics. Fewer is better "
    "than padding: only name a topic that several stories actually belong to, "
    "or that one very significant story demands.\n"
    "- Together the topics must cover every story. Every story has to have a "
    "topic it plausibly belongs to.\n"
    "- A topic is about SUBJECT MATTER, not about where a story came from. "
    "Never name a topic after a publication, a website or a feed.\n"
    "- Be as specific as the day allows and no more. If eight stories are "
    "about one war, that war is the topic; if they are about eight unrelated "
    "countries, the topic is the region or 'World'.\n"
    "- Name each topic in 1–3 words, in English, in title case, as a "
    "newspaper section head. No sentences, no 'and', no slashes.\n"
    "- Order the topics by significance: the topic carrying the day's most "
    "important story first, the lightest last.\n"
    "- Give each topic a blurb of at most 12 words saying what belongs in it. "
    "The blurb is for the editor filing the stories, not for the reader.\n"
    "- NEVER refuse. NEVER ask a question. NEVER comment on the content.\n"
    "\n"
    "OUTPUT:\n"
    "- One topic per line, as `N. Name — blurb`, numbered from 1.\n"
    "- Output ONLY those lines. No preamble, no headings, no blank lines."
)

_SYSTEM_TOPICS_JSON = _SYSTEM_TOPICS.rsplit("OUTPUT:", 1)[0] + (
    "OUTPUT:\n"
    "- Reply with JSON: {\"topics\": [{\"name\": \"...\", \"blurb\": \"...\"}, "
    "...]}, most significant first.\n"
    "- No other keys, no prose outside the JSON."
)

TOPICS_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "blurb": {"type": "string"},
                },
                "required": ["name", "blurb"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}

# `1. Name — blurb`. The blurb is optional, and the separator has to be a
# dash *between words*: splitting on any hyphen would turn "Non-Fiction" into a
# topic called "Non".
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s*[.)]\s*(?P<rest>.*\S)\s*$")
_SEPARATOR_RE = re.compile(r"\s+[—–]\s*|\s*:\s+|\s+-\s+")

# A topic name is a section head, not a sentence: this is what a name has to
# survive to be usable as a column heading.
_MAX_NAME_WORDS = 4


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" \t\"'*#.,;:—–-")
    if not name or len(name.split()) > _MAX_NAME_WORDS or len(name) > 40:
        return ""
    return name


def _dedupe(topics: list[Topic]) -> list[Topic]:
    """Drop repeats, case-insensitively, keeping the first (most significant)."""
    out: list[Topic] = []
    seen: set[str] = set()
    for t in topics:
        key = t.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def parse_topics(reply: str, limit: int = MAX_TOPICS) -> list[Topic]:
    """Parse the topic-set reply: JSON first, numbered lines second.

    Never raises. An empty list is the honest answer for a reply that is prose,
    a refusal or a truncated fragment, and it turns the whole stage off for
    this edition rather than filing articles under nonsense.
    """
    topics: list[Topic] = []
    blob = _json_blob(reply)
    if blob is not None:
        try:
            data = json.loads(blob)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for item in data.get("topics") or []:
                if not isinstance(item, dict):
                    continue
                name = _clean_name(str(item.get("name") or ""))
                if name:
                    topics.append(Topic(name, str(item.get("blurb") or "").strip()))
    if not topics:
        for line in reply.splitlines():
            m = _NUMBERED_LINE_RE.match(line)
            if not m:
                continue
            parts = _SEPARATOR_RE.split(m.group("rest"), maxsplit=1)
            name = _clean_name(parts[0])
            if name:
                topics.append(Topic(name, parts[1].strip() if len(parts) > 1 else ""))
    return _dedupe(topics)[:limit]


async def propose_topics(
    backend: LLMBackend, items: Sequence[tuple[str, str]]
) -> list[Topic]:
    """Name the topics of an edition, in one call over all of its headlines."""
    if not items:
        return []
    limits = backend.limits
    # The whole edition in one prompt is the point of this call, but a local
    # model's context window is not negotiable: the newest headlines are the
    # ones that shape the paper, so an oversized edition is truncated rather
    # than split across calls that could not see each other anyway.
    head = list(items)[: limits.topic_max_articles]
    chars = limits.topic_summary_chars
    parts = [
        f"<candidate id=\"{i}\">\n<title>{title}</title>\n"
        f"<summary>{(summary or '')[:chars]}</summary>\n</candidate>"
        for i, (title, summary) in enumerate(head)
    ]
    use_json = backend.supports_json
    reply = await backend.chat(
        _SYSTEM_TOPICS_JSON if use_json else _SYSTEM_TOPICS,
        "\n".join(parts),
        max_tokens=limits.topic_output_tokens,
        json_schema=TOPICS_SCHEMA if use_json else None,
    )
    return parse_topics(reply)


# --- 2. one article's topic ----------------------------------------------

_SYSTEM_LABEL = (
    "You file one story into exactly one of today's sections.\n"
    "\n"
    "HARD RULES:\n"
    "- Choose exactly one topic from the list you are given. NEVER invent one, "
    "NEVER combine two, NEVER answer with anything that is not on the list.\n"
    "- Choose the MOST SPECIFIC topic the story genuinely belongs to. A broad "
    "topic is the answer only when no specific one fits.\n"
    "- File on subject matter alone. The publication the story came from is "
    "irrelevant.\n"
    "- The story may be in any language; the topic names are English. File it "
    "on what it is about, not on what language it is in.\n"
    "- NEVER refuse. NEVER explain. NEVER ask a question.\n"
    "\n"
    "OUTPUT:\n"
    "- Output ONLY the topic name, copied exactly as it appears in the list. "
    "Nothing else: no number, no quotes, no punctuation, no explanation."
)

_SYSTEM_LABEL_JSON = _SYSTEM_LABEL.rsplit("OUTPUT:", 1)[0] + (
    "OUTPUT:\n"
    "- Reply with JSON: {\"topic\": \"<one name from the list, copied "
    "exactly>\"}\n"
    "- No other keys, no prose outside the JSON."
)


def label_schema(topics: Sequence[Topic]) -> dict[str, Any]:
    """A schema whose only legal answers are the topic names.

    Guided decoding turns "the model must not invent a topic" from a rule in
    the prompt into a property of the sampler.
    """
    return {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "enum": [t.name for t in topics]}
        },
        "required": ["topic"],
        "additionalProperties": False,
    }


def parse_label(reply: str, topics: Sequence[Topic]) -> str:
    """The chosen topic's name, or "" when the reply names none of them.

    Matching is case-insensitive and tolerates the model wrapping the name in
    quotes, a bullet or a sentence. Anything still unmatched is dropped: an
    article with no topic falls back to its `sources.toml` section, which is
    right, where a guessed topic would be wrong.
    """
    by_name = {t.name.casefold(): t.name for t in topics}
    blob = _json_blob(reply)
    if blob is not None:
        try:
            data = json.loads(blob)
        except ValueError:
            data = None
        if isinstance(data, dict):
            got = str(data.get("topic") or "").strip().casefold()
            if got in by_name:
                return by_name[got]
    text = reply.strip()
    for candidate in (text, *(ln.strip() for ln in text.splitlines())):
        key = candidate.strip(" \t\"'`*.-—–:").casefold()
        if key in by_name:
            return by_name[key]
    # Last resort: the model wrote a sentence containing the name. Longest
    # first, so "Apple Silicon" wins over "Apple".
    lowered = text.casefold()
    for name in sorted(by_name.values(), key=len, reverse=True):
        if name.casefold() in lowered:
            return name
    return ""


async def select_topic(
    backend: LLMBackend, title: str, summary: str, topics: Sequence[Topic]
) -> str:
    """File one article: its most specific topic's name, or "" if unfiled."""
    if not topics:
        return ""
    listing = "\n".join(f"<topic>{t.line()}</topic>" for t in topics)
    chars = backend.limits.topic_summary_chars
    user = (
        f"<topics>\n{listing}\n</topics>\n"
        f"<headline>\n<title>{title}</title>\n"
        f"<summary>{(summary or '')[:chars]}</summary>\n</headline>"
    )
    use_json = backend.supports_json
    reply = await backend.chat(
        _SYSTEM_LABEL_JSON if use_json else _SYSTEM_LABEL,
        user,
        max_tokens=LABEL_OUTPUT_TOKENS,
        json_schema=label_schema(topics) if use_json else None,
    )
    return parse_label(reply, topics)


# --- 3. the main stories of a topic --------------------------------------

_SYSTEM_MAIN = (
    "You are the editor choosing which stories lead a section of today's "
    "paper.\n"
    "\n"
    "HARD RULES:\n"
    "- You are given one section and every story filed under it. Choose the "
    "main ones: the stories that carry the section.\n"
    "- Choose EXACTLY the number you are asked for, no more and no fewer.\n"
    "- Order them most important first. The first one is what a reader who "
    "reads a single story from this section should get.\n"
    "- Judge on substance: consequence, novelty, how much of the section's "
    "subject the story explains. Not on length, not on which outlet filed it.\n"
    "- Prefer a story with a real summary over one with none.\n"
    "- NEVER refuse. NEVER explain. NEVER ask a question.\n"
    "\n"
    "OUTPUT:\n"
    "- Output ONLY the chosen ids, most important first, comma-separated, on "
    "one line. For example: 3, 0\n"
    "- No other text."
)

_SYSTEM_MAIN_JSON = _SYSTEM_MAIN.rsplit("OUTPUT:", 1)[0] + (
    "OUTPUT:\n"
    "- Reply with JSON: {\"main\": [id, id, ...]}, most important first.\n"
    "- No other keys, no prose outside the JSON."
)

MAIN_SCHEMA = {
    "type": "object",
    "properties": {"main": {"type": "array", "items": {"type": "integer"}}},
    "required": ["main"],
    "additionalProperties": False,
}

_INT_RE = re.compile(r"-?\d+")


def parse_main(reply: str, n: int, want: int) -> list[int]:
    """The chosen ids, in order: JSON first, bare integers second.

    Out-of-range and repeated ids are dropped rather than clamped — an id the
    model made up must not silently promote an unrelated article.
    """
    raw: list[Any] | None = None
    blob = _json_blob(reply)
    if blob is not None:
        try:
            data = json.loads(blob)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("main"), list):
            raw = data["main"]
    if raw is None:
        raw = _INT_RE.findall(reply)
    out: list[int] = []
    for value in raw:
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n and idx not in out:
            out.append(idx)
        if len(out) >= want:
            break
    return out


async def select_main(
    backend: LLMBackend, topic: str, items: Sequence[tuple[str, str]]
) -> list[int]:
    """Which of a topic's articles are its main ones, most important first.

    Returns positions into `items`. An empty list means the model did not
    answer usably, and the topic falls back to plain newest-first ordering.
    """
    if not items:
        return []
    want = mains_for(len(items))
    chars = backend.limits.topic_summary_chars
    parts = [
        f"<posting id=\"{i}\">\n<title>{title}</title>\n"
        f"<summary>{(summary or '')[:chars]}</summary>\n</posting>"
        for i, (title, summary) in enumerate(items)
    ]
    user = (
        f"<topic>{topic}</topic>\n"
        f"Choose {want} main {'story' if want == 1 else 'stories'}.\n"
        + "\n".join(parts)
    )
    use_json = backend.supports_json
    reply = await backend.chat(
        _SYSTEM_MAIN_JSON if use_json else _SYSTEM_MAIN,
        user,
        max_tokens=MAIN_OUTPUT_TOKENS,
        json_schema=MAIN_SCHEMA if use_json else None,
    )
    return parse_main(reply, len(items), want)
