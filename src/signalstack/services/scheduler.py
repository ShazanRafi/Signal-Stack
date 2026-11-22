import json
import sqlite3
from datetime import datetime, timedelta, timezone

from signalstack.infrastructure import config
from signalstack.services import audit, jobs
from signalstack.domain import schedule as T
from signalstack.infrastructure.db import connect, init_db

JOB_TYPES = ("weekly_brief", "monitor", "monthly_audit")
JOBS = {"weekly_brief": jobs.run_weekly_pipeline, "monitor": jobs.run_monitor,
        "monthly_audit": audit.run_monthly_audit}

DEFAULTS = {"timezone": "UTC", "brief_enabled": 1, "brief_weekday": 0, "brief_time": "08:00",
            "brief_days": 7, "monitor_enabled": 1, "crawl_every_hours": 12,
            "audit_enabled": 1, "audit_day": 1, "audit_time": "06:00"}
_RANGES = {"brief_enabled": (0, 1), "monitor_enabled": (0, 1), "audit_enabled": (0, 1),
           "brief_weekday": (0, 6), "brief_days": (1, 31), "crawl_every_hours": (1, 168),
           "audit_day": (1, 28)}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str):
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _validate(key: str, value):
    if key == "timezone":
        T.get_tz(str(value))
        return str(value)
    if key in ("brief_time", "audit_time"):
        return T.validate_hhmm(str(value))
    n = int(value)
    lo, hi = _RANGES[key]
    if not lo <= n <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return n


def _ensure_settings(conn, cid: int) -> None:
    if conn.execute("SELECT 1 FROM companies WHERE id = ?", (cid,)).fetchone() is None:
        raise KeyError(f"no company {cid}")
    conn.execute("INSERT OR IGNORE INTO company_settings (company_id, effective_from) "
                 "VALUES (?, ?)", (cid, _iso(_utcnow())))


def get_settings(company_id: int) -> dict:
    init_db()
    with connect() as conn:
        _ensure_settings(conn, company_id)
        return dict(conn.execute("SELECT * FROM company_settings WHERE company_id = ?",
                                 (company_id,)).fetchone())


def update_settings(company_id: int, **changes) -> dict:
    """Validate and save. Slots before this moment are ignored, so changing a schedule
    never produces false 'missed' runs."""
    init_db()
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    clean = {k: _validate(k, v) for k, v in changes.items()}
    with connect() as conn:
        _ensure_settings(conn, company_id)
        for k, v in clean.items():        
            conn.execute(f"UPDATE company_settings SET {k} = ? WHERE company_id = ?",
                         (v, company_id))
        conn.execute("UPDATE company_settings SET effective_from = ? WHERE company_id = ?",
                     (_iso(_utcnow()), company_id))
    return get_settings(company_id)


def set_brief_in(company_id: int, minutes: int) -> dict:
    if minutes < 1:
        raise ValueError("minutes must be at least 1")
    tz = T.get_tz(get_settings(company_id)["timezone"])
    target = (_utcnow() + timedelta(minutes=minutes)).astimezone(tz)
    return update_settings(company_id, brief_enabled=1, brief_weekday=target.weekday(),
                           brief_time=target.strftime("%H:%M"))


def _ineligible_reason(conn, cid: int) -> str | None:
    n = conn.execute("SELECT COUNT(*) FROM sources WHERE company_id = ? AND "
                     "status = 'approved' AND type != 'upload'", (cid,)).fetchone()[0]
    return None if n else "no approved external sources"


def _occurrence_attempt(conn, cid, job, occ, effective_from, now) -> int | None:
    if occ < effective_from:
        return None
    grace = timedelta(hours=config.SCHEDULE_GRACE_HOURS)
    runs = conn.execute("SELECT status, finished_at FROM job_runs WHERE company_id = ? AND "
                        "job_type = ? AND scheduled_for = ? ORDER BY id DESC",
                        (cid, job, _iso(occ))).fetchall()
    if not runs:
        if now - occ > grace:
            conn.execute(
                "INSERT INTO job_runs (company_id, job_type, triggered_by, scheduled_for, "
                "attempt, status, started_at, finished_at, error) "
                "VALUES (?, ?, 'scheduled', ?, 0, 'missed', ?, ?, ?)",
                (cid, job, _iso(occ), _iso(now), _iso(now),
                 f"no run happened within {config.SCHEDULE_GRACE_HOURS}h of the scheduled "
                 "time (was the worker running?)"))
            return None
        return 1
    last = runs[0]
    if last["status"] != "failed" or len(runs) >= config.RUN_MAX_ATTEMPTS or now - occ > grace:
        return None
    fin = _parse(last["finished_at"])
    if fin and now - fin >= timedelta(minutes=config.RUN_RETRY_MINUTES):
        return len(runs) + 1
    return None


