import json
from datetime import datetime, timedelta, timezone

from backend import config
from backend import sources as S
from backend.db import connect, init_db
from backend.fetcher import PoliteFetcher
from backend.jobs import JobOutcome
from backend.llm import LLMError
from backend.stack import build_stack

FEED_TYPES = ("rss", "news_query", "hn_query")
FAIL_STATUSES = ("error", "blocked")


def _sqlite_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _yield(conn, source_id: int, start: datetime, end: datetime) -> tuple[int, int]:
    r = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN g.status = 'analyzed' "
        "AND g.relevance >= 3 THEN 1 ELSE 0 END), 0) AS rel "
        "FROM records r LEFT JOIN signals g ON g.record_id = r.id "
        "WHERE r.source_id = ? AND r.discovered_at >= ? AND r.discovered_at < ?",
        (source_id, start.isoformat(), end.isoformat())).fetchone()
    return r["n"], r["rel"]


def get_source_health(company_id: int, window_days: int | None = None,
                      now: datetime | None = None) -> list[dict]:
    window = window_days or config.AUDIT_WINDOW_DAYS
    now = now or datetime.now(timezone.utc)
    w_start, p_start = now - timedelta(days=window), now - timedelta(days=2 * window)
    end = now + timedelta(seconds=1)
    rows = []
    with connect() as conn:
        srcs = [dict(r) for r in conn.execute(
            "SELECT s.id, s.category, s.type, s.target, s.url, s.status, s.validation, "
            "s.validation_note, s.checked_at, c.name AS competitor_name FROM sources s "
            "LEFT JOIN competitors c ON c.id = s.competitor_id "
            "WHERE s.company_id = ? AND s.status IN ('approved', 'proposed') "
            "AND s.type != 'upload' ORDER BY s.id", (company_id,))]
        for s in srcs:
            logs = conn.execute(
                "SELECT items_new FROM crawl_source_log WHERE source_id = ? AND created_at >= ?",
                (s["id"], _sqlite_ts(w_start))).fetchall()
            last3 = [r["status"] for r in conn.execute(
                "SELECT status FROM crawl_source_log WHERE source_id = ? "
                "ORDER BY id DESC LIMIT 3", (s["id"],))]
            sh = conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM "
                "record_sightings o WHERE o.record_id = x.record_id AND o.source_id != "
                "x.source_id) THEN 1 ELSE 0 END), 0) AS shared FROM record_sightings x "
                "WHERE x.source_id = ? AND x.seen_at >= ?",
                (s["id"], w_start.isoformat())).fetchone()
            n_recent, rel_recent = _yield(conn, s["id"], w_start, end)
            n_prev, rel_prev = _yield(conn, s["id"], p_start, w_start)
            rows.append({
                **s, "window": window, "crawls": len(logs),
                "new_items": sum(r["items_new"] for r in logs),
                "failing_streak": len(last3) == 3 and all(x in FAIL_STATUSES for x in last3),
                "shared_total": sh["total"],
                "shared_share": sh["shared"] / sh["total"] if sh["total"] else 0.0,
                "n_recent": n_recent, "rel_recent": rel_recent,
                "n_prev": n_prev, "rel_prev": rel_prev})
    return rows


def assess_source(h: dict) -> list[dict]:
    flags = []

    def flag(name, action, reason):
        flags.append({"flag": name, "action": action, "reason": reason})

    bad = False
    if h["validation"] == "invalid":
        bad = True
        flag("dead", "remove", f"the re-check failed: {h['validation_note']}")
    elif h["failing_streak"]:
        bad = True
        flag("failing", "remove_or_replace", "the last 3 crawls all failed or were blocked")
    if h["crawls"] < config.AUDIT_MIN_CRAWLS:
        flag("insufficient_history", "none",
             f"only {h['crawls']} crawl(s) in the last {h['window']} days; not enough "
             "history to judge activity or relevance")
        return flags
    if h["type"] in FEED_TYPES and h["new_items"] == 0 and not bad:
        flag("inactive", "review",
             f"no new records in {h['window']} days across {h['crawls']} crawls")
    if h["shared_total"] >= config.AUDIT_MIN_ITEMS \
            and h["shared_share"] >= config.AUDIT_REDUNDANT_SHARE:
        flag("redundant", "review",
             f"{h['shared_share']:.0%} of its {h['shared_total']} items also arrive via other "
             "sources (keep at least one of the overlapping sources)")
    if h["n_prev"] >= config.AUDIT_MIN_ITEMS and h["n_recent"] >= config.AUDIT_MIN_ITEMS:
        y_prev, y_rec = h["rel_prev"] / h["n_prev"], h["rel_recent"] / h["n_recent"]
        if y_rec < 0.5 * y_prev:
            flag("declining_relevance", "review",
                 f"share of records judged relevant fell from {y_prev:.0%} to {y_rec:.0%}")
    elif h["n_recent"] >= 2 * config.AUDIT_MIN_ITEMS and h["rel_recent"] == 0:
        flag("low_yield", "review",
             f"{h['n_recent']} records collected, none judged relevant")
    return flags


