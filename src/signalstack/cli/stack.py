import sys

from signalstack.infrastructure.db import connect, init_db
from signalstack.infrastructure.llm import LLMError
from signalstack.domain.stack import (add_source, approve_all_valid, build_stack, get_stack,
                           remove_competitor, set_source_status)

USAGE = """usage:
  python stack_cli.py build <company_id>
  python stack_cli.py list <company_id> [--all]
  python stack_cli.py approve-all <company_id>
  python stack_cli.py approve <source_id> [<source_id> ...]
  python stack_cli.py remove <source_id> [<source_id> ...]
  python stack_cli.py remove-competitor <competitor_id>
  python stack_cli.py add <company_id> <customers|competitors|market> <rss|news_query|hn_query|web_page> "<target>" "<reason>"
"""


def show(cid: int, show_all: bool) -> None:
    st = get_stack(cid)
    print(f"\n== COMPETITORS ({sum(1 for c in st['competitors'] if c['status'] == 'active')}) ==")
    for c in st["competitors"]:
        if c["status"] != "active" and not show_all:
            continue
        print(f"[{c['id']}] {c['name']} ({c['kind']}, {c['named_by']}) site={c['site_status']} "
              f"{c['url']}\n      {c['why_relevant']}")
    for cat in ("customers", "competitors", "market", "internal"):
        rows = [s for s in st["sources"] if s["category"] == cat
                and (show_all or s["status"] != "removed")]
        print(f"\n== {cat.upper()} {st['counts'].get(cat, {})} ==")
        for s in rows:
            print(f"[{s['id']}] {s['status']:<8} {s['validation']:<9} {s['tier']:<9} "
                  f"{s['type']:<10} {s['target'][:70]}")
            print(f"      why: {s['reason'][:200]}")
            if s["validation"] in ("warning", "invalid"):
                print(f"      note: {s['validation_note']}")


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
    cmd = a[0]
    try:
        if cmd == "build":
            r = build_stack(int(a[1]))
            print(f"competitors={r.competitors} proposed={r.proposed} rejected={r.rejected} "
                  f"duplicates={r.duplicates}")
            for n in r.notes:
                print("note:", n)
            show(int(a[1]), False)
        elif cmd == "list":
            show(int(a[1]), "--all" in a)
        elif cmd == "approve-all":
            print("approved:", approve_all_valid(int(a[1])))
        elif cmd in ("approve", "remove"):
            for sid in a[1:]:
                n = set_source_status(int(sid), "approved" if cmd == "approve" else "removed")
                print(f"source {sid}: {'ok' if n else 'not changed (invalid or missing)'}")
        elif cmd == "remove-competitor":
            remove_competitor(int(a[1]))
            print("removed competitor and its sources")
        elif cmd == "add":
            print(add_source(int(a[1]), a[2], a[3], a[4], a[5]))
        else:
            print(USAGE)
    except LLMError as e:
        print(f"LLM error: {e}")


if __name__ == "__main__":
    main()