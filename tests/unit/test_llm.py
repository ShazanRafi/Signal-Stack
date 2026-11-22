import pytest
from google.genai import errors
from pydantic import BaseModel

from signalstack.infrastructure import db
from signalstack.infrastructure import llm


class Pet(BaseModel):
    name: str
    age: int


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.models = self

    def generate_content(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResp(outcome)


def api_error(code):
    return errors.APIError(code, {"error": {"message": "test", "status": "TEST"}})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class NoLimit:
        def wait(self):
            pass

    monkeypatch.setattr(llm, "_limiter", NoLimit())

    def install(outcomes):
        fake = FakeClient(outcomes)
        monkeypatch.setattr(llm, "_get_client", lambda: fake)
        return fake

    return install


def test_valid_response_then_cache_hit(setup):
    fake = setup(['{"name": "Rex", "age": 3}'])
    a = llm.generate_structured("pet?", Pet)
    b = llm.generate_structured("pet?", Pet)
    assert a == b == Pet(name="Rex", age=3)
    assert fake.calls == 1 


def test_invalid_json_is_retried(setup):
    fake = setup(["not json", '{"name": "Rex"}', '{"name": "Rex", "age": 3}'])
    out = llm.generate_structured("pet?", Pet)
    assert out.age == 3
    assert fake.calls == 3


def test_failed_attempts_are_not_cached(setup):
    setup(["bad"] * (llm.config.LLM_MAX_RETRIES + 1))
    with pytest.raises(llm.LLMError):
        llm.generate_structured("pet?", Pet)
    fake = setup(['{"name": "Rex", "age": 3}'])
    assert llm.generate_structured("pet?", Pet).name == "Rex"
    assert fake.calls == 1


def test_quota_error_after_retries(setup):
    fake = setup([api_error(429)] * (llm.config.LLM_MAX_RETRIES + 1))
    with pytest.raises(llm.LLMQuotaError):
        llm.generate_structured("pet?", Pet)
    assert fake.calls == llm.config.LLM_MAX_RETRIES + 1


def test_non_retryable_error_fails_fast(setup):
    fake = setup([api_error(400)])
    with pytest.raises(llm.LLMError):
        llm.generate_structured("pet?", Pet)
    assert fake.calls == 1