def _bucket(flags: list[dict]) -> str:
    actions = {f["action"] for f in flags}
    if actions & {"remove", "remove_or_replace"}:
        return "removals"
    if "review" in actions:
        return "reviews"
    return "insufficient" if flags else "healthy"


def _item(h: dict, flags: list[dict]) -> str:
    who = f", {h['competitor_name']}" if h["competitor_name"] else ""
    lines = [f"- **[{h['id']}] {h['type']}**: {h['target'][:80]} ({h['category']}{who})"]
    lines += [f"  - {f['flag'].replace('_', ' ')}: {f['reason']}" for f in flags]
    lines.append(f"  - evidence: {h['crawls']} crawl(s), {h['new_items']} new record(s), "
                 f"{h['n_recent']} attributed record(s), {h['rel_recent']} judged relevant; "
                 f"last re-check: {h['validation']}")
    return "\n".join(lines)


def _render_report(name, now, window, groups, new_sources, n_checked, newly_dead, problems,
                   awaiting=0):
    out = [f"# Monthly Signal Stack Health Report: {name}",
           f"_Window: last {window} days · generated {now.strftime('%Y-%m-%d %H:%M')} UTC_",
           "## Summary",
           "\n".join([
               f"- Sources re-checked: {n_checked} (newly dead since the last check: {newly_dead})",
               f"- Recommended removals: {len(groups['removals'])} · needs review: "
               f"{len(groups['reviews'])} · no issues found: {len(groups['healthy'])} · "
               f"not enough history yet: {len(groups['insufficient'])}",
               f"- Newly discovered sources awaiting your approval: {len(new_sources)}",
               f"- Other proposed sources never approved (not judged): {awaiting}"])]
    if problems:
        out.append("**Problems during this audit:** " + "; ".join(problems))
    for key, title, empty in (
            ("removals", "Recommended removals", "None. Every source passed the re-check."),
            ("reviews", "Needs review", "None.")):
        out.append(f"## {title}")
        out.append("\n".join(_item(h, f) for h, f in groups[key]) or empty)
    out.append("## Recommended additions")
    if new_sources:
        out.append("Discovered this run and **not yet approved**:\n" + "\n".join(
            f"- **[{s['id']}] {s['type']}**: {s['target'][:80]} ({s['category']}, "
            f"{s['validation']})\n  - why: {s['reason'][:220]}" for s in new_sources))
    else:
        out.append("No new sources were discovered this run.")
    out.append("## Not enough history yet")
    out.append("\n".join(_item(h, f) for h, f in groups["insufficient"])
               or "Every source has enough crawl history to be judged.")
    out.append("## Method and limits")
    out.append("\n".join([
        f"- Each approved or proposed external source was re-fetched through the same "
        f"robots-respecting fetcher and re-validated; activity and relevance use the last "
        f"{window} days of crawl history.",
        "- Relevance is measured as the share of a source's records judged relevant (AI "
        "judgement, relevance 3/5 or higher), attributed to the first source that saw each record.",
        f"- Thresholds: at least {config.AUDIT_MIN_CRAWLS} crawls to judge, "
        f"{config.AUDIT_MIN_ITEMS} records for redundancy or relevance trends, redundancy "
        f"at {config.AUDIT_REDUNDANT_SHARE:.0%}.",
        "- Recommendations are never applied automatically. Removing or approving a source is "
        "your decision."]))
    return "\n\n".join(out)


