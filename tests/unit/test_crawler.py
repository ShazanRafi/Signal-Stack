import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from signalstack.infrastructure import db
from signalstack.services.crawler import crawl_company, get_records, get_run_log, list_runs
from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.fetcher import FetchResult

NOW = datetime.now(timezone.utc)
BODY = "We sell forged anvils to professional blacksmiths worldwide. " * 12


def rss(items):
    out = ""
    for it in items:
        src = f'<source url="https://pub.test">{it["source"]}</source>' if it.get("source") else ""
        out += (f"<item><title>{it['title']}</title><link>{it['link']}</link>"
                f"<description>{it.get('desc', '')}</description>"
                f"<pubDate>{format_datetime(NOW - timedelta(days=it['days']))}</pubDate>{src}</item>")
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>{out}</channel></rss>'


def ok(url, text, ctype="application/xml"):
    return FetchResult(url=url, final_url=url, status=200, content_type=ctype, text=text)


class Fake:
    def __init__(self, routes):
        self.routes = routes

    def fetch(self, url):
        r = self.routes.get(url)
        if callable(r):
            r = r(url)
        if isinstance(r, Exception):
            raise r
        return r or FetchResult(url=url, final_url=url, status=404, error="HTTP 404")

    def close(self):
        pass


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        return conn.execute("INSERT INTO companies (name, url, status) VALUES "
                            "('Acme', 'https://acme.test/', 'stack_built')").lastrowid


def add(cid, type_, url, category="market", status="approved"):
    with connect() as conn:
        return conn.execute(
            "INSERT INTO sources (company_id, category, type, target, url, reason, status, "
            "dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
            (cid, category, type_, url, url, "test reason", status, f"{type_}|{url}")).lastrowid


def statuses(run_id):
    return [l["status"] for l in get_run_log(run_id)]


def test_feed_ingest_dedupe_and_age_filter(cid):
    feed = "https://blog.test/feed"
    add(cid, "rss", feed)
    items = [dict(title="Launch of Product X", link="https://blog.test/x", days=2,
                  desc="We launched product X today for all customers."),
             dict(title="Older post", link="https://blog.test/o", days=5),
             dict(title="Ancient post", link="https://blog.test/a", days=200)]
    f = Fake({feed: ok(feed, rss(items))})

    r1 = crawl_company(cid, fetcher=f)
    assert r1.status == "ok" and r1.new_records == 2
    recs = get_records(cid)
    assert {x["title"] for x in recs} == {"Launch of Product X", "Older post"}
    x = next(r for r in recs if r["title"] == "Launch of Product X")
    assert x["published_at"] and "launched product X" in x["text"] and x["category"] == "market"
    assert get_run_log(r1.run_id)[0]["items_old"] == 1

    r2 = crawl_company(cid, fetcher=f)
    assert r2.new_records == 0 and r2.duplicates == 2 and len(get_records(cid)) == 2
    assert len(list_runs(cid)) == 2


def test_same_story_from_two_queries_is_one_record_with_two_sightings(cid):
    a = "https://news.google.com/rss/search?q=a"
    b = "https://news.google.com/rss/search?q=b"
    add(cid, "news_query", a)
    add(cid, "news_query", b)
    title = "Globex cuts prices - Pub News"
    f = Fake({
        a: ok(a, rss([dict(title=title, link="https://news.google.com/rss/articles/AAA",
                           days=1, source="Pub News")])),
        b: ok(b, rss([dict(title=title, link="https://news.google.com/rss/articles/BBB",
                           days=1, source="Pub News")])),
    })
    r = crawl_company(cid, fetcher=f)
    assert r.new_records == 1 and r.duplicates == 1
    rec = get_records(cid)[0]
    assert rec["title"] == "Globex cuts prices" and rec["publisher"] == "Pub News"
    assert rec["n_sightings"] == 2


def page(price, stamp):
    return (f"<html><head><title>Pricing</title></head><body><p>{BODY}</p>"
            f"<p>Standard plan: Rs {price} per month</p><p>Last updated {stamp}</p></body></html>")


