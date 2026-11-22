import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from signalstack.infrastructure import db
from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.fetcher import FetchResult
from signalstack.domain.stack import (CompetitorList, LLMCompetitor, LLMSourceIdea, SourcePlan,
                           add_source, approve_all_valid, build_stack, get_stack,
                           profile_text, remove_competitor, set_source_status)

BODY = "We sell forged anvils to professional blacksmiths worldwide. " * 12
PAGE = f"<html><head><title>Page</title></head><body><p>{BODY}</p></body></html>"
GLOBEX_HOME = (
    '<html><head><title>Globex Corp</title>'
    '<link rel="alternate" type="application/rss+xml" href="/blog/feed.xml"></head><body>'
    f'<nav><a href="/pricing">Pricing</a><a href="/blog">Blog</a></nav><p>{BODY}</p></body></html>')


def rss(days_old):
    now = datetime.now(timezone.utc)
    items = "".join(
        f"<item><title>T{i}</title><link>https://x.test/{i}</link>"
        f"<pubDate>{format_datetime(now - timedelta(days=d))}</pubDate></item>"
        for i, d in enumerate(days_old))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>{items}</channel></rss>'


PAGES = {
    "https://globex.test/": (GLOBEX_HOME, "text/html"),
    "https://globex.test/blog/feed.xml": (rss([2, 9]), "application/xml"),
    "https://globex.test/pricing": (PAGE, "text/html"),
    "https://other.test/": ("<html><head><title>Totally Different Brand</title></head>"
                            "<body><p>hello</p></body></html>", "text/html"),
    "https://rbi.gov.in/news": (PAGE, "text/html"),
    "https://new.test/news": (PAGE, "text/html"),
}


def handler(url):
    if url.startswith("https://www.bing.com/news/search"):
        return PAGES_OK(url, rss([1, 2, 3, 4, 5]), "application/xml")
    if url.startswith("https://hn.algolia.com/"):
        hits = [] if "emptyquery" in url else [{"created_at_i": 1700000000}]
        return PAGES_OK(url, json.dumps({"hits": hits}), "application/json")
    if url in PAGES:
        return PAGES_OK(url, *PAGES[url])
    return None


def PAGES_OK(url, text, ctype):
    return FetchResult(url=url, final_url=url, status=200, content_type=ctype, text=text)


class Fake:
    def __init__(self, fn=handler):
        self.fn = fn

    def fetch(self, url):
        r = self.fn(url)
        return r if r is not None else FetchResult(url=url, final_url=url, status=404,
                                                   error="HTTP 404")

    def close(self):
        pass


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, schema, **kw):
        self.prompts.append(prompt)
        if schema is CompetitorList:
            return CompetitorList(competitors=[
                LLMCompetitor(name="Globex Corp", url="https://globex.test/",
                              kind="competitor", why_relevant="Sells to the same smiths"),
                LLMCompetitor(name="Mismatch Co", url="https://other.test/",
                              kind="substitute", why_relevant="Substitute workflow"),
                LLMCompetitor(name="Acme", url="https://acme.test/", kind="competitor",
                              why_relevant="that is us"),
                LLMCompetitor(name="SelfCo", url="https://www.acme.test/", kind="competitor",
                              why_relevant="our own site"),
                LLMCompetitor(name="globex corp", url="", kind="competitor", why_relevant="dup"),
            ])
        if schema is SourcePlan:
            return SourcePlan(region="IN", ideas=[
                LLMSourceIdea(category="customers", type="news_query",
                              target="anvil buyers complaints", reason="Smiths complain about quality"),
                LLMSourceIdea(category="customers", type="news_query",
                              target="Anvil  Buyers Complaints", reason="duplicate of the first"),
                LLMSourceIdea(category="market", type="hn_query", target="emptyquery",
                              reason="Tech audience"),
                LLMSourceIdea(category="market", type="web_page", target="https://rbi.gov.in/news",
                              reason="Regulator announcements affect exports"),
                LLMSourceIdea(category="other", type="news_query", target="x", reason="bad category"),
                LLMSourceIdea(category="market", type="carrier_pigeon", target="x", reason="bad type"),
                LLMSourceIdea(category="market", type="news_query", target="anvil export policy",
                              reason="Export rules hit our key market"),
            ])
        raise AssertionError(schema)


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        c = conn.execute("INSERT INTO companies (name, url, industry, summary, status) VALUES "
                         "('Acme', 'https://acme.test/', 'Metalwork', 'Acme sells anvils.', "
                         "'interviewed')").lastrowid
        conn.execute("INSERT INTO profile_facts (company_id, category, claim, status) "
                     "VALUES (?, 'products', 'Acme sells forged steel anvils', 'verified')", (c,))
        conn.execute("INSERT INTO profile_entities (company_id, name, kind, turn_id) "
                     "VALUES (?, 'Initech', 'competitor', 1)", (c,))
        conn.execute("INSERT INTO profile_entries (company_id, topic, content) VALUES "
                     "(?, 'internal_info', 'We keep a CRM in HubSpot')", (c,))
    return c


