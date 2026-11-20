from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import db, scheduler
from backend.db import connect
from backend.jobs import JobOutcome

UTC = timezone.utc


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    clock = Clock(datetime(2026, 11, 23, 7, 0, tzinfo=UTC))  
    monkeypatch.setattr(scheduler, "_utcnow", clock)
    calls = []

    def install(job, status="ok", error="", raises=None):
        def fn(company_id, triggered_by, **kw):
            calls.append((job, company_id, triggered_by))
            if raises:
                raise raises
            return JobOutcome(status, {"n": 1}, error)
        monkeypatch.setitem(scheduler.JOBS, job, fn)

    for j in scheduler.JOB_TYPES:
        install(j)
    return SimpleNamespace(clock=clock, calls=calls, install=install)


def make_company(eligible=True, **settings):
    with connect() as conn:
        cid = conn.execute("INSERT INTO companies (name, url, status) VALUES "
                           "('Acme', 'https://acme.test/', 'stack_built')").lastrowid
        if eligible:
            conn.execute(
                "INSERT INTO sources (company_id, category, type, target, url, reason, status, "
                "dedupe_key) VALUES (?, 'market', 'rss', 'https://x.test/f', 'https://x.test/f', "
                "'r', 'approved', 'rss|x')", (cid,))
    if eligible:
        base = dict(monitor_enabled=0, audit_enabled=0)
        base.update(settings)
        scheduler.update_settings(cid, **base)
    return cid


def test_weekly_job_runs_once_at_its_slot(env):
    cid = make_company()
    assert scheduler.tick() == []                    
    env.clock.advance(hours=1, minutes=5)           
    results = scheduler.tick()
    assert [(r["job_type"], r["status"]) for r in results] == [("weekly_brief", "ok")]
    assert scheduler.tick() == []                   
    assert [c[0] for c in env.calls] == ["weekly_brief"]
    run = scheduler.list_job_runs(cid)[0]
    assert run["triggered_by"] == "scheduled" and run["scheduled_for"].startswith("2026-11-23T08:00")


def test_late_start_within_grace_still_runs(env):
    make_company()
    env.clock.advance(hours=5)                       
    assert len(scheduler.tick()) == 1


def test_missed_slot_is_logged_once_and_not_run_late(env):
    cid = make_company()
    env.clock.advance(hours=14, minutes=30)          
    assert scheduler.tick() == [] and scheduler.tick() == []
    missed = scheduler.list_job_runs(cid, statuses=("missed",))
    assert len(missed) == 1 and "worker" in missed[0]["error"]
    assert env.calls == []


def test_failed_run_is_retried_once_then_left_failed(env):
    cid = make_company()
    env.install("weekly_brief", raises=RuntimeError("boom"))
    env.clock.advance(hours=1, minutes=5)
    r = scheduler.tick()
    assert r[0]["status"] == "failed" and "boom" in r[0]["error"]
    env.clock.advance(minutes=10)
    assert scheduler.tick() == []                   
    env.clock.advance(minutes=25)                    
    r = scheduler.tick()
    assert len(r) == 1 and r[0]["status"] == "failed"
    env.clock.advance(hours=1)
    assert scheduler.tick() == []                   
    runs = scheduler.list_job_runs(cid, statuses=("failed",))
    assert len(runs) == 2 and {x["attempt"] for x in runs} == {1, 2}


def test_stale_running_row_is_marked_failed(env):
    cid = make_company()
    with connect() as conn:
        conn.execute("INSERT INTO job_runs (company_id, job_type, triggered_by, status, "
                     "started_at) VALUES (?, 'weekly_brief', 'scheduled', 'running', ?)",
                     (cid, scheduler._iso(env.clock() - timedelta(hours=3))))
    scheduler.tick()
    run = scheduler.list_job_runs(cid)[0]
    assert run["status"] == "failed" and "worker" in run["error"]


def test_same_job_cannot_run_twice_at_once(env):
    cid = make_company()
    with connect() as conn:
        conn.execute("INSERT INTO job_runs (company_id, job_type, triggered_by, status, "
                     "started_at) VALUES (?, 'weekly_brief', 'manual', 'running', ?)",
                     (cid, scheduler._iso(env.clock() - timedelta(minutes=5))))
    assert scheduler.run_job(cid, "weekly_brief") is None
    assert env.calls == []


def test_company_without_approved_sources_is_not_scheduled(env):
    cid = make_company(eligible=False)
    env.clock.advance(hours=2)
    assert scheduler.tick() == []
    row = next(r for r in scheduler.schedule_overview() if r["company_id"] == cid)
    assert row["scheduled"] is False and "no approved" in row["reason"]


def test_monitor_runs_on_its_interval(env):
    make_company(brief_enabled=0, monitor_enabled=1, crawl_every_hours=12)
    assert [r["job_type"] for r in scheduler.tick()] == ["monitor"]
    env.clock.advance(hours=11)
    assert scheduler.tick() == []
    env.clock.advance(hours=1, minutes=1)
    assert [r["job_type"] for r in scheduler.tick()] == ["monitor"]


def test_manual_run_is_logged_as_manual(env):
    cid = make_company()
    r = scheduler.run_job(cid, "monthly_audit")
    assert r["status"] == "ok"
    assert scheduler.list_job_runs(cid)[0]["triggered_by"] == "manual"


def test_brief_in_schedules_a_run_a_few_minutes_ahead(env):
    cid = make_company()
    s = scheduler.set_brief_in(cid, 2)
    assert (s["brief_weekday"], s["brief_time"]) == (0, "07:02")
    assert scheduler.tick() == []
    env.clock.advance(minutes=3)
    assert [r["job_type"] for r in scheduler.tick()] == ["weekly_brief"]


@pytest.mark.parametrize("change", [dict(timezone="Mars/Base"), dict(brief_time="25:00"),
                                    dict(audit_day=31), dict(brief_weekday=7),
                                    dict(crawl_every_hours=0), dict(nonsense=1)])
def test_invalid_settings_are_rejected(env, change):
    cid = make_company()
    with pytest.raises(ValueError):
        scheduler.update_settings(cid, **change)


def test_heartbeat_shows_worker_alive_then_stale(env):
    make_company()
    scheduler.tick()
    assert scheduler.worker_status()["alive"] is True
    env.clock.advance(minutes=10)
    assert scheduler.worker_status()["alive"] is False