import sys

from signalstack.services.analyzer import analyze_company, get_signals, reset_signals
from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.llm import LLMError
from signalstack.domain.ranker import rank_company

USAGE = """usage:
  python analyze_cli.py run <company_id> [max_records]
  python analyze_cli.py top <company_id> [n] [threat|opportunity|neutral]
  python analyze_cli.py rerank <company_id>     (after editing weights in config.py)
  python analyze_cli.py reset <company_id>      (delete signals to re-analyse everything)
"""


def main() -> None:
    init_db()
    a = sys.argv[1:]
    if len(a) < 2:
        with connect() as conn:
            rows = conn.execute("SELECT id, name, status FROM companies").fetchall()
        print(USAGE + "companies:")
        for r in rows:
            print(f"  {r['id']}: {r['name']} [{r['status']}]")
        return
    cmd, cid = a[0], int(a[1])
    try:
        if cmd == "run":
            s = analyze_company(cid, max_records=int(a[2]) if len(a) > 2 else 30)
            print(f"analyzed={s.analyzed} not_relevant={s.not_relevant} filtered={s.filtered} "
                  f"missing={s.missing} remaining={s.remaining}")
            for e in s.errors:
                print("error:", e)
            if s.remaining:
                print("run again to continue with the remaining records")
        elif cmd == "top":
            n = int(a[2]) if len(a) > 2 else 10
            for g in get_signals(cid, n, a[3] if len(a) > 3 else None):
                print(f"\n#{g['id']} [{g['priority']:.0f}] {g['classification'].upper()} "
                      f"urgency={g['urgency']} area={g['affected_area']} "
                      f"verification={g['verification']} ({g['competitor_name'] or g['category']})")
                print(f"  {g['title'][:100]}")
                print(f"  FACT: {g['what_happened']}")
                print(f"  EVIDENCE: {g['evidence_snippet']!r}" if g["evidence_snippet"]
                      else f"  EVIDENCE: none verified ({g['note']})")
                if g["customer_quote"]:
                    print(f"  PUBLIC COMMENT (author not verified as a customer): "
                          f"{g['customer_quote']!r}")
                print(f"  INTERPRETATION: {g['why_it_matters']}")
                print(f"  SUGGESTED: {g['suggested_action']}")
                print(f"  WHY RANKED: {g['explanation']}")
                print(f"  {g['url']}")
        elif cmd == "rerank":
            print("re-ranked:", rank_company(cid))
        elif cmd == "reset":
            print("deleted:", reset_signals(cid))
        else:
            print(USAGE)
    except LLMError as e:
        print(f"LLM error: {e}")


if __name__ == "__main__":
    main()