def test_page_change_detection_baseline_noise_and_real_change(cid):
    url = "https://globex.test/pricing"
    add(cid, "web_page", url, "competitors")
    routes = {url: ok(url, page(499, "10:15"), "text/html")}
    f = Fake(routes)

    r1 = crawl_company(cid, fetcher=f)
    assert statuses(r1.run_id) == ["baseline"] and r1.new_records == 0

    r2 = crawl_company(cid, fetcher=f)
    assert statuses(r2.run_id) == ["unchanged"]

    routes[url] = ok(url, page(499, "12:45"), "text/html")     
    r3 = crawl_company(cid, fetcher=f)
    assert statuses(r3.run_id) == ["unchanged"] and r3.new_records == 0

    routes[url] = ok(url, page(599, "13:00"), "text/html")    
    r4 = crawl_company(cid, fetcher=f)
    assert statuses(r4.run_id) == ["changed"] and r4.new_records == 1
    rec = get_records(cid)[0]
    assert rec["kind"] == "page_change" and rec["url"] == url
    assert "Rs 599" in rec["text"] and "Rs 499" in rec["text"]
    assert rec["text"].index("ADDED") < rec["text"].index("REMOVED")

    r5 = crawl_company(cid, fetcher=f)                          
    assert statuses(r5.run_id) == ["unchanged"] and len(get_records(cid)) == 1


def test_thin_page_is_skipped_not_reported_as_change(cid):
    url = "https://globex.test/pricing"
    add(cid, "web_page", url, "competitors")
    routes = {url: ok(url, page(499, "10:15"), "text/html")}
    f = Fake(routes)
    crawl_company(cid, fetcher=f)
    routes[url] = ok(url, "<html><body><div id='root'></div></body></html>", "text/html")
    r = crawl_company(cid, fetcher=f)
    assert statuses(r.run_id) == ["error"] and r.status == "failed"
    assert get_records(cid) == []


def test_failures_are_isolated_and_logged(cid):
    good, dead, blocked, boom = (f"https://x.test/{n}" for n in ("good", "dead", "blocked", "boom"))
    for u in (good, dead, blocked, boom):
        add(cid, "rss", u)
    f = Fake({
        good: ok(good, rss([dict(title="Fresh news", link="https://x.test/n1", days=1)])),
        blocked: FetchResult(url=blocked, final_url=blocked, blocked_by_robots=True,
                             error="blocked by robots.txt"),
        boom: RuntimeError("parser exploded"),
    })
    r = crawl_company(cid, fetcher=f)
    assert r.status == "partial" and r.errors == 3 and r.new_records == 1
    assert sorted(statuses(r.run_id)) == ["blocked", "error", "error", "new_items"]
    notes = " ".join(l["note"] for l in get_run_log(r.run_id))
    assert "parser exploded" in notes and "404" in notes and "robots" in notes


def test_all_sources_failing_marks_run_failed(cid):
    add(cid, "rss", "https://x.test/dead")
    r = crawl_company(cid, fetcher=Fake({}))
    assert r.status == "failed"
    assert list_runs(cid)[0]["status"] == "failed"


def test_no_approved_sources_is_a_logged_failure_and_uploads_are_ignored(cid):
    add(cid, "upload", "crm_export")                             
    add(cid, "rss", "https://x.test/feed", status="proposed")     
    r = crawl_company(cid, fetcher=Fake({}))
    assert r.status == "failed" and "no approved" in r.error
    assert list_runs(cid)[0]["error"] == r.error


def test_hacker_news_stories_and_customer_comments(cid):
    story_url = "https://hn.algolia.com/api/v1/search_by_date?query=anvil&tags=story&hitsPerPage=20"
    ts = int(NOW.timestamp()) - 3600

    def hn(url):
        if "tags=comment" in url:
            hits = [{"objectID": "2", "created_at_i": ts, "story_title": "Ask HN: anvils?",
                     "comment_text": "<p>I returned my anvil because the quality was awful "
                                     "and support never answered.</p>"},
                    {"objectID": "3", "created_at_i": ts, "story_title": "x", "comment_text": "lol"}]
        else:
            hits = [{"objectID": "1", "created_at_i": ts, "title": "Ask HN: anvils?",
                     "url": None, "story_text": "Where do I buy a good one?"}]
        return ok(url, json.dumps({"hits": hits}), "application/json")

    add(cid, "hn_query", story_url, "customers")
    r = crawl_company(cid, fetcher=Fake({story_url: hn, story_url.replace("tags=story", "tags=comment"): hn}))
    assert r.new_records == 2
    by_kind = {x["kind"]: x for x in get_records(cid)}
    assert by_kind["story"]["url"] == "https://news.ycombinator.com/item?id=1"
    assert "anvil because the quality was awful" in by_kind["comment"]["text"]
    assert "<p>" not in by_kind["comment"]["text"]