import time

from pydantic import BaseModel

from backend import config
from backend.db import init_db
from backend.llm import _get_client, generate_structured


class CapitalInfo(BaseModel):
    city: str
    country: str


def main() -> None:
    init_db()

    print("Models your key can see:")
    for m in _get_client().models.list():
        if "gemini" in m.name:
            print("  ", m.name)

    print(f"\nUsing model: {config.GEMINI_MODEL}")
    for i in (1, 2):
        t = time.time()
        out = generate_structured(
            "What is the capital of France? Return city and country.",
            CapitalInfo,
            purpose="smoke",
        )
        print(f"call {i}: {out}  ({time.time() - t:.2f}s)")


if __name__ == "__main__":
    main()