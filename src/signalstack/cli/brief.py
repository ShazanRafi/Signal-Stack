import sys
from pathlib import Path

from signalstack.domain.brief import (generate_brief, get_actions, get_brief, list_briefs,
                           set_action_status)
from signalstack.infrastructure.config import ROOT
from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.llm import LLMError

USAGE = """usage (replace the UPPERCASE words with real numbers):
  python brief_cli.py generate COMPANY_ID [DAYS]
  python brief_cli.py show BRIEF_ID
  python brief_cli.py list COMPANY_ID
  python brief_cli.py actions COMPANY_ID [todo|doing|done]
  python brief_cli.py action ACTION_ID todo|doing|done
  python brief_cli.py export BRIEF_ID [PATH]
"""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  
    init_db()
    a = sys.argv[1:]
    if len(a) < 2:
        with connect() as conn:
            rows = conn.execute("SELECT id, name, status FROM companies").fetchall()
        print(USAGE + "companies:")
        for r in rows:
            print(f"  {r['id']}: {r['name']} [{r['status']}]")
        return
    cmd = a[0]
    try:
        if cmd == "generate":
            r = generate_brief(int(a[1]), days=int(a[2]) if len(a) > 2 else 7)
            print(f"brief {r.brief_id}: {r.status}, {r.n_main} signal(s) above the threshold, "
                  f"{r.n_cited} source(s) cited")
            for n in r.notes:
                print("quality note:", n)
            print(f"\nRead it with: python brief_cli.py show {r.brief_id}")
        elif cmd == "show":
            print(get_brief(int(a[1]))["markdown"])
        elif cmd == "list":
            for b in list_briefs(int(a[1])):
                print(f"brief {b['id']} {b['created_at'][:16]} {b['run_type']:<7} "
                      f"{b['status']:<9} cited={b['n_signals']} "
                      f"{b['period_start'][:10]}..{b['period_end'][:10]}")
        elif cmd == "actions":
            for x in get_actions(int(a[1]), a[2] if len(a) > 2 else None):
                print(f"#{x['id']} [{x['status']}] P{x['priority']} {x['owner']}: {x['action']}")
        elif cmd == "action":
            set_action_status(int(a[1]), a[2])
            print("updated")
        elif cmd == "export":
            path = Path(a[2]) if len(a) > 2 else ROOT / "sample_reports" / f"brief_{a[1]}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(get_brief(int(a[1]))["markdown"], encoding="utf-8")
            print("written:", path)
        else:
            print(USAGE)
    except (LLMError, KeyError, ValueError) as e:
        print(f"error: {e}")


if __name__ == "__main__":
    main()