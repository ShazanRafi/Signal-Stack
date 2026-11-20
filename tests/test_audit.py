import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from backend import audit, db
from backend.audit import assess_source, get_latest_audit, run_monthly_audit
from backend.db import connect, init_db
from backend.fetcher import FetchResult
from backend.llm import LLMError

NOW = datetime.now(timezone.utc)


def rss():
    items = "".join(
        f"<item><title>T{i}</title><link>https://x.test/{i}</link>"
        f"<pubDate>{format_datetime(NOW - timedelta(days=i))}</pubDate></item>" for i in range(3))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>{items}</channel></rss>'


class Fake:
    def __init__(self, dead=()):
        self.dead = set(dead)

    def fetch(self, url):
        if url in self.dead:
            return FetchResult(url=url, final_url=url, status=404, error="HTTP 404")
        return FetchResult(url=url, final_url=url, status=200, content_type="application/xml",
                           text=rss())

    def close(self):
        pass


def h(**kw):
    base = dict(type="rss", validation="valid", validation_note="", failing_streak=False,
                crawls=5, new_items=4, shared_total=0, shared_share=0.0, n_recent=0,
                rel_recent=0, n_prev=0, rel_prev=0, window=30)
    base.update(kw)
    return base


def flags(**kw):
    return [f["flag"] for f in assess_source(h(**kw))]


def test_dead_and_failing_sources():
    f = assess_source(h(validation="invalid", validation_note="HTTP 404"))
    assert f[0]["flag"] == "dead" and f[0]["action"] == "remove" and "404" in f[0]["reason"]
    assert "inactive" not in flags(validation="invalid", new_items=0)   
    assert flags(failing_streak=True) == ["failing"]


def test_not_enough_history_makes_no_other_judgement():
    assert flags(crawls=1, new_items=0) == ["insufficient_history"]


def test_inactive_applies_to_feeds_not_pages():
    assert flags(new_items=0) == ["inactive"]
    assert flags(type="web_page", new_items=0) == []


def test_redundancy_needs_enough_items():
    assert flags(shared_total=6, shared_share=0.9) == ["redundant"]
    assert flags(shared_total=3, shared_share=0.9) == []


def test_declining_relevance_and_low_yield():
    assert flags(n_prev=10, rel_prev=8, n_recent=10, rel_recent=2) == ["declining_relevance"]
    assert flags(n_prev=10, rel_prev=8, n_recent=10, rel_recent=7) == []
    assert flags(n_recent=12, rel_recent=0) == ["low_yield"]


def test_healthy_source_has_no_flags():
    assert flags() == []


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        return conn.execute("INSERT INTO companies (name, url, status) VALUES "
                            "('Acme', 'https://acme.test/', 'stack_built')").lastrowid


def add_source(cid, name, crawls, new_items):
    url = f"https://{name}.test/feed"
    with connect() as conn:
        sid = conn.execute(
            "INSERT INTO sources (company_id, category, type, target, url, reason, status, "
            "validation, dedupe_key) VALUES (?, 'market', 'rss', ?, ?, 'because', 'approved', "
            "'valid', ?)", (cid, url, url, f"rss|{url}")).lastrowid
        if crawls:
            run = conn.execute("INSERT INTO crawl_runs (company_id, run_type, started_at) "
                               "VALUES (?, 'test', ?)", (cid, NOW.isoformat())).lastrowid
            for i in range(crawls):
                conn.execute("INSERT INTO crawl_source_log (run_id, source_id, status, "
                             "items_seen, items_new) VALUES (?, ?, ?, 5, ?)",
                             (run, sid, "new_items" if new_items else "no_new",
                              new_items if i == 0 else 0))
    return sid, url


def section(md, title):
    return md.split(f"## {title}")[1].split("\n## ")[0]


def fake_discovery(company_id, fetcher=None, llm_fn=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO sources (company_id, category, type, target, url, reason, status, "
            "validation, origin, dedupe_key) VALUES (?, 'market', 'news_query', 'new topic', "
            "'https://news.test/new', 'covers a new topic', 'proposed', 'valid', 'llm', "
            "'news_query|new topic')", (company_id,))
    return SimpleNamespace(notes=[])


def test_audit_flags_updates_health_and_never_removes(cid, monkeypatch):
    monkeypatch.setattr(audit, "build_stack", fake_discovery)
    quiet, quiet_url = add_source(cid, "quiet", 3, 0)
    dead, dead_url = add_source(cid, "dead", 3, 2)
    good, good_url = add_source(cid, "good", 3, 3)
    fresh, fresh_url = add_source(cid, "fresh", 1, 1)

    out = run_monthly_audit(cid, fetcher=Fake(dead=[dead_url]))
    assert out.status == "ok" and out.summary["report_id"]
    md = get_latest_audit(cid)["markdown"]

    assert dead_url in section(md, "Recommended removals")
    assert quiet_url in section(md, "Needs review") and "no new records" in section(md, "Needs review")
    assert fresh_url in section(md, "Not enough history yet")
    assert "https://news.test/new" not in md and "new topic" in section(md, "Recommended additions")
    assert good_url not in md.split("## Method and limits")[0].replace("## Summary", "")

    s = out.summary
    assert (s["removals"], s["reviews"], s["insufficient_history"], s["healthy"],
            s["new_sources"], s["newly_dead"]) == (1, 1, 1, 1, 1, 1)
    with connect() as conn:
        row = conn.execute("SELECT status, validation FROM sources WHERE id = ?", (dead,)).fetchone()
    assert row["status"] == "approved" and row["validation"] == "invalid"   


def test_discovery_failure_is_partial_but_report_is_still_written(cid, monkeypatch):
    def broken(*a, **k):
        raise LLMError("quota")
    monkeypatch.setattr(audit, "build_stack", broken)
    add_source(cid, "good", 3, 3)
    out = run_monthly_audit(cid, fetcher=Fake())
    assert out.status == "partial" and "discovery" in out.error
    assert get_latest_audit(cid) is not None
    assert "discovery skipped" in get_latest_audit(cid)["markdown"]


def test_unknown_company_raises(cid):
    with pytest.raises(KeyError):
        run_monthly_audit(9999, fetcher=Fake(), discover=False)