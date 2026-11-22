from dataclasses import dataclass, field
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from signalstack.services.analyzer import analyze_company
from signalstack.domain.brief import generate_brief
from signalstack.services.crawler import crawl_company

MAX_ANALYZE_ROUNDS = 4


@dataclass
class JobOutcome:
    status: str                       
    summary: dict = field(default_factory=dict)
    error: str = ""


class PipelineState(TypedDict, total=False):
    company_id: int
    fetcher: object | None
    llm_fn: Callable | None
    job_type: str
    triggered_by: str
    run_type: str
    days: int
    audit_options: dict
    audit_outcome: JobOutcome
    include_brief: bool
    steps: list[dict]
    problems: list[str]
    crawl_status: str
    fatal: bool
    brief_id: int | None


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


def _crawl_node(state: PipelineState) -> dict:
    steps, problems = list(state.get("steps", [])), list(state.get("problems", []))
    status = _crawl_step(state["company_id"], state.get("fetcher"), state["run_type"],
                         steps, problems)
    return {"steps": steps, "problems": problems, "crawl_status": status}


def _analyze_node(state: PipelineState) -> dict:
    steps, problems = list(state.get("steps", [])), list(state.get("problems", []))
    _analyze_step(state["company_id"], state.get("llm_fn"), steps, problems)
    return {"steps": steps, "problems": problems}


def _brief_node(state: PipelineState) -> dict:
    steps, problems = list(state.get("steps", [])), list(state.get("problems", []))
    try:
        b = generate_brief(state["company_id"], days=state["days"],
                           llm_fn=state.get("llm_fn"), run_type=state["run_type"])
        steps.append({"step": "brief", "status": b.status, "brief_id": b.brief_id,
                      "signals_above_threshold": b.n_main, "sources_cited": b.n_cited})
        if b.status == "data_only":
            problems.append("brief was generated without AI synthesis (LLM unavailable)")
        return {"steps": steps, "problems": problems, "brief_id": b.brief_id}
    except Exception as e:
        steps.append({"step": "brief", "status": "exception", "error": _err(e)})
        problems.append(f"brief failed ({_err(e)})")
        return {"steps": steps, "problems": problems, "fatal": True}


def _audit_node(state: PipelineState) -> dict:
    from signalstack.services.audit import run_monthly_audit as run_audit

    return {
        "audit_outcome": run_audit(
            state["company_id"],
            triggered_by=state["triggered_by"],
            fetcher=state.get("fetcher"),
            llm_fn=state.get("llm_fn"),
            **state["audit_options"],
        )
    }


def _route_job(state: PipelineState) -> str:
    return "audit" if state["job_type"] == "monthly_audit" else "crawl"


def _after_analysis(state: PipelineState) -> str:
    return "brief" if state["include_brief"] else END


_pipeline_builder = StateGraph(PipelineState)
_pipeline_builder.add_node("crawl", _crawl_node)
_pipeline_builder.add_node("analyze", _analyze_node)
_pipeline_builder.add_node("brief", _brief_node)
_pipeline_builder.add_node("audit", _audit_node)
_pipeline_builder.add_conditional_edges(
    START, _route_job, {"crawl": "crawl", "audit": "audit"}
)
_pipeline_builder.add_edge("crawl", "analyze")
_pipeline_builder.add_conditional_edges(
    "analyze", _after_analysis, {"brief": "brief", END: END}
)
_pipeline_builder.add_edge("brief", END)
_pipeline_builder.add_edge("audit", END)
_pipeline = _pipeline_builder.compile()


def _run_pipeline(company_id: int, triggered_by: str, days: int,
                  fetcher, llm_fn, include_brief: bool) -> JobOutcome:
    run_type = "weekly" if triggered_by == "scheduled" else "manual"
    state = _pipeline.invoke({
        "company_id": company_id,
        "fetcher": fetcher,
        "llm_fn": llm_fn,
        "job_type": "weekly_brief" if include_brief else "monitor",
        "triggered_by": triggered_by,
        "run_type": run_type,
        "days": days,
        "include_brief": include_brief,
        "steps": [],
        "problems": [],
    })
    fatal = state.get("fatal", False)
    if not include_brief and state.get("crawl_status") in ("failed", "exception"):
        fatal = True
    extras = {"brief_id": state.get("brief_id")} if include_brief else {}
    return _outcome(state["steps"], state["problems"], fatal=fatal, **extras)


def run_monthly_audit(company_id: int, triggered_by: str = "manual", fetcher=None,
                      llm_fn=None, window_days: int | None = None, discover: bool = True,
                      now=None) -> JobOutcome:
    state = _pipeline.invoke({
        "company_id": company_id,
        "fetcher": fetcher,
        "llm_fn": llm_fn,
        "job_type": "monthly_audit",
        "triggered_by": triggered_by,
        "audit_options": {"window_days": window_days, "discover": discover, "now": now},
    })
    return state["audit_outcome"]


def run_monitor(company_id: int, triggered_by: str = "scheduled", fetcher=None,
                llm_fn=None) -> JobOutcome:
    return _run_pipeline(company_id, triggered_by, 7, fetcher, llm_fn, include_brief=False)


def run_weekly_pipeline(company_id: int, triggered_by: str = "scheduled", days: int = 7,
                        fetcher=None, llm_fn=None) -> JobOutcome:
    return _run_pipeline(company_id, triggered_by, days, fetcher, llm_fn, include_brief=True)