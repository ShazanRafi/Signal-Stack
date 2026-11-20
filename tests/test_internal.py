import re

import pytest

from backend import analyzer, db, internal
from backend.brief import generate_brief, get_brief
from backend.db import connect, init_db
from backend.internal import ChunkTags, RowTag, analyze_upload, get_findings
from backend.llm import LLMError

ROWS = [
    "The price is too high compared with other agencies so we walked away",
    "Your price is too high for our budget and the competitor was cheaper",
    "Honestly the price is too high and nobody explained the fees upfront",
    "Support never answered my emails for two weeks",
    "I would love a mobile app to track my application status",
    "Great experience overall with the counsellor",
]


class FakeLLM:
    def __init__(self, plan, fail_on=()):
        self.plan, self.fail_on, self.prompts, self.calls = plan, set(fail_on), [], 0

    def __call__(self, prompt, schema, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls in self.fail_on:
            raise LLMError("boom")
        refs = re.findall(r"^(R\d+):", prompt, re.M)
        tags = [RowTag(row_ref=r, category=c, theme=t, quote=q)
                for r in refs if r in self.plan for c, t, q in [self.plan[r]]]
        return ChunkTags(tags=tags)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(internal, "CHUNK_ROWS", 3)
    init_db()
    with connect() as conn:
        cid = conn.execute("INSERT INTO companies (name, url, status) VALUES "
                           "('Acme', 'https://acme.test/', 'stack_built')").lastrowid
        sid = conn.execute(
            "INSERT INTO sources (company_id, category, type, target, reason, tier, status, "
            "dedupe_key) VALUES (?, 'internal', 'upload', 'customer_feedback', 'r', "
            "'internal', 'approved', 'upload|cf')", (cid,)).lastrowid
        uid = conn.execute(
            "INSERT INTO uploads (company_id, source_id, kind, filename, file_type, note, "
            "rows_analyzed) VALUES (?, ?, 'customer_feedback', 'fb.csv', 'csv', "
            "'PUBLIC PROXY DATASET: demo, not company data', ?)", (cid, sid, len(ROWS))).lastrowid
        conn.executemany("INSERT INTO upload_rows (upload_id, ref, text) VALUES (?, ?, ?)",
                         [(uid, f"R{i}", t) for i, t in enumerate(ROWS, 1)])
    return cid, uid


PRICE = ("objection", "high price", "price is too high")
PLAN = {"R1": PRICE, "R2": PRICE,
        "R3": ("objection", "high price", "a quote that is not in the row"),
        "R4": ("complaint", "slow support", "never answered my emails")}


def records(cid):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM records WHERE company_id = ? AND category = 'internal'", (cid,))]


def test_counts_and_quotes_are_verified_in_code(setup):
    cid, uid = setup
    out = analyze_upload(uid, llm_fn=FakeLLM(PLAN))
    f = {x["theme"]: x for x in get_findings(uid)}
    assert f["high price"]["row_count"] == 3                 
    assert len(f["high price"]["quotes"]) == 2              
    assert f["slow support"]["row_count"] == 1 and f["slow support"]["record_id"] is None
    recs = records(cid)
    assert len(recs) == 1 and out["records"] == 1
    r = recs[0]
    assert r["kind"] == "internal_finding" and "3 of 6 analysed rows" in r["title"]
    assert "price is too high" in r["text"] and "PUBLIC PROXY DATASET" in r["text"]
    assert "not in the row" not in r["text"]
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM record_sightings WHERE record_id = ?",
                            (r["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM uploads WHERE id = ?", (uid,)).fetchone()[0] == "analyzed"


def test_theme_without_enough_rows_or_quotes_does_not_become_a_record(setup):
    cid, uid = setup
    plan = {"R1": ("objection", "high price", "a quote that is not in the row"),
            "R2": ("objection", "high price", "another invented quote here"),
            "R5": ("product_request", "mobile app", "love a mobile app to track")}
    analyze_upload(uid, llm_fn=FakeLLM(plan))
    assert records(cid) == []                                


def test_invented_row_refs_and_categories_are_handled(setup):
    cid, uid = setup
    plan = {"R1": ("nonsense_category", "high price", "price is too high"),
            "R2": ("objection", "high price", "price is too high")}
    llm = FakeLLM(plan)
    base = llm.__call__

    def with_ghost(prompt, schema, **kw):
        res = base(prompt, schema, **kw)
        res.tags.append(RowTag(row_ref="R999", category="objection", theme="ghost", quote="x"))
        return res
    analyze_upload(uid, llm_fn=with_ghost)
    themes = {f["theme"] for f in get_findings(uid)}
    assert "ghost" not in themes and "high price" in themes


def test_existing_themes_are_offered_to_later_chunks(setup):
    cid, uid = setup
    llm = FakeLLM(PLAN)
    analyze_upload(uid, llm_fn=llm)
    assert len(llm.prompts) == 2
    assert "(none yet)" in llm.prompts[0] and "high price" in llm.prompts[1]


def test_partial_failure_is_reported_and_counts_only_processed_rows(setup):
    cid, uid = setup
    out = analyze_upload(uid, llm_fn=FakeLLM(PLAN, fail_on=[2]))
    assert out["rows_analyzed"] == 3 and out["errors"]
    with connect() as conn:
        row = conn.execute("SELECT status, error, rows_analyzed FROM uploads WHERE id = ?",
                           (uid,)).fetchone()
    assert row["status"] == "analyzed" and "chunk 2/2" in row["error"] and row["rows_analyzed"] == 3


def test_total_failure_keeps_earlier_results(setup):
    cid, uid = setup
    analyze_upload(uid, llm_fn=FakeLLM(PLAN))
    out = analyze_upload(uid, llm_fn=FakeLLM(PLAN, fail_on=[1, 2]))
    assert out["rows_analyzed"] == 0 and out["errors"]
    assert len(records(cid)) == 1


def test_reprocessing_replaces_records_instead_of_duplicating(setup):
    cid, uid = setup
    analyze_upload(uid, llm_fn=FakeLLM(PLAN))
    other = {"R1": ("complaint", "slow support", "never answered"),
             "R4": ("complaint", "slow support", "never answered my emails")}
    analyze_upload(uid, llm_fn=FakeLLM(other))
    recs = records(cid)
    assert len(recs) == 1 and "slow support" in recs[0]["title"]


def test_internal_records_always_pass_the_analyzer_prefilter():
    r = {"category": "internal", "kind": "internal_finding", "title": "t", "text": "x",
         "competitor_name": None}
    assert analyzer.prefilter(r, set())[0] is True


def test_brief_discloses_proxy_data_even_without_findings(setup):
    cid, uid = setup
    with connect() as conn:
        conn.execute("UPDATE uploads SET status = 'analyzed' WHERE id = ?", (uid,))

    def never(*a, **k):
        raise AssertionError("LLM must not be called")
    md = get_brief(generate_brief(cid, llm_fn=never).brief_id)["markdown"]
    section = md.split("## 5.")[1].split("## 6.")[0]
    assert "PUBLIC PROXY DATASET" in section and "fb.csv" in section
    assert "No internal finding met the relevance threshold" in section