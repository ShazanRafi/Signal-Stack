import hashlib
import json
import threading
import time
from typing import TypeVar

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import (
    GoogleAPIError,
    GoogleGenerativeAIError,
    GoogleRateLimitError,
)
from pydantic import BaseModel

from signalstack.infrastructure import config
from signalstack.infrastructure.db import connect

T = TypeVar("T", bound=BaseModel)

RETRYABLE_CODES = {429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


class LLMQuotaError(LLMError):
    pass


class _RateLimiter:
    def __init__(self, max_rpm: int):
        self._interval = 60.0 / max(1, max_rpm)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            time.sleep(delay)


_limiter = _RateLimiter(config.LLM_MAX_RPM)
_models: dict[float, ChatGoogleGenerativeAI] = {}
_model_lock = threading.Lock()


def _get_model(temperature: float) -> ChatGoogleGenerativeAI:
    if not config.GEMINI_API_KEY:
        raise LLMError("No API key found. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env")
    with _model_lock:
        if temperature not in _models:
            _models[temperature] = ChatGoogleGenerativeAI(
                model=config.GEMINI_MODEL,
                api_key=config.GEMINI_API_KEY,
                temperature=temperature,
                retries=0,
                cache=False,
            )
        return _models[temperature]


def _cache_key(
    prompt: str,
    schema: type[BaseModel],
    system: str | None,
    temperature: float,
) -> str:
    payload = json.dumps(
        {
            "model": config.GEMINI_MODEL,
            "system": system or "",
            "prompt": prompt,
            "schema": schema.model_json_schema(),
            "temperature": temperature,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT response_json FROM llm_cache WHERE key = ?", (key,)
        ).fetchone()
    return row["response_json"] if row else None


def _cache_set(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, model, response_json) VALUES (?, ?, ?)",
            (key, config.GEMINI_MODEL, value),
        )


def _log(purpose: str, cached: bool, ok: bool, error: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO llm_calls (model, purpose, cached, ok, error) VALUES (?, ?, ?, ?, ?)",
            (config.GEMINI_MODEL, purpose, int(cached), int(ok), error[:1000]),
        )


def generate_structured(
    prompt: str,
    schema: type[T],
    *,
    system: str | None = None,
    temperature: float = 0.2,
    use_cache: bool = True,
    purpose: str = "",
) -> T:
    """Call Gemini and return a validated instance of `schema`.

    Raises LLMQuotaError / LLMError. Never returns unvalidated data.
    Failed attempts are never cached.
    """
    key = _cache_key(prompt, schema, system, temperature)

    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            try:
                obj = schema.model_validate_json(cached)
                _log(purpose, cached=True, ok=True)
                return obj
            except ValueError:
                pass  

    last_error = "unknown error"
    last_code: int | None = None

    for attempt in range(config.LLM_MAX_RETRIES + 1):
        _limiter.wait()
        try:
            prompt_messages = [("human", "{prompt}")]
            if system:
                prompt_messages.insert(0, ("system", system))
            prompt_template = ChatPromptTemplate.from_messages(prompt_messages)
            chain = prompt_template | _get_model(temperature).with_structured_output(
                schema, method="json_schema")
            result = chain.invoke({"prompt": prompt})
            if result is None:
                raise ValueError("empty response (possibly blocked by safety filters)")
            obj = schema.model_validate(result)
        except GoogleRateLimitError as e:
            last_error = str(e)
            last_code = 429
            _log(purpose, cached=False, ok=False, error=last_error)
            if last_code not in RETRYABLE_CODES:
                raise LLMError(last_error) from e
            time.sleep(min(2 ** attempt * 2, 30))
            continue
        except GoogleAPIError as e:
            last_code = getattr(e, "code", None)
            last_error = f"APIError {last_code}: {e}"
            _log(purpose, cached=False, ok=False, error=last_error)
            if last_code not in RETRYABLE_CODES:
                raise LLMError(last_error) from e
            time.sleep(min(2 ** attempt * 2, 30))
            continue
        except GoogleGenerativeAIError as e:
            last_error = str(e)
            last_code = None
            _log(purpose, cached=False, ok=False, error=last_error)
            raise LLMError(last_error) from e
        except ValueError as e:
            last_error = f"Invalid model output: {e}"
            last_code = None
            _log(purpose, cached=False, ok=False, error=last_error)
            time.sleep(1)
            continue

        _cache_set(key, obj.model_dump_json())
        _log(purpose, cached=False, ok=True)
        return obj

    if last_code == 429:
        raise LLMQuotaError(f"Rate/quota limit still hit after retries. Last: {last_error}")
    raise LLMError(f"Failed after {config.LLM_MAX_RETRIES + 1} attempts. Last: {last_error}")