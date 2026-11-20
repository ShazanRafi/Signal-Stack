import sys

from backend.reassess import apply_reassessment, get_change, list_changes, reassess_context
from backend.interview import TOPICS
from backend.llm import LLMError

USAGE = """usage (replace UPPERCASE words with real values):
  python reassess_cli.py change COMPANY_ID "topic=new text" ["topic=new text" ...]
  python reassess_cli.py report CHANGE_ID
  python reassess_cli.py apply CHANGE_ID
  python reassess_cli.py list COMPANY_ID
topics: """ + ", ".join(TOPICS)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = sys.argv[1:]
    if len(a) < 2:
        print(USAGE)
        return
    try:
        if a[0] == "change":
            changes = dict(x.split("=", 1) for x in a[2:])
            r = reassess_context(int(a[1]), changes)
            print(f"change {r['change_id']}: {r['status']}\n")
            print(r["markdown"])
            print(f"\nApply it with: python reassess_cli.py apply {r['change_id']}")
        elif a[0] == "report":
            print(get_change(int(a[1]))["markdown"])
        elif a[0] == "apply":
            print(apply_reassessment(int(a[1])))
        elif a[0] == "list":
            for c in list_changes(int(a[1])):
                print(f"change {c['id']} [{c['status']}] {c['created_at'][:16]} {c['error']}")
        else:
            print(USAGE)
    except (LLMError, KeyError, ValueError) as e:
        print(f"error: {e}")


if __name__ == "__main__":
    main()