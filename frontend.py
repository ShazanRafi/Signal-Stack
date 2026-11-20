import json

import streamlit as st

from backend import scheduler as sc
from backend.audit import assess_source, get_latest_audit, get_source_health
from backend.brief import (generate_brief, get_actions, get_brief, list_briefs,
                           set_action_status)
from backend.crawler import get_run_log, list_runs
from backend.db import connect, init_db
from backend.feed import competitor_options, get_feed, get_shared_records
from backend.internal import analyze_upload, get_findings
from backend.interview import (TOPICS, edit_profile_entry, get_state, retry_pending,
                               start_interview, submit_answer)
from backend.research import get_profile, research_company
from backend.stack import (add_source, approve_all_valid, build_stack, get_stack,
                           remove_competitor, set_source_status)
from backend.uploads import KINDS, ingest_bytes, list_uploads
from backend.reassess import apply_reassessment, get_change, list_changes, reassess_context

st.set_page_config(page_title="Signal Stack", layout="wide")
init_db()

CATS = ["customers", "competitors", "market", "internal"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
BADGE = {"threat": "🔴", "opportunity": "🟢", "neutral": "⚪"}


def run(label, fn, *a, **kw):
    try:
        with st.spinner(label):
            return True, fn(*a, **kw)
    except Exception as e:
        st.error(f"{type(e).__name__}: {e}")
        return False, None


def company_list():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name, url, status FROM companies ORDER BY id")]


def page_setup(cid):
    st.header("Company setup")
    with st.form("setup"):
        name = st.text_input("Company name")
        url = st.text_input("Website", placeholder="https://www.example.com")
        go = st.form_submit_button("Research company")
    if go:
        if not name.strip() or not url.strip():
            st.warning("Enter both a name and a website.")
        else:
            ok, r = run("Fetching the site and building the profile (1 LLM call)...",
                        research_company, name, url)
            if ok:
                st.success(f"Research {r.status}: {r.n_verified} verified facts, "
                           f"{r.n_assumptions} assumptions.")
                for lim in r.limitations:
                    st.warning(lim)
    if cid is None:
        return
    st.divider()
    st.subheader("Pipeline steps (for the selected company)")
    st.caption("Normal order: interview, build stack, approve sources, crawl, analyse, brief.")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Build Signal Stack"):
        ok, r = run("Discovering and validating sources (2 LLM calls, a few minutes)...",
                    build_stack, cid)
        if ok:
            st.success(f"{r.competitors} competitors, {r.proposed} sources proposed, "
                       f"{r.rejected} rejected, {r.duplicates} duplicates.")
            for n in r.notes:
                st.info(n)
    if c2.button("Approve all valid sources"):
        ok, n = run("Approving...", approve_all_valid, cid)
        if ok:
            st.success(f"Approved {n} sources.")
    if c3.button("Crawl now"):
        from backend.crawler import crawl_company
        ok, s = run("Crawling approved sources...", crawl_company, cid)
        if ok:
            st.success(f"Crawl {s.status}: {s.new_records} new records, {s.errors} errors. {s.error}")
    if c4.button("Analyse records"):
        from backend.analyzer import analyze_company
        ok, s = run("Analysing (LLM)...", analyze_company, cid)
        if ok:
            st.success(f"Analysed {s.analyzed}, not relevant {s.not_relevant}, "
                       f"filtered {s.filtered}, remaining {s.remaining}.")
            for e in s.errors:
                st.warning(e)


def page_profile(cid):
    st.header("Company intelligence profile")
    p = get_profile(cid)
    c = p["company"]
    st.subheader(f"{c['name']} · {c['industry'] or 'industry unknown'}")
    st.write(c["summary"] or "_No summary (research did not produce one)._")
    for lim in c["limitations"]:
        st.warning(lim)
    tab1, tab2, tab3 = st.tabs(["Website facts", "Interview notes (editable)", "Open questions"])
    with tab1:
        st.caption("Verified = the quote was found in the fetched page text. Assumption = unverified.")
        for status in ("verified", "assumption"):
            rows = [f for f in p["facts"] if f["status"] == status]
            st.markdown(f"**{status.title()} ({len(rows)})**")
            for f in rows:
                st.markdown(f"- [{f['category']}] {f['claim']}")
                if status == "verified":
                    st.caption(f"{f['source_url']} — \"{f['evidence']}\"")
                elif f["check_note"]:
                    st.caption(f["check_note"])
    with tab2:
        st.caption("Edits are never overwritten by the AI. Editing a skipped topic fills it in.")
        notes = {e["topic"]: e for e in get_state(cid)["entries"]}
        for topic, desc in TOPICS.items():
            cur = notes.get(topic, {})
            with st.expander(f"{topic.replace('_', ' ').title()}"
                             f"{' (edited)' if cur.get('edited_by_user') else ''}", expanded=False):
                st.caption(desc)
                new = st.text_area("Note", cur.get("content", ""), key=f"note_{cid}_{topic}")
                if st.button("Save", key=f"save_{cid}_{topic}"):
                    edit_profile_entry(cid, topic, new)
                    st.success("Saved.")
        ents = get_state(cid)["entities"]
        if ents:
            st.markdown("**Names you mentioned:** " + ", ".join(f"{e['name']} ({e['kind']})" for e in ents))
    with tab3:
        for q in c["open_questions"]:
            st.markdown(f"- {q}")


def page_interview(cid):
    st.header("AI interview")
    state = get_state(cid)
    if not state["turns"]:
        st.info("The interview is built from this company's research and your answers.")
        if st.button("Start interview"):
            ok, _ = run("Preparing the first question...", start_interview, cid)
            if ok:
                st.rerun()
        return
    st.progress(state["core_done"] / state["core_total"],
                text=f"{state['core_done']} of {state['core_total']} core topics covered")
    if state["pending"]:
        st.warning("Your last answer is saved, but processing it failed (often an LLM limit).")
        if st.button("Retry"):
            ok, _ = run("Retrying...", retry_pending, cid)
            if ok:
                st.rerun()
    elif state["current"]:
        cur = state["current"]
        st.subheader(cur["question"])
        with st.form(f"ans_{cur['id']}"):
            text = st.text_area("Your answer (a few full sentences gives better sources)")
            c1, c2 = st.columns(2)
            go = c1.form_submit_button("Submit answer")
            skip = c2.form_submit_button("Skip (confidential)")
        if go and not text.strip():
            st.warning("Type an answer or press Skip.")
        elif go or skip:
            ok, _ = run("Saving and preparing the next question...", submit_answer,
                        cid, cur["id"], text, skipped=skip)
            if ok:
                st.rerun()
    else:
        st.success("Interview complete. Review or edit notes on the Profile page.")
    with st.expander("Answers so far"):
        for t in state["turns"]:
            if t["status"] == "asked":
                continue
            st.markdown(f"**Q{t['seq']}{' (follow-up)' if t['is_followup'] else ''}:** {t['question']}")
            st.write(t["answer"] if t["status"] == "answered" else "_skipped_")


def _source_table(rows):
    st.dataframe([{"id": s["id"], "status": s["status"], "validation": s["validation"],
                   "tier": s["tier"], "type": s["type"], "target": s["target"][:80],
                   "competitor": s.get("competitor_name") or "", "why monitored": s["reason"],
                   "note": s["validation_note"]} for s in rows],
                 use_container_width=True, hide_index=True)


def page_stack(cid):
    st.header("Signal Stack")
    stack = get_stack(cid)
    if not stack["sources"]:
        st.info("No stack yet. Use 'Build Signal Stack' on the Setup page.")
        return
    tabs = st.tabs([c.title() for c in CATS])
    for tab, cat in zip(tabs, CATS):
        with tab:
            if cat == "competitors":
                st.markdown("**Competitors and substitutes**")
                st.dataframe([{"name": c["name"], "kind": c["kind"], "named by": c["named_by"],
                               "site check": c["site_status"], "url": c["url"],
                               "why relevant": c["why_relevant"], "status": c["status"]}
                              for c in stack["competitors"]],
                             use_container_width=True, hide_index=True)
            _source_table([s for s in stack["sources"] if s["category"] == cat and s["status"] != "removed"])
            if cat == "internal":
                _internal_uploads(cid)


def _internal_uploads(cid):
    st.markdown("**Upload internal data** (CSV, Excel, PDF or text)")
    st.caption("Uploaded text is sent to the Gemini API. Do not upload confidential data on a "
               "free tier. Label public datasets as proxies.")
    with st.form("upload"):
        kind = st.selectbox("What is it?", list(KINDS), format_func=lambda k: KINDS[k])
        f = st.file_uploader("File", type=["csv", "tsv", "xlsx", "pdf", "txt", "md"])
        cols = st.text_input("Text column(s), comma separated (blank = auto-detect)")
        note = st.text_input("Data origin note",
                             placeholder="PUBLIC PROXY DATASET: name, source. Not company data.")
        go = st.form_submit_button("Upload")
    if go and f is not None:
        ok, r = run("Parsing file...", ingest_bytes, cid, kind, f.name, f.getvalue(),
                    text_cols=[c.strip() for c in cols.split(",") if c.strip()] or None, note=note)
        if ok:
            st.success(f"Upload {r['upload_id']}: {r['rows_to_analyze']} rows queued. {r['notes']}")
    for u in list_uploads(cid):
        st.markdown(f"**Upload {u['id']}** · {u['filename']} · {u['status']} · "
                    f"{u['rows_analyzed']}/{u['rows_with_text']} rows · {u['note']}")
        if st.button("Analyse" if u["status"] == "uploaded" else "Re-analyse", key=f"an{u['id']}"):
            ok, r = run("Tagging rows (LLM, several calls)...", analyze_upload, u["id"])
            if ok:
                st.success(f"{r['themes']} themes, {r['records']} records created.")
                for e in r["errors"]:
                    st.warning(e)
        for fnd in get_findings(u["id"]):
            st.markdown(f"- [{fnd['category']}] {fnd['theme']}: {fnd['row_count']} rows")
            for q in fnd["quotes"]:
                st.caption(f"\"{q}\"")


def page_sources(cid):
    st.header("Source manager")
    stack = get_stack(cid)
    ext = [s for s in stack["sources"] if s["type"] != "upload"]
    show = st.multiselect("Show status", ["approved", "proposed", "rejected", "removed"],
                          default=["approved", "proposed", "rejected"])
    _source_table([s for s in ext if s["status"] in show])
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Change a source**")
        sid = st.selectbox("Source id", [s["id"] for s in ext]) if ext else None
        a, b, c = st.columns(3)
        if sid and a.button("Approve"):
            n = set_source_status(sid, "approved")
            st.success("Approved." if n else "Not changed (invalid sources cannot be approved).")
        if sid and b.button("Remove"):
            set_source_status(sid, "removed")
            st.success("Removed.")
        if sid and c.button("Back to proposed"):
            set_source_status(sid, "proposed")
        comps = [x for x in stack["competitors"] if x["status"] == "active"]
        if comps:
            pick = st.selectbox("Remove a competitor and its sources", comps,
                                format_func=lambda x: x["name"])
            if st.button("Remove competitor"):
                remove_competitor(pick["id"])
                st.success("Removed.")
    with c2:
        st.markdown("**Add a source** (validated before it is approved)")
        with st.form("addsrc"):
            cat = st.selectbox("Category", ["customers", "competitors", "market"])
            typ = st.selectbox("Type", ["news_query", "rss", "web_page", "hn_query"])
            target = st.text_input("Search phrase or URL")
            reason = st.text_input("Why monitor this?")
            go = st.form_submit_button("Validate and add")
        if go:
            ok, r = run("Validating...", add_source, cid, cat, typ, target, reason)
            if ok:
                st.info(f"{r['status']} · validation {r['validation']} · {r['note']}")
    st.divider()
    st.subheader("Source health (last 30 days)")
    health = []
    for h in get_source_health(cid):
        fl = assess_source(h)
        health.append({"id": h["id"], "target": h["target"][:60], "status": h["status"],
                       "crawls": h["crawls"], "new items": h["new_items"],
                       "overlap": f"{h['shared_share']:.0%}",
                       "flags": ", ".join(f["flag"] for f in fl) or "none"})
    st.dataframe(health, use_container_width=True, hide_index=True)
    st.subheader("Duplicates (stored once, seen via several sources)")
    st.dataframe(get_shared_records(cid), use_container_width=True, hide_index=True)
    st.subheader("Crawl history")
    runs = list_runs(cid)
    st.dataframe([{k: r[k] for k in ("id", "run_type", "status", "started_at", "sources_checked",
                                     "new_records", "duplicates", "errors", "error")} for r in runs],
                 use_container_width=True, hide_index=True)
    if runs:
        rid = st.selectbox("Show log for crawl run", [r["id"] for r in runs])
        st.dataframe([{"source": l["source_id"], "type": l["type"], "status": l["status"],
                       "new": l["items_new"], "dup": l["items_duplicate"], "note": l["note"]}
                      for l in get_run_log(rid)], use_container_width=True, hide_index=True)


def page_feed(cid):
    st.header("Intelligence feed")
    comps = competitor_options(cid)
    c1, c2, c3, c4 = st.columns(4)
    cat = c1.selectbox("Category", ["all"] + CATS)
    comp = c2.selectbox("Competitor", [None] + comps, format_func=lambda x: "all" if x is None else x["name"])
    days = c3.selectbox("Date range", [None, 7, 30, 90], format_func=lambda d: "all time" if d is None else f"last {d} days")
    sort = c4.selectbox("Sort", ["date", "priority"])
    d1, d2, d3, d4 = st.columns(4)
    rel = d1.slider("Min AI relevance", 1, 5, 1)
    imp = d2.slider("Min AI impact", 1, 5, 1)
    ver = d3.selectbox("Verification", [None, "verified", "reported", "unverified"], format_func=lambda v: v or "all")
    cls = d4.selectbox("Type", [None, "threat", "opportunity", "neutral"], format_func=lambda v: v or "all")
    rows = get_feed(cid, None if cat == "all" else cat, comp["id"] if comp else None, days,
                    rel, imp, ver, cls, sort)
    st.caption(f"{len(rows)} signal(s). Relevance and impact are AI judgements; 'verified' means "
               "the quote exists in the stored source text, not that the claim is true.")
    for g in rows:
        when = (g["published_at"] or g["discovered_at"])[:10]
        with st.expander(f"{BADGE[g['classification']]} {when} · {g['title'][:90]} · priority {g['priority']:.0f}"):
            st.markdown(f"**What happened:** {g['what_happened']}")
            st.markdown(f"**Evidence ({g['verification']}):** "
                        + (f"\"{g['evidence_snippet']}\"" if g["evidence_snippet"] else "none verified"))
            if g["customer_quote"]:
                st.markdown(f"**Customer said:** \"{g['customer_quote']}\"")
            st.markdown(f"**AI interpretation:** {g['why_it_matters']}")
            st.markdown(f"**Suggested action:** {g['suggested_action']}")
            st.caption(f"{g['explanation']}  \nSource: {g['publisher']} · {g['url']}")


def page_brief(cid):
    st.header("Monday brief")
    c1, c2 = st.columns(2)
    if c1.button("Run full pipeline now (crawl → analyse → brief)"):
        ok, r = run("Running the weekly pipeline (several minutes)...", sc.run_job, cid, "weekly_brief")
        if ok:
            st.info("Another run is already in progress." if r is None else f"{r['status']} {r['error']}")
    if c2.button("Generate brief from existing signals"):
        ok, r = run("Writing the brief (1 LLM call)...", generate_brief, cid)
        if ok:
            st.success(f"Brief {r.brief_id}: {r.status}")
    briefs = list_briefs(cid)
    if not briefs:
        st.info("No briefs yet.")
        return
    bid = st.selectbox("History", [b["id"] for b in briefs],
                       format_func=lambda i: next(f"#{b['id']} · {b['created_at'][:16]} · {b['status']} · {b['run_type']}"
                                                  for b in briefs if b["id"] == i))
    b = get_brief(bid)
    st.download_button("Download .md", b["markdown"], f"brief_{bid}.md", "text/markdown")
    try:
        import markdown as md
        st.download_button("Download email-ready HTML", md.markdown(b["markdown"]), f"brief_{bid}.html", "text/html")
    except ImportError:
        st.caption("Optional: pip install markdown to enable HTML export.")
    st.divider()
    st.markdown(b["markdown"])
    st.divider()
    st.subheader("Action tracker")
    for a in get_actions(cid):
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**P{a['priority']} · {a['owner']}:** {a['action']}  \n_{a['reason']}_")
        opts = ["todo", "doing", "done"]
        new = c2.selectbox("Status", opts, index=opts.index(a["status"]), key=f"act{a['id']}",
                           label_visibility="collapsed")
        if new != a["status"]:
            set_action_status(a["id"], new)
            st.rerun()


def page_settings(cid):
    st.header("Settings & health")
    w = sc.worker_status()
    st.markdown("**Scheduler worker:** " + ("🟢 running" if w["alive"] else
                f"🔴 not running (last seen {w['last_seen'] or 'never'}). Start it with `python worker.py`."))
    s = sc.get_settings(cid)
    with st.form("settings"):
        tz = st.text_input("Time zone", s["timezone"])
        c1, c2, c3 = st.columns(3)
        wd = c1.selectbox("Brief weekday", range(7), index=s["brief_weekday"], format_func=lambda i: DAYS[i])
        bt = c2.text_input("Briefing time (HH:MM)", s["brief_time"])
        bd = c3.number_input("Brief covers days", 1, 31, s["brief_days"])
        c4, c5, c6 = st.columns(3)
        ch = c4.number_input("Monitoring frequency (hours)", 1, 168, s["crawl_every_hours"])
        ad = c5.number_input("Monthly audit day (1-28)", 1, 28, s["audit_day"])
        at = c6.text_input("Audit time (HH:MM)", s["audit_time"])
        e1, e2, e3 = st.columns(3)
        be = e1.checkbox("Weekly brief enabled", bool(s["brief_enabled"]))
        me = e2.checkbox("Monitoring enabled", bool(s["monitor_enabled"]))
        ae = e3.checkbox("Monthly audit enabled", bool(s["audit_enabled"]))
        if st.form_submit_button("Save settings"):
            try:
                sc.update_settings(cid, timezone=tz, brief_weekday=wd, brief_time=bt, brief_days=bd,
                                   crawl_every_hours=ch, audit_day=ad, audit_time=at,
                                   brief_enabled=int(be), monitor_enabled=int(me), audit_enabled=int(ae))
                st.success("Saved.")
            except ValueError as e:
                st.error(str(e))
    st.subheader("Run a job now")
    b1, b2, b3 = st.columns(3)
    for col, job in zip((b1, b2, b3), sc.JOB_TYPES):
        if col.button(job.replace("_", " ").title()):
            ok, r = run(f"Running {job}...", sc.run_job, cid, job)
            if ok:
                st.info("Already running." if r is None else f"{r['status']} {r['error']}")
    st.subheader("Job runs (failures and gaps are visible here)")
    only = st.checkbox("Only failed / partial / missed")
    runs = sc.list_job_runs(cid, 40, ("failed", "partial", "missed") if only else None)
    st.dataframe([{k: r[k] for k in ("id", "job_type", "triggered_by", "status", "attempt",
                                     "started_at", "error")} for r in runs],
                 use_container_width=True, hide_index=True)
    st.subheader("Latest monthly source-health report")
    rep = get_latest_audit(cid)
    st.markdown(rep["markdown"] if rep else "_No audit has run yet._")

def page_context(cid):
    st.header("Change business context")
    st.caption("Edit a profile topic; the system reviews the Signal Stack against the new "
               "context. Nothing is removed until you apply the report.")
    topic = st.selectbox("Topic", list(TOPICS), format_func=lambda t: t.replace("_", " ").title())
    cur = {e["topic"]: e["content"] for e in get_state(cid)["entries"]}.get(topic, "")
    new = st.text_area("New note", cur, key=f"ctx_{cid}_{topic}")
    if st.button("Reassess stack (3 LLM calls, a few minutes)"):
        ok, r = run("Reviewing sources and discovering new ones...", reassess_context, cid, {topic: new})
        if ok:
            st.success(f"Change {r['change_id']} is ready.")
    for c in list_changes(cid):
        with st.expander(f"Change {c['id']} · {c['status']} · {c['created_at'][:16]}"):
            ch = get_change(c["id"])
            st.markdown(ch["markdown"] or ch["error"])
            if ch["status"] == "ready" and st.button("Apply this report", key=f"apply{c['id']}"):
                st.success(str(apply_reassessment(c["id"])))

PAGES = {"Setup": page_setup, "Profile": page_profile, "Interview": page_interview,
         "Signal Stack": page_stack, "Source Manager": page_sources,
         "Intelligence Feed": page_feed, "Monday Brief": page_brief,
         "Settings & Health": page_settings}

st.sidebar.title("Signal Stack")
cos = company_list()
cid = None
if cos:
    pick = st.sidebar.selectbox("Company", cos, format_func=lambda c: f"{c['name']} [{c['status']}]")
    cid = pick["id"]
page = st.sidebar.radio("Page", list(PAGES))
if cid is None and page != "Setup":
    st.info("Add a company on the Setup page first.")
else:
    PAGES[page](cid) if page != "Setup" else page_setup(cid)