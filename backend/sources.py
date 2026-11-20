import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup

from backend.extract import extract_page, normalize_url, same_site

CATEGORIES = ("customers", "competitors", "market", "internal")
SOURCE_TYPES = ("rss", "news_query", "hn_query", "web_page", "upload")
REGIONS = {"IN", "US", "GB", "AU", "CA", "SG", "AE", "DE", "FR", "NZ", "IE", "ZA"}
STALE_DAYS = 180
SOFT_FAIL = {429, 503}          
OFFICIAL_SUFFIXES = (".gov", ".nic.in", ".edu", ".ac.in", ".europa.eu")
BRAND_STOP = {"the", "inc", "ltd", "llc", "pvt", "private", "limited", "co", "corp",
              "company", "group", "and"}


def canonical_url(raw: str) -> str:
    p = urlparse(normalize_url(raw))
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(p.query)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "ref"}]
    return urlunparse((p.scheme, host, p.path.rstrip("/") or "/", "", urlencode(query), ""))


def dedupe_key(type_: str, target: str) -> str:
    if type_ in ("news_query", "hn_query"):
        return f"{type_}|{' '.join(target.lower().split())}"
    if type_ == "upload":
        return f"upload|{target}"
    return f"{type_}|{canonical_url(target)}"


def fetch_url(type_: str, target: str, region: str = "US") -> str:
    """The exact URL the crawler will request for a source."""
    if type_ == "news_query":
        cc = region if region in REGIONS else "US"
        return "https://www.bing.com/news/search?" + urlencode(
            {"q": target, "format": "rss", "setmkt": f"en-{cc}"})
    if type_ == "hn_query":
        return "https://hn.algolia.com/api/v1/search_by_date?" + urlencode(
            {"query": target, "tags": "story", "hitsPerPage": 20})
    return normalize_url(target)


def source_tier(type_: str, url: str, own_sites: list[str]) -> str:
    """Rule-based source reliability class. A heuristic, not a truth score."""
    if type_ == "news_query":
        return "news"
    if type_ == "hn_query":
        return "community"
    if type_ == "upload":
        return "internal"
    if any(same_site(url, s) for s in own_sites if s):
        return "primary"
    host = urlparse(url).hostname or ""
    if host.endswith(OFFICIAL_SUFFIXES) or ".gov." in host:
        return "official"
    return "unrated"


@dataclass
class Validation:
    status: str            
    note: str
    items: int = 0
    last_item_at: str = ""


def _latest(entries) -> datetime | None:
    stamps = []
    for e in entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            stamps.append(calendar.timegm(t))
    return datetime.fromtimestamp(max(stamps), tz=timezone.utc) if stamps else None


def _check_feed(text: str, type_: str) -> Validation:
    feed = feedparser.parse(text.encode("utf-8"))    
    n = len(feed.entries)
    if n == 0:
        return Validation("invalid", "no feed items found (not a feed, or the query returned nothing)")
    latest = _latest(feed.entries)
    last = latest.isoformat() if latest else ""
    if type_ == "rss":
        if latest is None:
            return Validation("warning", f"{n} items but none carry dates", n, last)
        age = (datetime.now(timezone.utc) - latest).days
        if age > STALE_DAYS:
            return Validation("warning", f"feed is stale: newest item is {age} days old", n, last)
    if type_ == "news_query" and n < 3:
        return Validation("warning", f"only {n} result(s) for this query", n, last)
    return Validation("valid", f"{n} items", n, last)


def _check_hn(text: str) -> Validation:
    try:
        data = json.loads(text)
    except ValueError:
        return Validation("invalid", "response was not valid JSON")
    hits = data.get("hits") if isinstance(data, dict) else None
    if not hits:
        return Validation("invalid", "no matching stories")
    ts = max(int(h.get("created_at_i") or 0) for h in hits)
    last = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
    return Validation("valid", f"{len(hits)} stories", len(hits), last)


def validate_source(fetcher, type_: str, url: str) -> Validation:
    res = fetcher.fetch(url)
    if res.blocked_by_robots:
        return Validation("invalid", "blocked by robots.txt; will not be monitored")
    if res.status in SOFT_FAIL:
        return Validation("warning", f"temporarily unavailable (HTTP {res.status}); recheck later")
    if not res.ok:
        return Validation("invalid", res.error or "fetch failed")
    if type_ in ("rss", "news_query"):
        return _check_feed(res.text, type_)
    if type_ == "hn_query":
        return _check_hn(res.text)
    page = extract_page(res.text, res.final_url)
    if page.low_content:
        return Validation("warning", "very little text (JavaScript-rendered?); "
                                     "change detection may be unreliable")
    return Validation("valid", f"{len(page.text)} characters of text")


@dataclass
class SiteCheck:
    status: str            
    note: str
    html: str = ""
    final_url: str = ""
    links: list = field(default_factory=list)


def brand_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower())
            if t not in BRAND_STOP and len(t) >= 2]


def check_competitor_site(fetcher, name: str, url: str) -> SiteCheck:
    """Fetch the homepage and check it plausibly belongs to `name` (catches wrong URLs)."""
    if not url.strip():
        return SiteCheck("none", "no website known; monitored through news only")
    res = fetcher.fetch(url)
    if res.blocked_by_robots:
        return SiteCheck("blocked", "site blocked by robots.txt; not monitored directly")
    if not res.ok:
        return SiteCheck("unreachable", res.error or "fetch failed")
    page = extract_page(res.text, res.final_url)
    toks = brand_tokens(name)
    hay = " ".join([page.title, page.description, page.text[:3000],
                    urlparse(res.final_url).netloc]).lower()
    if toks and toks[0] not in hay:
        return SiteCheck("mismatch", f"page never mentions '{toks[0]}'; URL ignored")
    return SiteCheck("ok", "homepage fetched", res.text, res.final_url, page.links)


def find_feed_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        typ = (tag.get("type") or "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ):
            out.append(urljoin(base_url, tag["href"]))
    return out[:3]


PRICE_WORDS = {"pricing": 3, "plans": 2, "fees": 2, "charges": 1, "rates": 1}
UPDATE_WORDS = {"newsroom": 3, "changelog": 3, "press": 2, "news": 2, "blog": 2,
                "announcements": 2, "releases": 2, "updates": 1}


def _best(links, base_url: str, words: dict) -> str | None:
    best = None
    for url, anchor in links:
        if not same_site(url, base_url):
            continue
        path = urlparse(url).path.lower()
        if path in ("", "/") or path.endswith((".pdf", ".jpg", ".png", ".zip", ".xml")):
            continue
        score = sum(w for k, w in words.items() if k in f"{path} {anchor.lower()}")
        score -= path.strip("/").count("/") * 0.5
        if score > 0 and (best is None or score > best[0]):
            best = (score, url)
    return best[1] if best else None


def pick_watch_pages(links, base_url: str, has_feed: bool) -> list[tuple[str, str]]:
    """Pages worth change-detection: a pricing page, and an updates page if there is no feed."""
    out = []
    price = _best(links, base_url, PRICE_WORDS)
    if price:
        out.append(("pricing", price))
    if not has_feed:
        upd = _best(links, base_url, UPDATE_WORDS)
        if upd and upd != price:
            out.append(("updates", upd))
    return out