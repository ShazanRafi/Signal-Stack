import pytest

from signalstack.infrastructure import db
from signalstack.infrastructure.db import connect, init_db
from signalstack.domain.interview import (TOPICS, FirstQuestion, LLMEntity, OneQuestion,
                                          StepResult, edit_profile_entry, get_state,
                                          retry_pending, start_interview, submit_answer)
from signalstack.infrastructure.llm import LLMError

TOPIC_LIST = list(TOPICS)
FIRST_Q = "What are Acme's main revenue streams?"


def step(i, summary="Founder note", followup=None, entities=(), next_topic=None,
         next_q=None):
    nt = TOPIC_LIST[i + 1] if i + 1 < len(TOPIC_LIST) else ""
    return StepResult(
        topic_summary=summary,
        entities=[LLMEntity(name=n, kind=k) for n, k in entities],
        needs_followup=followup is not None,
        followup_question=followup or "",
        next_topic=nt if next_topic is None else next_topic,
        next_question=(f"Regarding {TOPICS[nt]}, what should we know?" if nt else "")
        if next_q is None else next_q,
    )


class FakeLLM:
    def __init__(self, steps=()):
        self.steps = list(steps)
        self.prompts, self.schemas = [], []
        self.fail_steps = False

    def __call__(self, prompt, schema, **kw):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if schema is FirstQuestion:
            return FirstQuestion(topic="business_lines", question=FIRST_Q)
        if schema is StepResult:
            if self.fail_steps:
                raise LLMError("quota")
            return self.steps.pop(0)
        if schema is OneQuestion:
            return OneQuestion(question=f"Fallback question number {len(self.prompts)}")
        raise AssertionError(schema)


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        c = conn.execute(
            "INSERT INTO companies (name, url, industry, summary, status) VALUES "
            "('Acme', 'https://acme.test/', 'Metalwork', 'Acme sells anvils.', 'researched')"
        ).lastrowid
        conn.execute("INSERT INTO profile_facts (company_id, category, claim, status) "
                     "VALUES (?, 'products', 'Acme sells forged steel anvils', 'verified')", (c,))
    return c


def run_default(cid, llm, skip=()):
    state = start_interview(cid, llm_fn=llm)
    for i in range(10):
        cur = state["current"]
        assert cur["topic"] == TOPIC_LIST[i]
        skipped = i in skip
        state = submit_answer(cid, cur["id"], "" if skipped else f"answer {i}",
                              skipped=skipped, llm_fn=llm)
    return state


def test_start_creates_first_question_and_resume_is_idempotent(cid):
    llm = FakeLLM()
    s1 = start_interview(cid, llm_fn=llm)
    assert s1["current"]["question"] == FIRST_Q and s1["current"]["seq"] == 1
    assert "Acme sells forged steel anvils" in llm.prompts[0]   
    n = len(llm.prompts)
    s2 = start_interview(cid, llm_fn=llm)
    assert len(llm.prompts) == n and s2["current"]["id"] == s1["current"]["id"]


def test_full_interview_covers_all_ten_topics_and_completes(cid):
    llm = FakeLLM([step(i) for i in range(10)])
    state = run_default(cid, llm)
    assert state["complete"] and state["core_done"] == 10 and state["current"] is None
    assert len(state["turns"]) == 10
    assert len({t["question"] for t in state["turns"]}) == 10      
    assert {e["topic"] for e in state["entries"]} == set(TOPIC_LIST)
    assert state["company"]["status"] == "interviewed"


def test_skip_stores_nothing_and_is_not_reasked(cid):
    llm = FakeLLM([step(i, summary="SHOULD BE IGNORED" if i == 2 else "ok") for i in range(10)])
    state = run_default(cid, llm, skip={2})
    assert state["complete"]
    assert TOPIC_LIST[2] not in {e["topic"] for e in state["entries"]}
    assert [t for t in state["turns"] if t["status"] == "skipped"][0]["topic"] == TOPIC_LIST[2]
    assert sum(1 for t in state["turns"] if t["topic"] == TOPIC_LIST[2]) == 1


