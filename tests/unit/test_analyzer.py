import re
from datetime import datetime, timedelta, timezone

import pytest

from signalstack.infrastructure import config, db
from signalstack.services.analyzer import (BatchAnalysis, LLMItem, analyze_company, get_signals,
                              reset_signals)
from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.llm import LLMError, LLMQuotaError

NOW = datetime.now(timezone.utc)
TEXT = "Export rules for forged steel anvils will tighten next quarter for blacksmiths in Europe."
SNIPPET = "rules for forged steel anvils will tighten"


class FakeLLM:
    def __init__(self, overrides=None, fail=None, extra=(), skip=()):
        self.overrides, self.fail, self.extra, self.skip = overrides or {}, fail, extra, skip
        self.calls, self.batches = 0, []

    def __call__(self, prompt, schema, **kw):
        self.calls += 1
        if self.fail:
            raise self.fail
        ids = [int(x) for x in re.findall(r"=== RECORD (\d+) ===", prompt)]
        self.batches.append(ids)
        items = []
        for i in [*ids, *self.extra]:
            if i in self.skip:
                continue
            d = dict(record_id=i, relevant=True, what_happened="Something happened",
                     affected_area="regulation", classification="threat", urgency="high",
                     impact=4, relevance=4, why_it_matters="It matters", suggested_action="Act",
                     evidence_snippet="", customer_quote="", theme="")
            d.update(self.overrides.get(i, {}))
            items.append(LLMItem(**d))
        return BatchAnalysis(items=items)


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        return conn.execute(
            "INSERT INTO companies (name, url, industry, summary, status) VALUES ('Acme', "
            "'https://acme.test/', 'Metalwork', 'Acme sells forged steel anvils; each anvil "
            "is made for blacksmiths.', 'crawled')").lastrowid


