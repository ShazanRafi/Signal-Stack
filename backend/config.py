import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
LLM_MAX_RPM = int(os.getenv("LLM_MAX_RPM", "8"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "4"))
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "signalstack.db"))
USER_AGENT = os.getenv(
    "USER_AGENT", "SignalStackBot/0.1 (student project; contact: your-email@example.com)"
)
PRIORITY_WEIGHTS = {
    "relevance": 0.25, "impact": 0.20, "urgency": 0.15, "reliability": 0.10,
    "evidence": 0.10, "recency": 0.10, "novelty": 0.10,
}
RECENCY_HALF_LIFE_DAYS = 14