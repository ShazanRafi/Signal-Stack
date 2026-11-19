import hashlib
import json
import threading
import time
from typing import Type, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.config import settings
from app.db.models import LLMCache, LLMCall
from app.db.session import SessionLocal

T = TypeVar("T", bound=BaseModel)

RETRYABLE_CODES = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """Any LLM failure."""


class LLMQuotaError(LLMError):
    """Rate/quota exhausted even after retries (check your daily free-tier quota)."""


class _RateLimiter:
    """Spaces calls evenly so we never exceed max_rpm."""

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


_limiter = _RateLimiter(settings.llm_max_rpm)
_client = None


def _get_client():
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not set. Add it to your .env file.")
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _cache_key(prompt: str, schema: Type[BaseModel], system: str | None, temperature: float) -> str:
    # Schema is part of the key: changing the schema must not reuse old answers.
    payload = json.dumps(
        {
            "model": settings.gemini_model,
            "system": system or "",
            "prompt": prompt,
            "schema": schema.model_json_schema(),
            "temperature": temperature,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    with SessionLocal() as s:
        row = s.get(LLMCache, key)
        return row.response_json if row else None


def _cache_set(key: str, value: str) -> None:
    with SessionLocal() as s:
        s.merge(LLMCache(key=key, model=settings.gemini_model, response_json=value))
        s.commit()


def _log(purpose: str, cached: bool, ok: bool, error: str = "") -> None:
    with SessionLocal() as s:
        s.add(LLMCall(model=settings.gemini_model, purpose=purpose,
                      cached=cached, ok=ok, error=error[:1000]))
        s.commit()


def generate_structured(
    prompt: str,
    schema: Type[T],
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
                pass  # incompatible old cache entry: regenerate

    last_error = "unknown error"
    last_code: int | None = None

    for attempt in range(settings.llm_max_retries + 1):
        _limiter.wait()
        try:
            resp = _get_client().models.generate_content(
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            text = resp.text
            if not text:
                raise ValueError("empty response (possibly blocked by safety filters)")
            obj = schema.model_validate_json(text)  # ValidationError is a ValueError
        except errors.APIError as e:
            last_error = f"APIError {e.code}: {e.message}"
            last_code = e.code
            _log(purpose, cached=False, ok=False, error=last_error)
            if e.code not in RETRYABLE_CODES:
                raise LLMError(last_error) from e
            time.sleep(min(2 ** attempt * 2, 30))
            continue
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
    raise LLMError(f"Failed after {settings.llm_max_retries + 1} attempts. Last: {last_error}")