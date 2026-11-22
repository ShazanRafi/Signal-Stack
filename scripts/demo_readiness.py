import sys

from signalstack.infrastructure.db import connect, init_db

MEETUNIVERSITY = "meetuniversity"
MIN_COMPANIES = 3
MIN_INDUSTRIES = 3
MIN_COMPETITORS = 5
MAX_COMPETITORS = 10
MIN_MEETUNIVERSITY_SOURCES = 20
MIN_RECORDS = 30


def _check(label: str, passed: bool, detail: str) -> bool:
    print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def main() -> int:
    try:
        company_ids = [int(value) for value in sys.argv[1:]]
    except ValueError:
        print("usage: python scripts\\demo_readiness.py [COMPANY_ID ...]")
        return 2

    init_db()
    with connect() as conn:
        if company_ids:
            placeholders = ",".join("?" for _ in company_ids)
            companies = [dict(r) for r in conn.execute(
                f"SELECT id, name, industry FROM companies WHERE id IN ({placeholders}) "
                "ORDER BY id", company_ids)]
        else:
            companies = [dict(r) for r in conn.execute(
                "SELECT id, name, industry FROM companies ORDER BY id")]

        for company in companies:
            cid = company["id"]
            company["core_topics"] = conn.execute(
                "SELECT COUNT(DISTINCT topic) FROM interview_turns WHERE company_id = ? "
                "AND is_followup = 0 AND status IN ('answered', 'skipped')", (cid,)).fetchone()[0]
            company["competitors"] = conn.execute(
                "SELECT COUNT(*) FROM competitors WHERE company_id = ? AND status = 'active'",
                (cid,)).fetchone()[0]
            company["approved_sources"] = conn.execute(
                "SELECT COUNT(*) FROM sources WHERE company_id = ? AND status = 'approved' "
                "AND type != 'upload'", (cid,)).fetchone()[0]
            company["records"] = conn.execute(
                "SELECT COUNT(*) FROM records WHERE company_id = ?", (cid,)).fetchone()[0]
            company["cited_briefs"] = conn.execute(
                "SELECT COUNT(*) FROM briefs WHERE company_id = ? AND status IN "
                "('complete', 'data_only') AND n_signals > 0", (cid,)).fetchone()[0]
            company["audits"] = conn.execute(
                "SELECT COUNT(*) FROM audit_reports WHERE company_id = ?", (cid,)).fetchone()[0]

    passed = True
    print("Demo readiness (this checks database counts only; inspect source evidence manually).")
    if not companies:
        print("[FAIL] No companies selected.")
        return 1

    for company in companies:
        print(
            f"{company['id']}: {company['name']} | industry={company['industry'] or 'unset'}"
            f" | interview={company['core_topics']}/10"
            f" | competitors={company['competitors']}"
            f" | approved external sources={company['approved_sources']}"
            f" | records={company['records']}"
            f" | cited briefs={company['cited_briefs']}"
            f" | audits={company['audits']}"
        )

    meetuniversity = [c for c in companies
                      if c["name"].strip().casefold() == MEETUNIVERSITY]
    passed &= _check("MeetUniversity selected", len(meetuniversity) == 1,
                     f"found {len(meetuniversity)} matching company")
    if len(meetuniversity) == 1:
        company = meetuniversity[0]
        passed &= _check("MeetUniversity interview", company["core_topics"] == 10,
                         f"{company['core_topics']}/10 core topics completed or skipped")
        passed &= _check(
            "MeetUniversity competitors",
            MIN_COMPETITORS <= company["competitors"] <= MAX_COMPETITORS,
            f"{company['competitors']} active; target {MIN_COMPETITORS}-{MAX_COMPETITORS}")
        passed &= _check(
            "MeetUniversity approved sources",
            company["approved_sources"] >= MIN_MEETUNIVERSITY_SOURCES,
            f"{company['approved_sources']} approved; target at least "
            f"{MIN_MEETUNIVERSITY_SOURCES}")
        passed &= _check("MeetUniversity cited brief", company["cited_briefs"] >= 1,
                         f"{company['cited_briefs']} brief(s) with cited signals")

    industries = {c["industry"].strip().casefold() for c in companies if c["industry"].strip()}
    passed &= _check("Company configurations", len(companies) >= MIN_COMPANIES,
                     f"{len(companies)} selected; target at least {MIN_COMPANIES}")
    passed &= _check("Distinct industries", len(industries) >= MIN_INDUSTRIES,
                     f"{len(industries)} non-empty industry labels; target at least "
                     f"{MIN_INDUSTRIES}")
    incomplete_interviews = [c["name"] for c in companies if c["core_topics"] != 10]
    passed &= _check("Core interviews", not incomplete_interviews,
                     "all selected companies have 10 core topics" if not incomplete_interviews
                     else "incomplete: " + ", ".join(incomplete_interviews))
    missing_briefs = [c["name"] for c in companies if c["cited_briefs"] < 1]
    passed &= _check("Cited briefs", not missing_briefs,
                     "all selected companies have at least one cited brief" if not missing_briefs
                     else "missing: " + ", ".join(missing_briefs))

    record_total = sum(c["records"] for c in companies)
    passed &= _check("Collected records", record_total >= MIN_RECORDS,
                     f"{record_total} across selected companies; target at least {MIN_RECORDS}")
    audit_total = sum(c["audits"] for c in companies)
    passed &= _check("Monthly audit evidence", audit_total > 0,
                     f"{audit_total} stored audit report(s)")
    print("Record counts do not establish that data is genuine; verify dates, links, and "
          "captured evidence before submission.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