def test_memory_later_prompts_contain_earlier_answers(cid):
    llm = FakeLLM([step(0), step(1)])
    state = start_interview(cid, llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "We sell anvils to blacksmiths",
                          llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "Our buyers are small forges",
                          llm_fn=llm)
    last = llm.prompts[-1]
    assert "We sell anvils to blacksmiths" in last and "Our buyers are small forges" in last


def test_followup_is_same_topic_capped_and_core_count_unchanged(cid):
    llm = FakeLLM([step(0, followup="Which product earns most?"),
                   step(0, followup="And again?"),      
                   step(1)])
    state = start_interview(cid, llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "Anvils and tongs", llm_fn=llm)
    assert state["current"]["is_followup"] == 1 and state["current"]["topic"] == TOPIC_LIST[0]
    assert state["core_done"] == 1
    state = submit_answer(cid, state["current"]["id"], "Anvils", llm_fn=llm)
    assert state["current"]["is_followup"] == 0 and state["current"]["topic"] == TOPIC_LIST[1]


def test_invalid_next_topic_falls_back_to_a_remaining_topic(cid):
    llm = FakeLLM([step(0, next_topic="business_lines", next_q="Something about revenue?")])
    state = start_interview(cid, llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "Anvils", llm_fn=llm)
    assert state["current"]["topic"] == TOPIC_LIST[1]
    assert state["current"]["question"].startswith("Fallback question")
    assert llm.schemas[-1] is OneQuestion


def test_duplicate_question_is_replaced(cid):
    llm = FakeLLM([step(0, next_q=FIRST_Q)])
    state = start_interview(cid, llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "Anvils", llm_fn=llm)
    assert state["current"]["question"].startswith("Fallback question")


def test_hallucinated_entities_are_dropped(cid):
    llm = FakeLLM([step(0, entities=[("Globex", "competitor"), ("Madeupco", "competitor")])])
    state = start_interview(cid, llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "Our main rival is Globex",
                          llm_fn=llm)
    assert [(e["name"], e["kind"]) for e in state["entities"]] == [("Globex", "competitor")]


def test_llm_failure_keeps_answer_and_retry_recovers(cid):
    llm = FakeLLM([step(0)])
    state = start_interview(cid, llm_fn=llm)
    turn_id = state["current"]["id"]
    llm.fail_steps = True
    with pytest.raises(LLMError):
        submit_answer(cid, turn_id, "Anvils", llm_fn=llm)
    s = get_state(cid)
    assert len(s["pending"]) == 1 and s["pending"][0]["answer"] == "Anvils"
    assert s["current"] is None
    llm.fail_steps = False
    s = retry_pending(cid, llm_fn=llm)
    assert not s["pending"] and s["current"]["topic"] == TOPIC_LIST[1]


def test_user_edit_is_not_overwritten_by_later_llm_merge(cid):
    llm = FakeLLM([step(0, summary="AI note", followup="Tell me more?"),
                   step(0, summary="AI merged note")])
    state = start_interview(cid, llm_fn=llm)
    state = submit_answer(cid, state["current"]["id"], "Anvils", llm_fn=llm)
    edit_profile_entry(cid, TOPIC_LIST[0], "Founder rewrite")
    submit_answer(cid, state["current"]["id"], "More detail", llm_fn=llm)
    entry = get_state(cid)["entries"][0]
    assert entry["content"] == "Founder rewrite" and entry["edited_by_user"] == 1


def test_edit_can_fill_a_skipped_topic_and_empty_edit_removes_it(cid):
    edit_profile_entry(cid, "competitors", "Globex and Initech")
    assert get_state(cid)["entries"][0]["content"] == "Globex and Initech"
    edit_profile_entry(cid, "competitors", "  ")
    assert get_state(cid)["entries"] == []
    with pytest.raises(ValueError):
        edit_profile_entry(cid, "not_a_topic", "x")


def test_empty_answer_and_double_answer_rejected(cid):
    llm = FakeLLM([step(0)])
    state = start_interview(cid, llm_fn=llm)
    tid = state["current"]["id"]
    with pytest.raises(ValueError):
        submit_answer(cid, tid, "   ", llm_fn=llm)
    submit_answer(cid, tid, "Anvils", llm_fn=llm)
    with pytest.raises(ValueError):
        submit_answer(cid, tid, "again", llm_fn=llm)