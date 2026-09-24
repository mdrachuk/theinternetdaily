"""A channel URL copied from its Videos tab names the channel, not the tab."""
from __future__ import annotations

import httpx

from tid.sources.youtube import resolve_channel_id

PAGE = ('<html><link rel="canonical" '
        'href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"></html>')


async def test_a_videos_tab_url_resolves_like_the_handle():
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, text=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        assert (await resolve_channel_id(c, "https://www.youtube.com/@x/videos")
                == "UCabcdefghijklmnopqrstuv")
    assert asked == ["/@x"]


async def test_a_channel_link_with_a_tab_needs_no_fetch():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(500))) as c:
        assert (await resolve_channel_id(
            c, "https://www.youtube.com/channel/UCOT2iLov0V7Re7ku_3UBtcQ/videos")
            == "UCOT2iLov0V7Re7ku_3UBtcQ")
