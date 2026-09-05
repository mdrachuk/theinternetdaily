"""Compatibility: the fetchers moved to `tid.sources`, one module per kind.

Kept so `from tid.fetch import fetch_rss` still works for anyone who wrote
it. New code should import from `tid.sources`.
"""
from __future__ import annotations

from .sources import RawItem, fetch_hn, fetch_rss, fetch_wikipedia_events

__all__ = ["RawItem", "fetch_hn", "fetch_rss", "fetch_wikipedia_events"]
