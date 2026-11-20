import sys

from backend import scheduler as sc
from backend.audit import get_latest_audit
from backend.db import connect, init_db
from backend.llm import LLMError

USAGE = """usage (replace UPPERCASE words with real values):
  python scheduler_cli.py overview
  python scheduler_cli.py settings COMPANY_ID
  python scheduler_cli.py set COMPANY_ID key=value [key=value ...]
  python scheduler_cli.py brief-in COMPANY_ID MINUTES
  python scheduler_cli.py run-now COMPANY_ID weekly_brief|monitor|monthly_audit
  python scheduler_cli.py tick
  python scheduler_cli.py runs [COMPANY_ID] [LIMIT]
  python scheduler_cli.py failures [COMPANY_ID]
  python scheduler_cli.py audit-report COMPANY_ID
keys: timezone brief_weekday(0=Mon) brief_time(HH:MM) brief_days crawl_every_hours
      audit_day(1-28) audit_time(HH:MM) brief_enabled monitor_enabled audit_enabled (0 or 1)
"""


def show_runs(rows) -> None:
    for r in rows:
        print(f"run {r['id']} {r['started_at'][:19]} co={r['company_id']} {r['job_type']:<13} "
              f"{r['triggered_by']:<9} {r['status']:<8} attempt={r['attempt']}")
        if r["error"]:
            print(f"      {r['error'][:200]}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    init_db()
    a = sys.argv[1:]
    if not a:
        print(USAGE)
        return
    cmd = a[0]
    try:
        if cmd == "overview":
            w = sc.worker_status()
            print("worker:", "ALIVE" if w["alive"] else
                  ("not running (last seen " + w["last_seen"] + ")" if w["seen"]
                   else "never started"))
            for r in sc.schedule_overview():
                if not r["scheduled"]:
                    print(f"\n{r['company_id']}: {r['name']}: NOT scheduled ({r['reason']})")
                    continue
                print(f"\n{r['company_id']}: {r['name']} (tz {r['settings']['timezone']})")
                print(f"  next brief: {r['next_brief'] or 'disabled'}   "
                      f"next audit: {r['next_audit'] or 'disabled'}   "
                      f"monitor every {r['settings']['crawl_every_hours']}h"
                      f"{'' if r['settings']['monitor_enabled'] else ' (disabled)'}")
                for job, last in r["last_runs"].items():
                    print(f"  last {job}: " + (f"{last['status']} at {last['started_at'][:19]}"
                                               if last else "never"))
        elif cmd == "settings":
            for k, v in sc.get_settings(int(a[1])).items():
                print(f"  {k}: {v}")
        elif cmd == "set":
            changes = dict(x.split("=", 1) for x in a[2:])
            for k, v in sc.update_settings(int(a[1]), **changes).items():
                print(f"  {k}: {v}")
        elif cmd == "brief-in":
            s = sc.set_brief_in(int(a[1]), int(a[2]))
            print(f"weekly brief now scheduled for weekday {s['brief_weekday']} at "
                  f"{s['brief_time']} ({s['timezone']}). Start the worker: python worker.py")
        elif cmd == "run-now":
            r = sc.run_job(int(a[1]), a[2], "manual")
            if r is None:
                print("that job is already running")
            else:
                print(f"run {r['run_id']}: {r['status']} {r['error']}")
                print("summary:", r["summary"])
        elif cmd == "tick":
            results = sc.tick()
            print(f"{len(results)} job(s) ran")
            for r in results:
                print(f"  company {r['company_id']} {r['job_type']}: {r['status']} {r['error']}")
        elif cmd == "runs":
            cid = int(a[1]) if len(a) > 1 else None
            show_runs(sc.list_job_runs(cid, int(a[2]) if len(a) > 2 else 20))
        elif cmd == "failures":
            cid = int(a[1]) if len(a) > 1 else None
            show_runs(sc.list_job_runs(cid, 30, ("failed", "partial", "missed")))
        elif cmd == "audit-report":
            rep = get_latest_audit(int(a[1]))
            print(rep["markdown"] if rep else "no audit has been run yet")
        else:
            print(USAGE)
    except (LLMError, KeyError, ValueError, IndexError) as e:
        print(f"error: {e}")


if __name__ == "__main__":
    main()