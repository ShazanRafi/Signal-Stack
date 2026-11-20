import sys

from backend.db import connect

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
with connect() as conn:
    for c in conn.execute("SELECT id, name, industry FROM companies ORDER BY id").fetchall():
        i = c["id"]
        print(f"\n=== {i}: {c['name']} ({c['industry']}) ===")
        qs = [r["question"] for r in conn.execute(
            "SELECT question FROM interview_turns WHERE company_id = ? ORDER BY seq LIMIT 3", (i,))]
        for q in qs:
            print("  Q:", q[:150])
        comps = [r["name"] for r in conn.execute(
            "SELECT name FROM competitors WHERE company_id = ? AND status = 'active'", (i,))]
        print("  competitors:", ", ".join(comps) or "none")
        for r in conn.execute("SELECT category, status, COUNT(*) n FROM sources WHERE company_id = ? "
                              "GROUP BY category, status ORDER BY category", (i,)):
            print(f"  sources {r['category']:<12} {r['status']:<9} {r['n']}")
        n = lambda sql: conn.execute(sql, (i,)).fetchone()[0]
        print("  records:", n("SELECT COUNT(*) FROM records WHERE company_id = ?"),
              "| signals:", n("SELECT COUNT(*) FROM signals WHERE company_id = ? AND status='analyzed'"),
              "| briefs:", n("SELECT COUNT(*) FROM briefs WHERE company_id = ?"))