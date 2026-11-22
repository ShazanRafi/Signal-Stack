import calendar
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import feedparser
from bs4 import BeautifulSoup

from signalstack.domain.verify import normalize

MAX_TEXT = 8000
MAX_ITEMS_PER_FEED = 30
MIN_COMMENT_CHARS = 40
MAX_DIFF_LINES = 40

_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b", re.I)
_VOLATILE = re.compile(r"\bago\b|copyright|\u00a9|all rights reserved|\b\d{1,2}:\d{2}\b", re.I)


def strip_html(s: str) -> str:
    if not s:
        return ""
    return " ".join(BeautifulSoup(s, "html.parser").get_text(" ", strip=True).split())


def text_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(normalize(p) for p in parts).encode("utf-8")).hexdigest()


def _published(entry, now: datetime) -> str:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return ""
    dt = datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    if dt > now + timedelta(days=1):     
        return ""
    return dt.isoformat()

def _unwrap_bing(link: str) -> str:
    """Bing news links are redirects; the real publisher URL sits in the url= parameter."""
    p = urlparse(link)
    if p.netloc.endswith("bing.com") and p.path.startswith("/news/apiclick"):
        real = parse_qs(p.query).get("url", [""])[0]
        if real.startswith(("http://", "https://")):
            return real
    return link

def parse_feed(text: str, news: bool = False, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    feed = feedparser.parse(text.encode("utf-8"))
    feed_title = (feed.feed.get("title") or "").strip()
    items = []
    for e in feed.entries[:MAX_ITEMS_PER_FEED]:
        title = strip_html(e.get("title", ""))
        link = (e.get("link") or "").strip()
        if news:
            link = _unwrap_bing(link)
        if not title or not link:
            continue
        if news:
            publisher = (((e.get("source") or {}).get("title") or "")
                         or str(e.get("news_source") or "")).strip()
            if not publisher:
                publisher = urlparse(link).netloc.removeprefix("www.")
        else:
            publisher = feed_title
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3].strip()
        raw = e["content"][0].get("value", "") if e.get("content") else ""
        body = strip_html(raw or e.get("summary", "") or e.get("description", ""))
        echo = normalize(body).startswith(normalize(title)) and len(body) < len(title) + 40
        text_ = title if (not body or echo) else f"{title}\n{body}"
        items.append({"title": title, "url": link, "text": text_[:MAX_TEXT],
                      "published_at": _published(e, now), "publisher": publisher,
                      "kind": "article"})
    return items


def parse_hn(text: str, comments: bool = False) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = []
    for h in (data.get("hits") if isinstance(data, dict) else None) or []:
        oid = str(h.get("objectID") or "")
        if not oid:
            continue
        discussion = f"https://news.ycombinator.com/item?id={oid}"
        ts = h.get("created_at_i")
        published = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else ""
        if comments:
            body = strip_html(h.get("comment_text") or "")
            if len(body) < MIN_COMMENT_CHARS:
                continue
            items.append({"title": "Comment on: " + (h.get("story_title") or "a Hacker News story"),
                          "url": discussion, "text": body[:MAX_TEXT], "published_at": published,
                          "publisher": "Hacker News comment", "kind": "comment"})
        else:
            title = (h.get("title") or "").strip()
            if not title:
                continue
            body = strip_html(h.get("story_text") or "")
            items.append({"title": title, "url": (h.get("url") or "").strip() or discussion,
                          "text": (f"{title}\n{body}" if body else title)[:MAX_TEXT],
                          "published_at": published, "publisher": "Hacker News",
                          "kind": "story"})
    return items


def _keep(line: str) -> bool:
    return bool(re.search(r"\w", line)) and not _VOLATILE.search(line) and not _DATE.search(line)


def page_diff(old: str, new: str) -> tuple[list[str], list[str]]:
    old_l = [l.strip() for l in old.splitlines() if l.strip()]
    new_l = [l.strip() for l in new.splitlines() if l.strip()]
    old_s, new_s = set(old_l), set(new_l)
    added = list(dict.fromkeys(l for l in new_l if l not in old_s and _keep(l)))
    removed = list(dict.fromkeys(l for l in old_l if l not in new_s and _keep(l)))
    return added, removed


def format_change(added: list[str], removed: list[str]) -> str:
    parts = []
    if added:
        parts.append("ADDED:\n" + "\n".join(f"- {l}" for l in added[:MAX_DIFF_LINES]))
    if removed:
        parts.append("REMOVED:\n" + "\n".join(f"- {l}" for l in removed[:MAX_DIFF_LINES]))
    return "\n".join(parts)[:MAX_TEXT]