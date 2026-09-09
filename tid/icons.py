"""Source marks: the little favicon in every byline, and the medium glyphs.

The favicons come from Google's s2 service, but the *reader's* browser never
talks to it. The page points at `/icon/<domain>.png` on this origin, and this
module fetches once, caches to disk, and serves the file from then on. A
newspaper you read offline should not phone anyone home on every headline, and
after the first ingest it doesn't.

The medium glyphs (watch, listen) are inlined SVG rather than fetched: two
shapes are not worth a network dependency, and they have to render on an e-ink
browser with no connection at all.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

# A hostname, and nothing that could be read as anything else: no scheme, no
# path, no query, no traversal. This is what keeps a request for
# `/icon/../../etc/passwd.png` from ever reaching the filesystem, and what
# stops a crafted "domain" from adding parameters to the upstream URL.
_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})+$")

_S2 = "https://www.google.com/s2/favicons"

# A 1x1 transparent PNG, served when the upstream lookup fails. Better than a
# 404 per byline: the browser stops asking and the layout does not shift.
BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600"
    "00001f15c4890000000b49444154789c6360000200000500017a5e"
    "ab3f0000000049454e44ae426082"
)

# How long a miss stands before it is looked up again. A site with no favicon
# is rare and stable, so a day is plenty; what the day buys is that a blank
# cached during a network blip, or by an older tid that did not understand the
# reply, heals at the next ingest instead of standing forever.
BLANK_TTL = 24 * 60 * 60

# The formats a browser will paint in an <img>. Google hands back whatever the
# site itself publishes — mostly PNG, but JPEG for a good third of the web,
# and the odd GIF, WebP or .ico — so accepting only PNG left every byline from
# those sites blank. The URL keeps its `.png` suffix as a name; the bytes are
# served with the type they really are.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def media_type(blob: bytes) -> str | None:
    """The MIME type of an icon blob by its magic bytes, or None if it is
    not an image a browser can show (an HTML error page, say)."""
    for magic, kind in _MAGIC:
        if blob.startswith(magic):
            if kind == "image/webp" and blob[8:12] != b"WEBP":
                return None
            return kind
    return None


def is_fresh(path: Path) -> bool:
    """Whether a cached mark still stands: a real icon always does, a blank
    only until BLANK_TTL has passed since it was written."""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size != len(BLANK_PNG) or path.read_bytes() != BLANK_PNG:
        return True
    return time.time() - st.st_mtime < BLANK_TTL


def is_domain(value: str) -> bool:
    return bool(_DOMAIN_RE.match(value)) and len(value) <= 253


def domain_for(url: str) -> str:
    """The registrable-ish host of an article URL, for its favicon."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().removeprefix("www.")
    return host if is_domain(host) else ""


def icon_url(url: str) -> str:
    """The `<img src>` for an article's source mark, or "" for none."""
    domain = domain_for(url)
    return f"/icon/{domain}.png" if domain else ""


def icon_path(cache_dir: Path, domain: str) -> Path:
    return cache_dir / "icons" / f"{domain}.png"


async def fetch_icon(
    client: httpx.AsyncClient, cache_dir: Path, domain: str
) -> Path | None:
    """Cache one domain's favicon to disk. Returns the path, or None.

    The only host contacted is Google's: `domain` is a query parameter to it,
    never something we connect to, so an odd value costs a blank icon rather
    than a request to somewhere unexpected.
    """
    if not is_domain(domain):
        return None
    out = icon_path(cache_dir, domain)
    if is_fresh(out):
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = await client.get(_S2, params={"sz": "64", "domain": domain},
                             timeout=10)
        blob = r.content if r.status_code == 200 else b""
    except httpx.HTTPError:
        blob = b""
    # Negative results are cached too, as the blank: a domain with no favicon
    # would otherwise be re-fetched on every page view, rather than once a day.
    tmp = out.with_suffix(".png.tmp")
    tmp.write_bytes(blob if media_type(blob) else BLANK_PNG)
    tmp.replace(out)
    return out


# --- medium glyphs --------------------------------------------------------

# Drawn to sit on the baseline next to 9px uppercase type, in the same weight
# as the favicon beside them. `currentColor` so they inherit the byline colour.
_GLYPHS = {
    # Ruled lines: the "this is text" mark. Only the filter row uses it — a
    # byline on a written article says `read` by having no glyph at all, which
    # is what keeps the glyphs meaning "not the usual thing".
    "read": (
        '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
        '<path fill="currentColor" d="M2 3h12v1.6H2V3zm1.6 3.6h8.8v1.6H3.6V6.6z'
        'M2 10.2h12v1.6H2v-1.6zm1.6 3.6h8.8v1.6H3.6v-1.6z"/></svg>'
    ),
    "watch": (
        '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
        '<path fill="currentColor" d="M8 1a7 7 0 100 14A7 7 0 008 1zm-1.2 4.2'
        'l4.2 2.5a.35.35 0 010 .6l-4.2 2.5a.35.35 0 01-.55-.3V5.5a.35.35 0 '
        '01.55-.3z"/></svg>'
    ),
    "listen": (
        '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
        '<path fill="currentColor" d="M8 1.5A5.5 5.5 0 002.5 7v1.2h1.2A1.8 '
        '1.8 0 015.5 10v2a1.8 1.8 0 01-1.8 1.8A2.7 2.7 0 011 11.1V7a7 7 0 '
        '0114 0v4.1a2.7 2.7 0 01-2.7 2.7A1.8 1.8 0 0110.5 12v-2a1.8 1.8 0 '
        '011.8-1.8h1.2V7A5.5 5.5 0 008 1.5z"/></svg>'
    ),
}


def glyph(medium: str) -> str:
    """Inline SVG for a byline's medium. `read` deliberately gets nothing."""
    return "" if medium == "read" else _GLYPHS.get(medium, "")


def filter_glyph(medium: str) -> str:
    """Inline SVG for a filter chip, where `read` does need a mark of its own:
    the chip carries no text, so without one it is an empty box."""
    return _GLYPHS.get(medium, "")