def test_full_build(cid):
    llm = FakeLLM()
    r = build_stack(cid, fetcher=Fake(), llm_fn=llm)
    assert "Initech" in llm.prompts[0] and "forged steel anvils" in llm.prompts[0]

    st = get_stack(cid)
    by = {c["name"]: c for c in st["competitors"]}
    assert set(by) == {"Globex Corp", "Mismatch Co", "Initech"}       
    assert by["Globex Corp"]["site_status"] == "ok"
    assert by["Mismatch Co"]["site_status"] == "mismatch" and by["Mismatch Co"]["url"] == ""
    assert by["Initech"]["named_by"] == "founder" and by["Initech"]["site_status"] == "none"

    srcs = st["sources"]
    ext = [s for s in srcs if s["type"] != "upload"]
    assert len(ext) == 9                     
    assert r.duplicates == 1 and r.rejected == 1 and r.proposed == 8
    rejected = [s for s in ext if s["status"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["type"] == "hn_query" and rejected[0]["validation_note"]
    assert all(s["reason"].strip() for s in srcs)                     
    assert next(s for s in ext if s["type"] == "rss")["tier"] == "primary"
    assert next(s for s in ext if "rbi.gov.in" in s["url"])["tier"] == "official"
    assert any("setmkt=en-IN" in s["url"] for s in ext if s["type"] == "news_query")

    internal = [s for s in srcs if s["type"] == "upload"]
    assert len(internal) == 3 and all("HubSpot" in s["reason"] for s in internal)
    assert all(s["status"] == "proposed" for s in internal)


def test_public_source_notes_are_not_labeled_as_founder_input(cid):
    with connect() as conn:
        conn.execute(
            "INSERT INTO profile_entries (company_id, topic, content) VALUES (?, ?, ?)",
            (cid, "business_lines",
             "[PUBLIC-SOURCE REVIEW ONLY; NOT FOUNDER INPUT] Publicly listed services."))
        prompt = profile_text(conn, cid)

    assert "What the founder told us:\n- [internal_info] We keep a CRM in HubSpot" in prompt
    assert "Public-source review (not founder input):" in prompt
    assert "[business_lines] [PUBLIC-SOURCE REVIEW ONLY; NOT FOUNDER INPUT]" in prompt


def test_rerun_does_not_duplicate_and_removed_competitor_stays_removed(cid):
    build_stack(cid, fetcher=Fake(), llm_fn=FakeLLM())
    n_src, n_comp = len(get_stack(cid)["sources"]), len(get_stack(cid)["competitors"])
    build_stack(cid, fetcher=Fake(), llm_fn=FakeLLM())
    assert (len(get_stack(cid)["sources"]), len(get_stack(cid)["competitors"])) == (n_src, n_comp)

    globex = next(c for c in get_stack(cid)["competitors"] if c["name"] == "Globex Corp")
    remove_competitor(globex["id"])
    build_stack(cid, fetcher=Fake(), llm_fn=FakeLLM())
    st = get_stack(cid)
    assert next(c for c in st["competitors"] if c["name"] == "Globex Corp")["status"] == "removed"
    assert all(s["status"] == "removed" for s in st["sources"] if s["competitor_id"] == globex["id"])


def test_build_stack_does_not_exceed_competitor_cap(cid):
    with connect() as conn:
        conn.executemany(
            "INSERT INTO competitors (company_id, name) VALUES (?, ?)",
            [(cid, f"Existing Competitor {i}") for i in range(1, 11)])

    result = build_stack(cid, fetcher=Fake(), llm_fn=FakeLLM())

    stack = get_stack(cid)
    active = [c for c in stack["competitors"] if c["status"] == "active"]
    assert len(active) == 10
    assert "Competitor cap reached (10)" in " ".join(result.notes)
    assert not any(c["name"] == "Mismatch Co" for c in active)


def test_approve_all_valid_only_approves_clean_external_sources(cid):
    build_stack(cid, fetcher=Fake(), llm_fn=FakeLLM())
    assert approve_all_valid(cid) == 8
    st = get_stack(cid)
    assert all(s["status"] == "proposed" for s in st["sources"] if s["type"] == "upload")
    rejected = next(s for s in st["sources"] if s["status"] == "rejected")
    assert set_source_status(rejected["id"], "approved") == 0         
    approved = next(s for s in st["sources"] if s["status"] == "approved")
    assert set_source_status(approved["id"], "removed") == 1


def test_add_source_validates_before_approving(cid):
    good = add_source(cid, "market", "web_page", "https://new.test/news", "Industry body updates",
                      fetcher=Fake())
    assert good["status"] == "approved"
    bad = add_source(cid, "market", "web_page", "https://missing.test/", "Hoping it exists",
                     fetcher=Fake())
    assert bad["status"] == "rejected" and "404" in bad["note"]
    with pytest.raises(ValueError):
        add_source(cid, "market", "web_page", "https://new.test/x", "  ", fetcher=Fake())