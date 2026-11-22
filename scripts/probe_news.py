from signalstack.infrastructure.fetcher import PoliteFetcher
from signalstack.infrastructure.parsers import parse_feed

CANDIDATES = {
    "bing_news_rss": "https://www.bing.com/news/search?q=IDP+Education&format=rss",
    "yahoo_news_rss": "https://news.search.yahoo.com/rss?p=IDP+Education",
}

f = PoliteFetcher()
for name, url in CANDIDATES.items():
    r = f.fetch(url)
    items = parse_feed(r.text, news=True) if r.ok else []
    print(name, "| status", r.status, "| robots_blocked", r.blocked_by_robots,
          "| type", r.content_type, "| error", r.error, "| items", len(items))
    for it in items[:3]:
        print("   ", it["published_at"][:10], it["publisher"], "|", it["title"][:70])