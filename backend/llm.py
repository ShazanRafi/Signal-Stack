import hashlib
import json
import threading
import time
from typing import Type, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from backend import config
from backend.db import connect

T = TypeVar("T", bound=BaseModel)

RETRYABLE_CODES = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """Any LLM failure."""


class LLMQuotaError(LLMError):
    """Rate/quota still exhausted after retries (check your daily free-tier quota)."""


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


_limiter = _RateLimiter(config.LLM_MAX_RPM)
_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise LLMError("No API key found. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _cache_key(prompt: str, schema: Type[BaseModel], system: str | None, temperature: float) -> str:
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
                pass  

    last_error = "unknown error"
    last_code: int | None = None

    for attempt in range(config.LLM_MAX_RETRIES + 1):
        _limiter.wait()
        try:
            resp = _get_client().models.generate_content(
                model=config.GEMINI_MODEL,
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
            obj = schema.model_validate_json(text)  
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
    raise LLMError(f"Failed after {config.LLM_MAX_RETRIES + 1} attempts. Last: {last_error}")