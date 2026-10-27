"""Headlines from RSS feeds. Free, no API keys."""
import logging

import feedparser

log = logging.getLogger(__name__)


def headlines(feeds, max_items=15):
    items = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
            for e in parsed.entries[:max_items]:
                title = (e.get("title") or "").strip()
                if title:
                    items.append(title)
        except Exception as exc:  # a dead feed should never stop the bot
            log.warning("feed failed %s: %s", url, exc)
    seen, unique = set(), []
    for t in items:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:max_items]
