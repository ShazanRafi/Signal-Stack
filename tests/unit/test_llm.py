import pytest
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import GoogleAPIError, GoogleRateLimitError
from pydantic import BaseModel

from signalstack.infrastructure import db
from signalstack.infrastructure import llm


class Pet(BaseModel):
    name: str
    age: int


class FakeRunnable:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __call__(self, messages):
        return self.invoke(messages)


class FakeModel:
    def __init__(self, outcomes):
        self.runnable = FakeRunnable(outcomes)

    def with_structured_output(self, schema, method):
        assert method == "json_schema"
        return self.runnable


def api_error(code):
    if code == 429:
        return GoogleRateLimitError("quota test")
    return GoogleAPIError(code=code, response_json={"error": "test"}, response=None)


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
        fake = FakeModel(outcomes)
        monkeypatch.setattr(llm, "_get_model", lambda temperature: fake)
        return fake.runnable

    return install


def test_valid_response_then_cache_hit(setup):
    fake = setup([{"name": "Rex", "age": 3}])
    a = llm.generate_structured("pet?", Pet)
    b = llm.generate_structured("pet?", Pet)
    assert a == b == Pet(name="Rex", age=3)
    assert fake.calls == 1 


def test_langchain_gemini_model_accepts_structured_schema(monkeypatch):
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_models", {})

    model = llm._get_model(0.2)
    structured = model.with_structured_output(Pet, method="json_schema")

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.max_retries == 0
    assert structured is not None


def test_invalid_structured_output_is_retried(setup):
    fake = setup([{"name": "Rex"}, {"name": "Rex"}, {"name": "Rex", "age": 3}])
    out = llm.generate_structured("pet?", Pet)
    assert out.age == 3
    assert fake.calls == 3


def test_failed_attempts_are_not_cached(setup):
    setup([{"name": "Rex"}] * (llm.config.LLM_MAX_RETRIES + 1))
    with pytest.raises(llm.LLMError):
        llm.generate_structured("pet?", Pet)
    fake = setup([{"name": "Rex", "age": 3}])
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