def _src(conn, cid, tier, source_type="rss", category="market"):
    row = conn.execute("SELECT id FROM sources WHERE company_id=? AND tier=? AND type=? "
                       "AND category=?", (cid, tier, source_type, category)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO sources (company_id, category, type, target, url, reason, tier, status, "
        "dedupe_key) VALUES (?, ?, ?, ?, ?, 'r', ?, 'approved', ?)",
        (cid, category, source_type, tier, tier, tier, f"{source_type}|{category}|{tier}")).lastrowid


def add_rec(cid, title, text=TEXT, tier="news", kind="article", days=1, sightings=1,
            category="market", source_type="rss"):
    with connect() as conn:
        sid = _src(conn, cid, tier, source_type, category)
        pub = "" if days is None else (NOW - timedelta(days=days)).isoformat()
        rid = conn.execute(
            "INSERT INTO records (company_id, source_id, category, kind, title, url, publisher, "
            "text, url_key, content_hash, published_at, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Pub', ?, ?, ?, ?, ?)",
            (cid, sid, category, kind, title, f"https://x.test/{title}", text, f"k|{title}",
             f"h|{title}", pub, NOW.isoformat())).lastrowid
        conn.execute("INSERT INTO record_sightings (record_id, source_id, seen_at) "
                     "VALUES (?, ?, ?)", (rid, sid, NOW.isoformat()))
        if sightings > 1:
            conn.execute("INSERT INTO record_sightings (record_id, source_id, seen_at) "
                         "VALUES (?, ?, ?)", (rid, _src(conn, cid, "community"), NOW.isoformat()))
    return rid


def sig(rid):
    with connect() as conn:
        r = conn.execute("SELECT * FROM signals WHERE record_id = ?", (rid,)).fetchone()
    return dict(r) if r else None


def test_prefilter_keeps_irrelevant_records_away_from_the_llm(cid):
    junk = add_rec(cid, "Celebrity gala", "Photos from the weekend gala event")
    good = add_rec(cid, "Anvil export rules")
    llm = FakeLLM()
    s = analyze_company(cid, llm_fn=llm)
    assert junk not in [i for b in llm.batches for i in b]
    assert sig(junk)["status"] == "filtered" and "prefilter" in sig(junk)["note"]
    assert sig(good)["status"] == "analyzed" and (s.filtered, s.analyzed) == (1, 1)


def test_snippet_verification_and_verification_levels(cid):
    a = add_rec(cid, "Rules A", tier="primary")
    b = add_rec(cid, "Rules B", tier="news")
    c = add_rec(cid, "Rules C", tier="news")
    d = add_rec(cid, "Rules D", tier="news", sightings=2)
    good = {"evidence_snippet": SNIPPET}
    fake = {"evidence_snippet": "rules for steel anvils are being scrapped entirely soon"}
    analyze_company(cid, llm_fn=FakeLLM({a: good, b: good, c: fake, d: good}))
    assert (sig(a)["verification"], sig(a)["evidence_verified"]) == ("verified", 1)
    assert sig(b)["verification"] == "reported"
    assert sig(c)["verification"] == "unverified" and sig(c)["evidence_snippet"] == ""
    assert "not found" in sig(c)["note"]
    assert sig(d)["verification"] == "verified"
    assert sig(a)["evidence_strength"] > sig(b)["evidence_strength"] > sig(c)["evidence_strength"]


def test_not_relevant_is_stored_and_never_reanalysed(cid):
    r = add_rec(cid, "Rules A")
    analyze_company(cid, llm_fn=FakeLLM({r: {"relevant": False}}))
    assert sig(r)["status"] == "not_relevant" and get_signals(cid) == []
    again = FakeLLM()
    analyze_company(cid, llm_fn=again)
    assert again.calls == 0


def test_llm_failure_leaves_records_pending_and_rerun_recovers(cid):
    r = add_rec(cid, "Rules A")
    s = analyze_company(cid, llm_fn=FakeLLM(fail=LLMError("boom")))
    assert s.errors and sig(r) is None
    s = analyze_company(cid, llm_fn=FakeLLM(fail=LLMQuotaError("quota")))
    assert s.errors and sig(r) is None
    assert analyze_company(cid, llm_fn=FakeLLM()).analyzed == 1


def test_invalid_enums_and_scores_are_clamped(cid):
    r = add_rec(cid, "Rules A")
    analyze_company(cid, llm_fn=FakeLLM({r: dict(classification="banana", urgency="asap",
                                                 impact=99, relevance=-3, affected_area="zzz")}))
    g = sig(r)
    assert (g["classification"], g["urgency"], g["impact"], g["relevance"], g["affected_area"]) \
        == ("neutral", "low", 5, 1, "other")


def test_unknown_and_missing_record_ids_are_handled(cid):
    a = add_rec(cid, "Rules A")
    b = add_rec(cid, "Rules B")
    s = analyze_company(cid, llm_fn=FakeLLM(extra=[9999], skip=[b]))
    assert sig(a)["status"] == "analyzed" and sig(b) is None and s.missing == 1
    assert sig(9999) is None


def test_customer_quote_must_appear_verbatim(cid):
    text = "I returned my anvil because the quality was awful, other blacksmiths warned me too."
    good = add_rec(cid, "Comment one", text, kind="comment", category="customers",
                   source_type="hn_query")
    bad = add_rec(cid, "Comment two", text, kind="comment", category="customers",
                  source_type="hn_query")
    article = add_rec(cid, "News article", text, category="customers",
                      source_type="news_query")
    analyze_company(cid, llm_fn=FakeLLM({
        good: {"customer_quote": "returned my anvil because the quality was awful"},
        bad: {"customer_quote": "the quality of their anvils is terrible and getting worse"},
        article: {"customer_quote": "I returned my anvil because the quality was awful"}}))
    assert sig(good)["customer_quote"].startswith("returned my anvil")
    assert sig(bad)["customer_quote"] == ""
    assert sig(article)["customer_quote"] == ""


def test_ranking_is_explainable_and_orders_sensibly(cid):
    assert abs(sum(config.PRIORITY_WEIGHTS.values()) - 1.0) < 1e-9
    strong = add_rec(cid, "Rules alpha", tier="primary", days=1)
    copy = add_rec(cid, "Rules gamma", tier="primary", days=1)     
    weak = add_rec(cid, "Old minor note about forged anvils", "Minor note on forged steel anvils.",
                   tier="community", days=80)
    analyze_company(cid, llm_fn=FakeLLM({
        strong: dict(evidence_snippet=SNIPPET, impact=5, relevance=5),
        copy: dict(evidence_snippet=SNIPPET, impact=5, relevance=5),
        weak: dict(impact=1, relevance=2, urgency="low", classification="neutral")}))
    s_strong, s_copy, s_weak = sig(strong), sig(copy), sig(weak)
    assert s_strong["novelty"] == 1.0 and s_copy["novelty"] < 0.5
    assert s_strong["priority"] > s_copy["priority"] > s_weak["priority"]
    assert "relevance 5/5" in s_strong["explanation"] and "novelty" in s_strong["explanation"]
    assert [g["record_id"] for g in get_signals(cid)][0] == strong


def test_reset_allows_reanalysis(cid):
    add_rec(cid, "Rules A")
    analyze_company(cid, llm_fn=FakeLLM())
    assert reset_signals(cid) == 1
    again = FakeLLM()
    analyze_company(cid, llm_fn=again)
    assert again.calls == 1