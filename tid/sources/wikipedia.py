"""`kind = "wikipedia_events"`: one item per day of Portal:Current_events.

Not a feed and not configured as a regular source in the shipped
sources.toml — world news is a PDF cover decoration — but it is a kind the
gather understands, so it lives beside the others.
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td
from typing import Any

import httpx

from .base import Link, RawItem


async def fetch_wikipedia_events(
    source_name: str = "World news",
    days_back: int = 1,
) -> list[RawItem]:
    """One item per day of Wikipedia's Portal:Current_events.

    days_back=1 → just today. Increase to backfill recent days. Needs no
    network of its own: the URLs are derived from the date.
    """
    from ..wiki import current_events_title, current_events_url

    today = _date.today()
    return [
        RawItem(
            source=source_name,
            url=current_events_url(today - _td(days=delta)),
            title=current_events_title(today - _td(days=delta)),
            kind="wikipedia_events",
            surfaced=(today - _td(days=delta)).isoformat(),
        )
        for delta in range(days_back)
    ]


class WikipediaEventsSource:
    kind = "wikipedia_events"
    label = "Wikipedia current events"

    async def fetch(
        self, client: httpx.AsyncClient, src: dict[str, Any]
    ) -> list[RawItem]:
        return await fetch_wikipedia_events(
            source_name=src["name"], days_back=int(src.get("days_back", 1)),
        )

    def links(self, url: str, source: str, extra: dict[str, Any]) -> list[Link]:
        return [Link(label=source, href=url, role="article")]
