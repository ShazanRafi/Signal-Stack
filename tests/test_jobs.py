from types import SimpleNamespace as NS

from backend import jobs

CRAWL_OK = NS(status="ok", sources_checked=5, new_records=3, duplicates=1, errors=0, error="")
CRAWL_FAILED = NS(status="failed", sources_checked=5, new_records=0, duplicates=0, errors=5,
                  error="every source failed")
DONE = NS(analyzed=3, not_relevant=1, filtered=2, remaining=0, errors=[])
BRIEF_OK = NS(brief_id=7, status="complete", n_main=2, n_cited=2, notes=[])


def stub(monkeypatch, name, value):
    state = {"calls": 0}
    queue = list(value) if isinstance(value, list) else None

    def fn(*a, **k):
        state["calls"] += 1
        v = queue.pop(0) if queue is not None else value
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(jobs, name, fn)
    return state


def run(monkeypatch, which="run_weekly_pipeline", crawl=CRAWL_OK, analyze=DONE, brief=BRIEF_OK):
    st = {"crawl": stub(monkeypatch, "crawl_company", crawl),
          "analyze": stub(monkeypatch, "analyze_company", analyze),
          "brief": stub(monkeypatch, "generate_brief", brief)}
    return getattr(jobs, which)(1, "scheduled"), st


def names(out):
    return [s["step"] for s in out.summary["steps"]]


def test_everything_ok(monkeypatch):
    out, st = run(monkeypatch)
    assert out.status == "ok" and out.error == "" and out.summary["brief_id"] == 7
    assert names(out) == ["crawl", "analyze", "brief"]


def test_failed_crawl_is_partial_and_brief_still_generated(monkeypatch):
    out, st = run(monkeypatch, crawl=CRAWL_FAILED)
    assert out.status == "partial" and "crawl failed" in out.error
    assert st["brief"]["calls"] == 1


def test_crawl_crash_is_partial(monkeypatch):
    out, st = run(monkeypatch, crawl=RuntimeError("network down"))
    assert out.status == "partial" and "network down" in out.error
    assert st["brief"]["calls"] == 1


def test_analysis_continues_until_nothing_remains(monkeypatch):
    first = NS(analyzed=3, not_relevant=0, filtered=0, remaining=5, errors=[])
    second = NS(analyzed=3, not_relevant=0, filtered=0, remaining=0, errors=[])
    out, st = run(monkeypatch, analyze=[first, second])
    assert st["analyze"]["calls"] == 2 and out.status == "ok"
    assert next(s for s in out.summary["steps"] if s["step"] == "analyze")["analyzed"] == 6


def test_analysis_error_stops_and_is_reported(monkeypatch):
    bad = NS(analyzed=0, not_relevant=0, filtered=0, remaining=4, errors=["quota: x"])
    out, st = run(monkeypatch, analyze=bad)
    assert st["analyze"]["calls"] == 1 and out.status == "partial" and "quota" in out.error


def test_backlog_after_max_rounds_is_reported(monkeypatch):
    more = NS(analyzed=3, not_relevant=0, filtered=0, remaining=5, errors=[])
    out, st = run(monkeypatch, analyze=more)
    assert st["analyze"]["calls"] == jobs.MAX_ANALYZE_ROUNDS
    assert out.status == "partial" and "awaiting analysis" in out.error


def test_brief_failure_fails_the_job(monkeypatch):
    out, _ = run(monkeypatch, brief=RuntimeError("db locked"))
    assert out.status == "failed" and "db locked" in out.error


def test_data_only_brief_is_partial(monkeypatch):
    out, _ = run(monkeypatch, brief=NS(brief_id=9, status="data_only", n_main=1, n_cited=1, notes=[]))
    assert out.status == "partial" and "without AI synthesis" in out.error


def test_monitor_never_makes_a_brief_and_fails_when_crawl_fails(monkeypatch):
    out, st = run(monkeypatch, "run_monitor", crawl=CRAWL_FAILED)
    assert out.status == "failed" and st["brief"]["calls"] == 0
    out, st = run(monkeypatch, "run_monitor")
    assert out.status == "ok" and st["brief"]["calls"] == 0