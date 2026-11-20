import pytest

from backend import db, reassess
from backend.db import connect, init_db
from backend.interview import edit_profile_entry
from backend.llm import LLMError
from backend.reassess import (CompetitorVerdict, Reassessment, SourceVerdict,
                              apply_reassessment, get_change, reassess_context)

CHANGE = {"top_segments": "Now selling to students heading to Canada"}


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        c = conn.execute("INSERT INTO companies (name, url, status) VALUES "
                         "('Acme', 'https://acme.test/', 'stack_built')").lastrowid
    edit_profile_entry(c, "top_segments", "Sell to students in India")
    return c


def add_src(cid, target, category="market", status="approved", origin="llm",
            competitor_id=None, validation="valid"):
    with connect() as conn:
        return conn.execute(
            "INSERT INTO sources (company_id, category, type, target, url, reason, origin, "
            "status, validation, competitor_id, dedupe_key) VALUES "
            "(?, ?, 'news_query', ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, category, target, target, f"reason for {target}", origin, status,
             validation, competitor_id, f"news_query|{target}")).lastrowid


def status_of(table, row_id):
    with connect() as conn:
        return conn.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


class FakeLLM:
    def __init__(self, sources=(), competitors=(), fail=None):
        self.sources, self.competitors, self.fail, self.prompts = sources, competitors, fail, []

    def __call__(self, prompt, schema, **kw):
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return Reassessment(sources=[SourceVerdict(**s) for s in self.sources],
                            competitors=[CompetitorVerdict(**c) for c in self.competitors])


def fake_build(company_id, fetcher=None, llm_fn=None):
    with connect() as conn:
        for target, validation in (("new canada topic", "valid"), ("new warn topic", "warning")):
            conn.execute(
                "INSERT INTO sources (company_id, category, type, target, url, reason, status, "
                "validation, dedupe_key) VALUES (?, 'market', 'news_query', ?, ?, 'why new', "
                "'proposed', ?, ?)", (company_id, target, target, validation, f"n|{target}"))
        conn.execute("INSERT INTO competitors (company_id, name) VALUES (?, 'NewRival')",
                     (company_id,))


@pytest.fixture
def stack(cid, monkeypatch):
    monkeypatch.setattr(reassess, "build_stack", fake_build)
    with connect() as conn:
        comp = conn.execute("INSERT INTO competitors (company_id, name, why_relevant) "
                            "VALUES (?, 'Globex', 'same students')", (cid,)).lastrowid
    ids = {"keep": add_src(cid, "keep topic"), "drop": add_src(cid, "old india topic"),
           "noreason": add_src(cid, "quiet topic"),
           "user": add_src(cid, "user-added-topic", origin="user"),
           "comp_src": add_src(cid, "globex headlines", "competitors", competitor_id=comp),
           "comp": comp}
    llm = FakeLLM(
        sources=[dict(source_id=ids["keep"], keep=True, reason=""),
                 dict(source_id=ids["drop"], keep=False, reason="Focus moved from India to Canada"),
                 dict(source_id=ids["noreason"], keep=False, reason="  "),
                 dict(source_id=9999, keep=False, reason="ghost")],
        competitors=[dict(competitor_id=comp, keep=False, reason="Not active in Canada")])
    return ids, llm


def test_report_filters_verdicts_and_changes_nothing_until_applied(cid, stack):
    ids, llm = stack
    out = reassess_context(cid, CHANGE, llm_fn=llm)
    res = out["result"]
    assert out["status"] == "ready"
    assert [s["id"] for s in res["drop_sources"]] == [ids["drop"]]      
    assert [c["id"] for c in res["drop_competitors"]] == [ids["comp"]]
    assert len(res["new_sources"]) == 2 and [c["name"] for c in res["new_competitors"]] == ["NewRival"]
    assert res["kept_sources"] == 2
    prompt = llm.prompts[0]
    assert "user-added-topic" not in prompt and "globex headlines" not in prompt
    assert "Sell to students in India" in prompt and "heading to Canada" in prompt
    md = out["markdown"]
    assert "Sell to students in India" in md and "heading to Canada" in md
    assert "Focus moved from India to Canada" in md
    assert status_of("sources", ids["drop"]) == "approved"            


def test_apply_removes_flagged_and_approves_only_clean_new_sources(cid, stack):
    ids, llm = stack
    ch = reassess_context(cid, CHANGE, llm_fn=llm)["change_id"]
    r = apply_reassessment(ch)
    assert r == {"sources_removed": 1, "competitors_removed": 1, "new_sources_approved": 1}
    assert status_of("sources", ids["drop"]) == "removed"
    assert status_of("sources", ids["comp_src"]) == "removed"       
    assert status_of("competitors", ids["comp"]) == "removed"
    assert status_of("sources", ids["user"]) == "approved"
    with connect() as conn:
        rows = {r["target"]: r["status"] for r in conn.execute(
            "SELECT target, status FROM sources WHERE target LIKE 'new %'")}
    assert rows == {"new canada topic": "approved", "new warn topic": "proposed"}
    assert get_change(ch)["status"] == "applied"
    with pytest.raises(ValueError):
        apply_reassessment(ch)


def test_invalid_or_unchanged_input_is_rejected_without_a_log_row(cid):
    for bad in ({"nonsense": "x"}, {"top_segments": "  "}, {},
                {"top_segments": "Sell to students in India"}):
        with pytest.raises(ValueError):
            reassess_context(cid, bad, llm_fn=FakeLLM())
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_changes").fetchone()[0] == 0


def test_llm_failure_marks_change_failed_but_keeps_the_edit(cid):
    with pytest.raises(LLMError):
        reassess_context(cid, CHANGE, llm_fn=FakeLLM(fail=LLMError("quota")), discover=False)
    with connect() as conn:
        c = conn.execute("SELECT status, error FROM context_changes").fetchone()
        note = conn.execute("SELECT content FROM profile_entries WHERE company_id = ? "
                            "AND topic = 'top_segments'", (cid,)).fetchone()[0]
    assert c["status"] == "failed" and "quota" in c["error"] and note == CHANGE["top_segments"]


def test_discovery_failure_still_produces_a_report(cid, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(reassess, "build_stack", boom)
    out = reassess_context(cid, CHANGE, llm_fn=FakeLLM())
    assert out["status"] == "ready" and "discovery failed" in " ".join(out["result"]["notes"])