def run_monthly_audit(company_id: int, triggered_by: str = "manual", fetcher=None,
                      llm_fn=None, window_days: int | None = None, discover: bool = True,
                      now: datetime | None = None) -> JobOutcome:
    init_db()
    now = now or datetime.now(timezone.utc)
    window = window_days or config.AUDIT_WINDOW_DAYS
    with connect() as conn:
        company = conn.execute("SELECT id, name FROM companies WHERE id = ?",
                               (company_id,)).fetchone()
        if company is None:
            raise KeyError(f"no company {company_id}")
        srcs = [dict(r) for r in conn.execute(
            "SELECT id, type, url, validation FROM sources WHERE company_id = ? "
            "AND status IN ('approved', 'proposed') AND type != 'upload' ORDER BY id",
            (company_id,))]
        max_src = conn.execute("SELECT COALESCE(MAX(id), 0) FROM sources").fetchone()[0]

    problems, newly_dead = [], 0
    own = fetcher is None
    fetcher = fetcher or PoliteFetcher()
    try:
        for s in srcs:
            try:
                v = S.validate_source(fetcher, s["type"], s["url"])
            except Exception as e:
                v = S.Validation("warning", f"re-check crashed: {type(e).__name__}")
            if v.status == "invalid" and s["validation"] != "invalid":
                newly_dead += 1
            with connect() as conn:
                conn.execute(
                    "UPDATE sources SET validation = ?, validation_note = ?, "
                    "items_found = CASE WHEN ? > 0 THEN ? ELSE items_found END, "
                    "last_item_at = CASE WHEN ? != '' THEN ? ELSE last_item_at END, "
                    "checked_at = ? WHERE id = ?",
                    (v.status, v.note, v.items, v.items, v.last_item_at, v.last_item_at,
                     now.isoformat(), s["id"]))
        if discover:
            try:
                build_stack(company_id, fetcher=fetcher, llm_fn=llm_fn)
            except LLMError as e:
                problems.append(f"discovery skipped (LLM): {str(e)[:150]}")
            except Exception as e:
                problems.append(f"discovery failed: {type(e).__name__}: {str(e)[:150]}")
    finally:
        if own:
            fetcher.close()

    with connect() as conn:
        new_sources = [dict(r) for r in conn.execute(
            "SELECT id, category, type, target, reason, validation FROM sources "
            "WHERE company_id = ? AND id > ? AND status = 'proposed' AND type != 'upload' "
            "ORDER BY id", (company_id, max_src))]

    groups = {"removals": [], "reviews": [], "insufficient": [], "healthy": []}
    awaiting = 0
    for h in get_source_health(company_id, window, now):
        if h["id"] > max_src:
            continue           
        flags = assess_source(h)
        if h["status"] == "proposed" and h["validation"] != "invalid":
            awaiting += 1      
            continue
        groups[_bucket(flags)].append((h, flags))
    markdown = _render_report(company["name"], now, window, groups, new_sources,
                              len(srcs), newly_dead, problems, awaiting)
    summary = {"sources_checked": len(srcs), "newly_dead": newly_dead,
               "removals": len(groups["removals"]), "reviews": len(groups["reviews"]),
               "insufficient_history": len(groups["insufficient"]),
               "healthy": len(groups["healthy"]), "new_sources": len(new_sources),
                "awaiting_approval": awaiting}
    with connect() as conn:
        report_id = conn.execute(
            "INSERT INTO audit_reports (company_id, period_start, period_end, markdown, "
            "summary_json) VALUES (?, ?, ?, ?, ?)",
            (company_id, (now - timedelta(days=window)).isoformat(), now.isoformat(),
             markdown, json.dumps(summary))).lastrowid
    return JobOutcome("partial" if problems else "ok", {"report_id": report_id, **summary},
                      "; ".join(problems)[:500])


def get_latest_audit(company_id: int) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM audit_reports WHERE company_id = ? "
                         "ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["summary"] = json.loads(d.pop("summary_json"))
    return d


def list_audits(company_id: int, limit: int = 12) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, period_start, period_end, created_at FROM audit_reports "
            "WHERE company_id = ? ORDER BY id DESC LIMIT ?", (company_id, limit))]