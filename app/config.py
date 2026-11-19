from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    llm_max_rpm: int = 8
    llm_max_retries: int = 4
    database_url: str = f"sqlite:///{ROOT / 'data' / 'signalstack.db'}"
    user_agent: str = "SignalStackBot/0.1 (student project; contact: your-email@example.com)"


settings = Settings()