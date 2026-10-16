"""Feed readers for every source type: Google News, YouTube channel, Reddit and generic RSS."""
from urllib.parse import quote_plus

import feedparser
import httpx

from canaldark.models import SourceItem, SourceSpec

USER_AGENT = "Mozilla/5.0 (CanalDark; +https://github.com/Andrelbmachado/Canal-Dark)"


def feed_url(spec: SourceSpec) -> str:
    if spec.tipo == "google_news":
        return f"https://news.google.com/rss/search?q={quote_plus(spec.busca)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    if spec.tipo == "youtube_canal":
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={spec.id}"
    if spec.tipo == "reddit":
        return f"https://www.reddit.com/r/{spec.sub}/.rss"
    return spec.url


def _is_video(url: str) -> bool:
    return any(h in url for h in ("youtube.com/watch", "youtu.be/", "youtube.com/shorts/", "v.redd.it/"))


async def read_source(spec: SourceSpec, client: httpx.AsyncClient) -> list[SourceItem]:
    resp = await client.get(feed_url(spec), headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    feed_title = feed.feed.get("title", "") or spec.tipo
    items = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        source = e.get("source", {}).get("title") if spec.tipo == "google_news" else None
        video = link if spec.tipo == "youtube_canal" or _is_video(link) else None
        items.append(SourceItem(
            url=link,
            title=e.get("title", ""),
            summary=e.get("summary", ""),
            published=e.get("published"),
            source_name=source or feed_title,
            video_url=video,
        ))
    return items
