from dataclasses import dataclass, field

from signalstack.services.analyzer import analyze_company
from signalstack.domain.brief import generate_brief
from signalstack.services.crawler import crawl_company

MAX_ANALYZE_ROUNDS = 4


@dataclass
class JobOutcome:
    status: str                       
    summary: dict = field(default_factory=dict)
    error: str = ""


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def _crawl_step(company_id, fetcher, run_type, steps, problems) -> str:
    try:
        c = crawl_company(company_id, fetcher=fetcher, run_type=run_type)
    except Exception as e:
        steps.append({"step": "crawl", "status": "exception", "error": _err(e)})
        problems.append(f"crawl crashed ({_err(e)})")
        return "exception"
    steps.append({"step": "crawl", "status": c.status, "sources_checked": c.sources_checked,
                  "new_records": c.new_records, "duplicates": c.duplicates,
                  "errors": c.errors, "error": c.error})
    if c.status != "ok":
        detail = c.error or f"{c.errors} of {c.sources_checked} sources failed"
        problems.append(f"crawl {c.status} ({detail})")
    return c.status


def _analyze_step(company_id, llm_fn, steps, problems) -> None:
    total = {"analyzed": 0, "not_relevant": 0, "filtered": 0}
    remaining, rounds = 0, 0
    for _ in range(MAX_ANALYZE_ROUNDS):
        rounds += 1
        try:
            a = analyze_company(company_id, llm_fn=llm_fn)
        except Exception as e:
            problems.append(f"analysis crashed ({_err(e)})")
            break
        total["analyzed"] += a.analyzed
        total["not_relevant"] += a.not_relevant
        total["filtered"] += a.filtered
        remaining = a.remaining
        if a.errors:
            problems.append("analysis: " + a.errors[0][:200])
            break
        if a.remaining == 0:
            break
    else:
        problems.append(f"{remaining} record(s) still awaiting analysis after "
                        f"{MAX_ANALYZE_ROUNDS} rounds; the next run continues")
    steps.append({"step": "analyze", "rounds": rounds, **total, "remaining": remaining})


def _outcome(steps, problems, fatal=False, **extra) -> JobOutcome:
    status = "failed" if fatal else ("partial" if problems else "ok")
    return JobOutcome(status, {"steps": steps, **extra}, "; ".join(problems)[:500])


def run_monitor(company_id: int, triggered_by: str = "scheduled", fetcher=None,
                llm_fn=None) -> JobOutcome:
    run_type = "weekly" if triggered_by == "scheduled" else "manual"
    steps, problems = [], []
    crawl_status = _crawl_step(company_id, fetcher, run_type, steps, problems)
    _analyze_step(company_id, llm_fn, steps, problems)
    return _outcome(steps, problems, fatal=crawl_status in ("failed", "exception"))


def run_weekly_pipeline(company_id: int, triggered_by: str = "scheduled", days: int = 7,
                        fetcher=None, llm_fn=None) -> JobOutcome:
    run_type = "weekly" if triggered_by == "scheduled" else "manual"
    steps, problems = [], []
    _crawl_step(company_id, fetcher, run_type, steps, problems)
    _analyze_step(company_id, llm_fn, steps, problems)
    brief_id, brief_ok = None, True
    try:
        b = generate_brief(company_id, days=days, llm_fn=llm_fn, run_type=run_type)
        brief_id = b.brief_id
        steps.append({"step": "brief", "status": b.status, "brief_id": b.brief_id,
                      "signals_above_threshold": b.n_main, "sources_cited": b.n_cited})
        if b.status == "data_only":
            problems.append("brief was generated without AI synthesis (LLM unavailable)")
    except Exception as e:
        brief_ok = False
        steps.append({"step": "brief", "status": "exception", "error": _err(e)})
        problems.append(f"brief failed ({_err(e)})")
    return _outcome(steps, problems, fatal=not brief_ok, brief_id=brief_id)