def _monitor_due(conn, cid, s, now) -> bool:
    row = conn.execute("SELECT started_at FROM job_runs WHERE company_id = ? AND job_type IN "
                       "('monitor', 'weekly_brief') AND status != 'missed' "
                       "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if row is None:
        return True
    last = _parse(row["started_at"])
    return last is None or now - last >= timedelta(hours=s["crawl_every_hours"])


def _due(conn, cid, job, s, now):
    if job == "monitor":
        return ("", 1) if s["monitor_enabled"] and _monitor_due(conn, cid, s, now) else None
    if job == "weekly_brief":
        if not s["brief_enabled"]:
            return None
        occ = T.latest_weekly(now, s["brief_weekday"], s["brief_time"], s["timezone"])
    else:
        if not s["audit_enabled"]:
            return None
        occ = T.latest_monthly(now, s["audit_day"], s["audit_time"], s["timezone"])
    attempt = _occurrence_attempt(conn, cid, job, occ,
                                  _parse(s["effective_from"]) or EPOCH, now)
    return (_iso(occ), attempt) if attempt else None


def run_job(company_id: int, job_type: str, triggered_by: str = "manual",
            scheduled_for: str = "", attempt: int = 1, fetcher=None, llm_fn=None):
    """Run one job with a logged row. Returns None if the same job is already running."""
    init_db()
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type: {job_type}")
    with connect() as conn:
        if conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise KeyError(f"no company {company_id}")
    try:
        with connect() as conn:
            run_id = conn.execute(
                "INSERT INTO job_runs (company_id, job_type, triggered_by, scheduled_for, "
                "attempt, status, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (company_id, job_type, triggered_by, scheduled_for, attempt,
                 _iso(_utcnow()))).lastrowid
    except sqlite3.IntegrityError:
        return None
    try:
        kwargs = {"fetcher": fetcher, "llm_fn": llm_fn}
        if job_type == "weekly_brief":
            kwargs["days"] = get_settings(company_id)["brief_days"]
        out = JOBS[job_type](company_id, triggered_by, **kwargs)
        status, summary, error = out.status, out.summary, out.error
    except Exception as e:
        status, summary, error = "failed", {}, f"{type(e).__name__}: {e}"[:500]
    with connect() as conn:
        conn.execute("UPDATE job_runs SET status = ?, finished_at = ?, summary = ?, error = ? "
                     "WHERE id = ?", (status, _iso(_utcnow()),
                                      json.dumps(summary, default=str), error[:500], run_id))
    return {"run_id": run_id, "status": status, "summary": summary, "error": error}


def recover_stale_runs(now: datetime | None = None) -> int:
    now = now or _utcnow()
    cutoff = _iso(now - timedelta(minutes=config.STALE_RUN_MINUTES))
    with connect() as conn:
        return conn.execute(
            "UPDATE job_runs SET status = 'failed', finished_at = ?, error = ? "
            "WHERE status = 'running' AND started_at < ?",
            (_iso(now), f"worker stopped or crashed during this run (marked failed after "
                        f"{config.STALE_RUN_MINUTES} minutes)", cutoff)).rowcount


def _heartbeat(now: datetime, note: str = "") -> None:
    with connect() as conn:
        conn.execute("INSERT INTO worker_heartbeat (id, last_seen, note) VALUES (1, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, "
                     "note = excluded.note", (_iso(now), note))


def tick(now: datetime | None = None, fetcher=None, llm_fn=None) -> list[dict]:
    init_db()
    now = now or _utcnow()
    _heartbeat(now, "tick")
    recover_stale_runs(now)
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM companies ORDER BY id")]
    results = []
    for cid in ids:
        with connect() as conn:
            if _ineligible_reason(conn, cid):
                continue
        s = get_settings(cid)
        for job in JOB_TYPES:
            with connect() as conn:
                due = _due(conn, cid, job, s, now)
            if due is None:
                continue
            r = run_job(cid, job, "scheduled", due[0], due[1], fetcher=fetcher, llm_fn=llm_fn)
            if r:
                results.append({"company_id": cid, "job_type": job, **r})
    return results


def list_job_runs(company_id: int | None = None, limit: int = 30,
                  statuses: tuple | None = None) -> list[dict]:
    sql = ("SELECT j.*, c.name AS company_name FROM job_runs j "
           "JOIN companies c ON c.id = j.company_id WHERE 1 = 1")
    args: list = []
    if company_id is not None:
        sql += " AND j.company_id = ?"
        args.append(company_id)
    if statuses:
        sql += f" AND j.status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    sql += " ORDER BY j.id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def worker_status(now: datetime | None = None) -> dict:
    now = now or _utcnow()
    with connect() as conn:
        row = conn.execute("SELECT last_seen FROM worker_heartbeat WHERE id = 1").fetchone()
    seen = _parse(row["last_seen"]) if row else None
    if seen is None:
        return {"seen": False, "alive": False, "last_seen": ""}
    age = (now - seen).total_seconds()
    return {"seen": True, "alive": age <= 3 * config.SCHEDULER_POLL_SECONDS,
            "last_seen": row["last_seen"], "age_seconds": int(age)}


def schedule_overview(now: datetime | None = None) -> list[dict]:
    init_db()
    now = now or _utcnow()
    with connect() as conn:
        companies = [dict(r) for r in conn.execute("SELECT id, name FROM companies ORDER BY id")]
        reasons = {c["id"]: _ineligible_reason(conn, c["id"]) for c in companies}
    out = []
    for c in companies:
        row = {"company_id": c["id"], "name": c["name"], "scheduled": reasons[c["id"]] is None,
               "reason": reasons[c["id"]] or ""}
        if row["scheduled"]:
            s = get_settings(c["id"])
            row["settings"] = s
            row["next_brief"] = _iso(T.next_weekly(now, s["brief_weekday"], s["brief_time"],
                                                   s["timezone"])) if s["brief_enabled"] else ""
            row["next_audit"] = _iso(T.next_monthly(now, s["audit_day"], s["audit_time"],
                                                    s["timezone"])) if s["audit_enabled"] else ""
            with connect() as conn:
                row["last_runs"] = {}
                for job in JOB_TYPES:
                    r = conn.execute("SELECT status, started_at, error FROM job_runs WHERE "
                                     "company_id = ? AND job_type = ? AND status != 'missed' "
                                     "ORDER BY id DESC LIMIT 1", (c["id"], job)).fetchone()
                    row["last_runs"][job] = dict(r) if r else None
        out.append(row)
    return out