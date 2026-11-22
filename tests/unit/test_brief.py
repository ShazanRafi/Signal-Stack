import re
from datetime import datetime, timedelta, timezone

import pytest

from signalstack.infrastructure import db
from signalstack.domain.brief import (BriefNarrative, LLMAction, LLMOpportunity, LLMWatch,
                           generate_brief, get_actions, get_brief, set_action_status)
from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.llm import LLMError

NOW = datetime.now(timezone.utc)


def narr(**kw):
    base = dict(overview="", customers_summary="", competitors_summary="", market_summary="",
                internal_summary="", opportunities=[], actions=[], watch=[])
    base.update(kw)
    return BriefNarrative(**base)


class FakeLLM:
    def __init__(self, narrative=None, fail=None):
        self.narrative, self.fail, self.calls, self.prompts = narrative, fail, 0, []

    def __call__(self, prompt, schema, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return self.narrative


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        return conn.execute("INSERT INTO companies (name, url, industry, summary, status) VALUES "
                            "('Acme', 'https://acme.test/', 'Metalwork', 'Acme sells anvils.', "
                            "'crawled')").lastrowid


@pytest.fixture
def comp(cid):
    with connect() as conn:
        return conn.execute("INSERT INTO competitors (company_id, name) VALUES (?, 'Globex')",
                            (cid,)).lastrowid


def add_sig(cid, title, category="market", priority=60.0, relevance=4, classification="threat",
            urgency="high", snippet="", verification="reported", theme="", quote="",
            days_ago=0, tier="news", published="", competitor_id=None, text="body text",
            source_type="rss", kind="article"):
    with connect() as conn:
        row = conn.execute("SELECT id FROM sources WHERE company_id=? AND tier=? AND type=?",
                           (cid, tier, source_type)).fetchone()
        src = row["id"] if row else conn.execute(
            "INSERT INTO sources (company_id, category, type, target, url, reason, tier, "
            "status, dedupe_key) VALUES (?, 'market', ?, ?, ?, 'r', ?, 'approved', ?)",
            (cid, source_type, tier, tier, tier, f"{source_type}|{tier}")).lastrowid
        disc = (NOW - timedelta(days=days_ago)).isoformat()
        rid = conn.execute(
            "INSERT INTO records (company_id, source_id, competitor_id, category, kind, title, "
            "url, publisher, text, url_key, content_hash, published_at, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'Pub News', ?, ?, ?, ?, ?)",
            (cid, src, competitor_id, category, kind, title,
             f"https://news.test/{title.replace(' ', '-')}", text, f"k|{title}", f"h|{title}",
             published, disc)).lastrowid
        return conn.execute(
            "INSERT INTO signals (company_id, record_id, status, what_happened, affected_area, "
            "classification, urgency, impact, relevance, why_it_matters, suggested_action, "
            "evidence_snippet, evidence_verified, customer_quote, theme, verification, "
            "reliability, evidence_strength, recency, novelty, priority, explanation) "
            "VALUES (?, ?, 'analyzed', ?, 'other', ?, ?, 4, ?, ?, 'Act', ?, ?, ?, ?, ?, "
            "0.7, 0.5, 0.5, 1.0, ?, 'because')",
            (cid, rid, f"{title} happened", classification, urgency, relevance,
             f"Interpretation of {title}", snippet, int(bool(snippet)), quote, theme,
             verification, priority)).lastrowid


def test_empty_period_gives_honest_brief_without_calling_llm(cid):
    def never(*a, **k):
        raise AssertionError("LLM must not be called")
    r = generate_brief(cid, llm_fn=never, now=NOW)
    md = get_brief(r.brief_id)["markdown"]
    assert r.status == "complete" and r.n_main == 0
    for n in range(1, 10):
        assert f"## {n}. " in md
    assert "## 10. Source Appendix" in md
    assert "No developments met the relevance threshold" in md
    assert md.count("No new records were collected") >= 3


def test_full_brief_has_all_sections_and_every_citation_resolves(cid, comp):
    a = add_sig(cid, "Globex cuts prices", "competitors", 80, snippet="Globex cuts prices on every plan",
                verification="verified", tier="primary", competitor_id=comp,
                published=(NOW - timedelta(days=2)).isoformat())
    b = add_sig(cid, "Export rules tighten", "market", 70)
    nar = narr(
        overview=f"Competitor pricing and export rules both moved [S{a}] [S{b}].",
        competitors_summary=f"Globex cut its prices [S{a}].",
        market_summary=f"New export rules are coming [S{b}].",
        opportunities=[LLMOpportunity(title="Counter Globex on price", rationale="A bundle may win share",
                                      impact="high", next_step="Draft a bundle", priority=1,
                                      signal_ids=[a])],
        actions=[LLMAction(action="Review pricing", reason="Globex price cut", owner="Founder",
                           priority=1, signal_ids=[a])],
        watch=[LLMWatch(item="Export rules", reason="may tighten further", signal_ids=[b])])
    r = generate_brief(cid, llm_fn=FakeLLM(nar), now=NOW)
    md = get_brief(r.brief_id)["markdown"]

    for title in ("1. Executive Summary", "2. Customer Signals", "3. Competitor Intelligence",
                  "4. Market Intelligence", "5. Internal Business Intelligence",
                  "6. Opportunities Identified", "7. Risks & Threats",
                  "8. Recommended Actions for This Week", "9. What We Should Watch",
                  "10. Source Appendix"):
        assert f"## {title}" in md
    body, appendix = md.split("## 10. Source Appendix")
    for n in set(re.findall(r"\[(\d+)\]", body)):
        assert f"[{n}] " in appendix                      
    assert "https://news.test/Globex-cuts-prices" in appendix
    assert "Globex cuts prices on every plan" in appendix  
    assert "Published:" in appendix and "Discovered:" in appendix
    assert body.index("Globex cuts prices") < body.index("Export rules tighten")  
    assert r.status == "complete" and r.n_cited == 2


def test_unsafe_ai_items_are_removed_and_reported(cid):
    a = add_sig(cid, "Export rules tighten", "market", 70, text="Export duty rises to 12% from April")
    nar = narr(
        overview="Everything changed [S99999].",
        market_summary=f"Duty rises to 45% [S{a}].",
        opportunities=[
            LLMOpportunity(title="Ghost", rationale="x", impact="high", next_step="y",
                           priority=1, signal_ids=[424242]),
            LLMOpportunity(title="Real one", rationale="Duty change opens a niche",
                           impact="medium", next_step="Test pricing", priority=2, signal_ids=[a])],
        actions=[LLMAction(action="Do thing", reason="Because", owner="", priority=9,
                           signal_ids=[a])])
    r = generate_brief(cid, llm_fn=FakeLLM(nar), now=NOW)
    b = get_brief(r.brief_id)
    md = b["markdown"]
    assert "Everything changed" not in md and "Duty rises to 45%" not in md
    opp_section = md.split("## 6.")[1].split("## 7.")[0]
    assert "Real one" in opp_section and "Ghost" not in opp_section
    notes = " ".join(b["quality_notes"])
    assert "Overview removed" in notes and "Market summary removed" in notes and "Ghost" in notes
    assert "Unassigned" in md and "priority 5" in md


def test_llm_failure_gives_data_only_brief_with_cards(cid):
    add_sig(cid, "Export rules tighten", "market", 70)
    r = generate_brief(cid, llm_fn=FakeLLM(fail=LLMError("quota")), now=NOW)
    b = get_brief(r.brief_id)
    assert r.status == "data_only" and b["status"] == "data_only"
    assert "Data-only brief" in b["markdown"] and "Export rules tighten" in b["markdown"]
    assert "Not generated" in b["markdown"]
    assert get_actions(cid) == []


def test_threshold_window_and_watch_pool(cid):
    strong = add_sig(cid, "Strong item", "market", 70, relevance=4)
    weak = add_sig(cid, "Weak item", "market", 30, relevance=3)
    noise = add_sig(cid, "Noise item", "market", 10, relevance=1)
    old = add_sig(cid, "Old item", "market", 90, relevance=5, days_ago=30)
    llm = FakeLLM(narr(watch=[LLMWatch(item="Weak thing", reason="early", signal_ids=[weak])]))
    r = generate_brief(cid, llm_fn=llm, days=7, now=NOW)
    prompt = llm.prompts[0]
    assert f"[S{strong}]" in prompt and f"[S{weak}]" in prompt
    assert f"[S{noise}]" not in prompt and f"[S{old}]" not in prompt
    md = get_brief(r.brief_id)["markdown"]
    assert "Weak thing" in md and "Old item" not in md


def test_opportunity_shortfall_is_stated_not_padded(cid):
    a = add_sig(cid, "Export rules tighten", "market", 70)
    nar = narr(opportunities=[LLMOpportunity(title="Only one", rationale="Reason", impact="low",
                                             next_step="Do it", priority=1, signal_ids=[a])])
    r = generate_brief(cid, llm_fn=FakeLLM(nar), now=NOW)
    b = get_brief(r.brief_id)
    assert "Only 1 evidence-backed opportunity" in " ".join(b["quality_notes"])
    assert "Only 1 evidence-backed opportunity was identified" in b["markdown"]
    assert "Opportunity 2" not in b["markdown"]


def test_actions_are_stored_and_trackable(cid):
    a = add_sig(cid, "Export rules tighten", "market", 70)
    nar = narr(actions=[
        LLMAction(action="Call top agents", reason="Price war", owner="Head of sales",
                  priority=2, signal_ids=[a]),
        LLMAction(action="Update FAQ", reason="Confusion", owner="Support", priority=1,
                  signal_ids=[a])])
    r = generate_brief(cid, llm_fn=FakeLLM(nar), now=NOW)
    acts = get_actions(cid)
    assert [x["action"] for x in acts] == ["Update FAQ", "Call top agents"]
    assert all(x["status"] == "todo" for x in acts)
    assert f"action #{acts[0]['id']}" in get_brief(r.brief_id)["markdown"]
    set_action_status(acts[0]["id"], "done")
    assert get_actions(cid, "done")[0]["action"] == "Update FAQ"
    with pytest.raises(ValueError):
        set_action_status(acts[0]["id"], "banana")


def test_customer_caveat_changes_when_real_quotes_exist(cid):
    add_sig(cid, "Buyers complain", "customers", 60, theme="slow support")
    r1 = generate_brief(cid, llm_fn=FakeLLM(narr()), now=NOW)
    assert "No qualifying public community comments" in get_brief(r1.brief_id)["markdown"]

    add_sig(cid, "Forum thread", "customers", 55, theme="slow support",
            quote="support never answered my emails",
            text="He said support never answered my emails for weeks",
            source_type="hn_query", kind="comment")
    r2 = generate_brief(cid, llm_fn=FakeLLM(narr()), now=NOW)
    md = get_brief(r2.brief_id)["markdown"]
    assert "support never answered my emails" in md and "1 verbatim public community comment" in md
    assert "customer status is not independently verified" in md
    assert "slow support (2)" in md


def test_brief_discloses_manual_public_source_review(cid):
    signal_id = add_sig(cid, "External update")
    with connect() as conn:
        conn.execute("UPDATE signals SET note=? WHERE id=?",
                     ("Manual public-source review; Gemini unavailable after HTTP 503.",
                      signal_id))

    r = generate_brief(cid, llm_fn=FakeLLM(narr()), now=NOW)

    assert "Gemini did not analyze these items" in get_brief(r.brief_id)["markdown"]