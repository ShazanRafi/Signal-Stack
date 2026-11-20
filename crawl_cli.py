import sys

from backend.crawler import crawl_company, get_records, get_run_log, list_runs
from backend.db import connect, init_db

USAGE = """usage:
  python crawl_cli.py run <company_id>
  python crawl_cli.py runs <company_id>
  python crawl_cli.py log <run_id>
  python crawl_cli.py records <company_id> [limit] [category]
"""


def show_log(run_id: int) -> None:
    for l in get_run_log(run_id):
        print(f"[src {l['source_id']}] {l['status']:<10} seen={l['items_seen']} new={l['items_new']} "
              f"dup={l['items_duplicate']} old={l['items_old']} {l['type']:<10} {l['target'][:55]}")
        if l["note"]:
            print(f"        note: {l['note']}")


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
    cmd, n = a[0], int(a[1])
    if cmd == "run":
        s = crawl_company(n)
        print(f"run {s.run_id}: {s.status} sources={s.sources_checked} new={s.new_records} "
              f"duplicates={s.duplicates} errors={s.errors} {s.error}")
        show_log(s.run_id)
    elif cmd == "runs":
        for r in list_runs(n):
            print(f"run {r['id']} {r['started_at'][:19]} {r['run_type']:<7} {r['status']:<8} "
                  f"checked={r['sources_checked']} new={r['new_records']} errors={r['errors']}")
    elif cmd == "log":
        show_log(n)
    elif cmd == "records":
        limit = int(a[2]) if len(a) > 2 else 30
        for r in get_records(n, limit, a[3] if len(a) > 3 else None):
            print(f"[{r['id']}] {(r['published_at'] or 'no date')[:10]} {r['category']:<11} "
                  f"{r['kind']:<11} x{r['n_sightings']} {r['publisher'][:25]} | {r['title'][:90]}")
            print(f"      {r['url']}\n      {r['text'][:160]!r}")
    else:
        print(USAGE)


if __name__ == "__main__":
    main()