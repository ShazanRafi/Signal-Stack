import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from signalstack.infrastructure import sources as S
from signalstack.infrastructure.fetcher import FetchResult

BODY = "We sell forged anvils to professional blacksmiths worldwide. " * 12


def ok(url, text, ctype="text/html"):
    return FetchResult(url=url, final_url=url, status=200, content_type=ctype, text=text)


def fail(url="u", status=None, blocked=False):
    return FetchResult(url=url, final_url=url, status=status, blocked_by_robots=blocked,
                       error="blocked by robots.txt" if blocked else f"HTTP {status}")


class Fake:
    def __init__(self, result):
        self.result = result

    def fetch(self, url):
        return self.result


def rss(days_old):
    now = datetime.now(timezone.utc)
    items = "".join(
        f"<item><title>T{i}</title><link>https://x.test/{i}</link>"
        f"<pubDate>{format_datetime(now - timedelta(days=d))}</pubDate></item>"
        for i, d in enumerate(days_old))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>{items}</channel></rss>'


def test_canonical_url_ignores_tracking_www_slash_fragment():
    assert S.canonical_url("https://www.Acme.com/blog/?utm_source=x&id=2#top") == \
        "https://acme.com/blog?id=2"
    assert S.canonical_url("acme.com") == "https://acme.com/"


def test_dedupe_key_normalises_queries():
    assert S.dedupe_key("news_query", "Foo  Bar") == S.dedupe_key("news_query", "foo bar")


def test_fetch_url_region_and_defaults():
    assert "setmkt=en-IN" in S.fetch_url("news_query", "x", "IN")
    assert "setmkt=en-US" in S.fetch_url("news_query", "x", "ZZ")
    assert S.fetch_url("hn_query", "x").startswith("https://hn.algolia.com/api/v1/search_by_date")


def test_fresh_rss_is_valid():
    v = S.validate_source(Fake(ok("u", rss([1, 5, 9]), "application/xml")), "rss", "u")
    assert v.status == "valid" and v.items == 3 and v.last_item_at


def test_stale_rss_warns():
    v = S.validate_source(Fake(ok("u", rss([400, 500]), "application/xml")), "rss", "u")
    assert v.status == "warning" and "stale" in v.note


def test_html_instead_of_feed_is_invalid():
    v = S.validate_source(Fake(ok("u", "<html><body>hi</body></html>")), "rss", "u")
    assert v.status == "invalid"


def test_empty_news_query_invalid_and_small_one_warns():
    assert S.validate_source(Fake(ok("u", rss([]), "application/xml")),
                             "news_query", "u").status == "invalid"
    assert S.validate_source(Fake(ok("u", rss([1]), "application/xml")),
                             "news_query", "u").status == "warning"


def test_failures_are_classified():
    assert S.validate_source(Fake(fail(blocked=True)), "web_page", "u").status == "invalid"
    assert "robots" in S.validate_source(Fake(fail(blocked=True)), "web_page", "u").note
    assert S.validate_source(Fake(fail(status=429)), "rss", "u").status == "warning"
    assert S.validate_source(Fake(fail(status=404)), "rss", "u").status == "invalid"


def test_hn_validation():
    good = json.dumps({"hits": [{"created_at_i": 1700000000}]})
    assert S.validate_source(Fake(ok("u", good, "application/json")), "hn_query", "u").status == "valid"
    assert S.validate_source(Fake(ok("u", '{"hits": []}', "application/json")),
                             "hn_query", "u").status == "invalid"
    assert S.validate_source(Fake(ok("u", "nope", "application/json")),
                             "hn_query", "u").status == "invalid"


def test_page_validation_thin_vs_full():
    thin = "<html><body><div id='root'></div></body></html>"
    full = f"<html><body><p>{BODY}</p></body></html>"
    assert S.validate_source(Fake(ok("u", thin)), "web_page", "u").status == "warning"
    assert S.validate_source(Fake(ok("u", full)), "web_page", "u").status == "valid"


def test_competitor_site_checks():
    html = (f"<html><head><title>Globex Corp</title></head><body><nav>"
            f"<a href='/pricing'>Pricing</a></nav><p>{BODY}</p></body></html>")
    s = S.check_competitor_site(Fake(ok("https://globex.test/", html)), "Globex Corp",
                                "https://globex.test/")
    assert s.status == "ok" and ("https://globex.test/pricing", "Pricing") in s.links
    other = "<html><head><title>Totally Different Brand</title></head><body><p>hi</p></body></html>"
    assert S.check_competitor_site(Fake(ok("https://other.test/", other)), "Mismatch Co",
                                   "https://other.test/").status == "mismatch"
    assert S.check_competitor_site(Fake(fail(blocked=True)), "Globex", "https://g.test/").status == "blocked"
    assert S.check_competitor_site(Fake(None), "Globex", "  ").status == "none"


def test_feed_link_discovery():
    html = ('<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            '<link rel="stylesheet" href="/a.css"></head></html>')
    assert S.find_feed_links(html, "https://g.test/") == ["https://g.test/feed.xml"]


def test_watch_pages_depend_on_feed():
    links = [("https://g.test/pricing", "Pricing"), ("https://g.test/blog", "Blog"),
             ("https://other.com/pricing", "Pricing")]
    assert S.pick_watch_pages(links, "https://g.test/", False) == [
        ("pricing", "https://g.test/pricing"), ("updates", "https://g.test/blog")]
    assert S.pick_watch_pages(links, "https://g.test/", True) == [
        ("pricing", "https://g.test/pricing")]


def test_tiers():
    assert S.source_tier("news_query", "u", []) == "news"
    assert S.source_tier("web_page", "https://rbi.gov.in/news", []) == "official"
    assert S.source_tier("rss", "https://globex.test/feed", ["https://globex.test/"]) == "primary"
    assert S.source_tier("web_page", "https://random.test/x", []) == "unrated"