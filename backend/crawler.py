from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from backend import parsers as P
from backend.db import connect, init_db
from backend.extract import extract_page
from backend.fetcher import PoliteFetcher
from backend.sources import canonical_url

MAX_AGE_DAYS = 90       
MIN_CHANGE_CHARS = 10   


@dataclass
class Outcome:
    status: str        
    seen: int = 0
    new: int = 0
    duplicate: int = 0
    old: int = 0
    note: str = ""


@dataclass
class CrawlSummary:
    run_id: int
    status: str
    sources_checked: int = 0
    new_records: int = 0
    duplicates: int = 0
    errors: int = 0
    error: str = ""


def _url_key(url: str) -> str:
    try:
        return canonical_url(url)
    except ValueError:
        return url.strip().lower()


def _insert_record(conn, src: dict, run_id: int, it: dict, now_iso: str) -> tuple[int, bool]:
    url_key = it.get("url_key") or _url_key(it["url"])
    chash = it.get("content_hash") or P.text_hash(it["title"], it["text"][:300])
    cur = conn.execute(
        "INSERT OR IGNORE INTO records (company_id, source_id, competitor_id, crawl_run_id, "
        "category, kind, title, url, publisher, text, url_key, content_hash, published_at, "
        "discovered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (src["company_id"], src["id"], src["competitor_id"], run_id, src["category"],
         it["kind"], it["title"], it["url"], it["publisher"], it["text"], url_key, chash,
         it["published_at"], now_iso))
    if cur.rowcount:
        rid, is_new = cur.lastrowid, True
    else:  
        row = conn.execute("SELECT id FROM records WHERE company_id = ? AND "
                           "(url_key = ? OR content_hash = ?) LIMIT 1",
                           (src["company_id"], url_key, chash)).fetchone()
        rid, is_new = row["id"], False
    conn.execute("INSERT OR IGNORE INTO record_sightings (record_id, source_id, seen_at) "
                 "VALUES (?, ?, ?)", (rid, src["id"], now_iso))
    return rid, is_new


def _persist(src, run_id, items, now, empty_note="") -> Outcome:
    cutoff = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()
    out = Outcome("no_new")
    with connect() as conn:
        for it in items:
            out.seen += 1
            if it["published_at"] and it["published_at"] < cutoff:
                out.old += 1
                continue
            _, is_new = _insert_record(conn, src, run_id, it, now.isoformat())
            if is_new:
                out.new += 1
            else:
                out.duplicate += 1
    out.status = "new_items" if out.new else "no_new"
    if not items:
        out.note = empty_note
    elif out.old:
        out.note = f"{out.old} item(s) older than {MAX_AGE_DAYS} days skipped"
    return out


def _fetch_problem(res) -> Outcome | None:
    if res.blocked_by_robots:
        return Outcome("blocked", note="blocked by robots.txt")
    if not res.ok:
        return Outcome("error", note=res.error or f"HTTP {res.status}")
    return None


def _crawl_feed(fetcher, src, run_id, now) -> Outcome:
    res = fetcher.fetch(src["url"])
    if (bad := _fetch_problem(res)):
        return bad
    items = P.parse_feed(res.text, news=src["type"] == "news_query", now=now)
    return _persist(src, run_id, items, now, "feed returned no items")


def _crawl_hn(fetcher, src, run_id, now) -> Outcome:
    res = fetcher.fetch(src["url"])
    if (bad := _fetch_problem(res)):
        return bad
    items = P.parse_hn(res.text)
    note = ""
    if src["category"] == "customers":      
        cres = fetcher.fetch(src["url"].replace("tags=story", "tags=comment"))
        if cres.ok:
            items += P.parse_hn(cres.text, comments=True)
        else:
            note = f"comment search failed: {cres.error or 'blocked by robots.txt'}"
    out = _persist(src, run_id, items, now, "no matching stories or comments")
    out.note = out.note or note
    return out


def _crawl_page(fetcher, src, run_id, now) -> Outcome:
    res = fetcher.fetch(src["url"])
    if (bad := _fetch_problem(res)):
        return bad
    page = extract_page(res.text, res.final_url, max_chars=30000)
    if page.low_content:
        return Outcome("error", note="page returned almost no text; skipped to avoid "
                                     "reporting a false change")
    new_hash = P.text_hash(page.text)
    now_iso = now.isoformat()

    def snapshot(conn):
        conn.execute("INSERT INTO page_snapshots (source_id, crawl_run_id, content_hash, text, "
                     "fetched_at) VALUES (?, ?, ?, ?, ?)",
                     (src["id"], run_id, new_hash, page.text, now_iso))

    with connect() as conn:
        prev = conn.execute("SELECT content_hash, text FROM page_snapshots WHERE source_id = ? "
                            "ORDER BY id DESC LIMIT 1", (src["id"],)).fetchone()
        if prev is None:
            snapshot(conn)
            return Outcome("baseline", note="first snapshot stored; changes are detected "
                                            "from the next crawl")
        if prev["content_hash"] == new_hash:
            return Outcome("unchanged")
        added, removed = P.page_diff(prev["text"], page.text)
        snapshot(conn)                     
        if len("".join(added + removed)) < MIN_CHANGE_CHARS:
            return Outcome("unchanged", note="only minor or volatile lines changed "
                                             "(dates, times, copyright)")
        recent = conn.execute("SELECT status FROM crawl_source_log WHERE source_id = ? "
                              "ORDER BY id DESC LIMIT 2", (src["id"],)).fetchall()
        noisy = len(recent) == 2 and all(r["status"] == "changed" for r in recent)
        item = {
            "title": f"Page changed: {page.title or src['target']}",
            "url": res.final_url, "text": P.format_change(added, removed),
            "published_at": "", "publisher": urlparse(res.final_url).netloc, "kind": "page_change",
            "url_key": f"{_url_key(res.final_url)}|change|{new_hash[:16]}",
            "content_hash": P.text_hash("pagechange", str(src["id"]), new_hash),
        }
        _, is_new = _insert_record(conn, src, run_id, item, now_iso)
    note = f"+{len(added)} / -{len(removed)} lines"
    if noisy:
        note += "; changed in 3 consecutive crawls, possibly dynamic content"
    return Outcome("changed", seen=1, new=int(is_new), duplicate=int(not is_new), note=note)


HANDLERS = {"rss": _crawl_feed, "news_query": _crawl_feed,
            "hn_query": _crawl_hn, "web_page": _crawl_page}


# ---- run orchestration ----
def _log(run_id: int, source_id: int, out: Outcome, now: datetime) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO crawl_source_log (run_id, source_id, status, items_seen, items_new, "
            "items_duplicate, items_old, note) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, source_id, out.status, out.seen, out.new, out.duplicate, out.old,
             out.note[:500]))
        conn.execute("UPDATE sources SET checked_at = ?, items_found = CASE WHEN ? > 0 "
                     "THEN ? ELSE items_found END WHERE id = ?",
                     (now.isoformat(), out.seen, out.seen, source_id))


def _finish(s: CrawlSummary) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE crawl_runs SET status=?, finished_at=?, sources_checked=?, new_records=?, "
            "duplicates=?, errors=?, error=? WHERE id=?",
            (s.status, datetime.now(timezone.utc).isoformat(), s.sources_checked,
             s.new_records, s.duplicates, s.errors, s.error[:500], s.run_id))


def crawl_company(company_id: int, fetcher=None, run_type: str = "manual",
                  source_ids: list[int] | None = None,
                  now: datetime | None = None) -> CrawlSummary:
    """Crawl every approved external source. One failing source never stops the run."""
    init_db()
    now = now or datetime.now(timezone.utc)
    with connect() as conn:
        if conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise KeyError(f"no company {company_id}")
        srcs = [dict(r) for r in conn.execute(
            "SELECT * FROM sources WHERE company_id = ? AND status = 'approved' "
            "AND type != 'upload' ORDER BY id", (company_id,))]
        run_id = conn.execute("INSERT INTO crawl_runs (company_id, run_type, started_at) "
                              "VALUES (?, ?, ?)", (company_id, run_type, now.isoformat())).lastrowid
    if source_ids is not None:
        srcs = [s for s in srcs if s["id"] in set(source_ids)]

    summary = CrawlSummary(run_id=run_id, status="running")
    if not srcs:
        summary.status, summary.error = "failed", "no approved sources to crawl"
        _finish(summary)
        return summary

    own = fetcher is None
    fetcher = fetcher or PoliteFetcher()
    try:
        for src in srcs:
            try:
                out = HANDLERS[src["type"]](fetcher, src, run_id, now)
            except Exception as e:      
                out = Outcome("error", note=f"{type(e).__name__}: {e}")
            _log(run_id, src["id"], out, now)
            summary.sources_checked += 1
            summary.new_records += out.new
            summary.duplicates += out.duplicate
            summary.errors += out.status in ("error", "blocked")
        if summary.errors == summary.sources_checked:
            summary.status, summary.error = "failed", "every source failed"
        else:
            summary.status = "partial" if summary.errors else "ok"
    except Exception as e:
        summary.status, summary.error = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        if own:
            fetcher.close()
        _finish(summary)
    return summary


def list_runs(company_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM crawl_runs WHERE company_id = ? ORDER BY id DESC LIMIT ?",
            (company_id, limit))]


def get_run_log(run_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT l.*, s.target, s.type, s.category FROM crawl_source_log l "
            "JOIN sources s ON s.id = l.source_id WHERE l.run_id = ? ORDER BY l.id", (run_id,))]


def get_records(company_id: int, limit: int = 50, category: str | None = None) -> list[dict]:
    sql = ("SELECT r.*, c.name AS competitor_name, "
           "(SELECT COUNT(*) FROM record_sightings x WHERE x.record_id = r.id) AS n_sightings "
           "FROM records r LEFT JOIN competitors c ON c.id = r.competitor_id "
           "WHERE r.company_id = ?")
    args: list = [company_id]
    if category:
        sql += " AND r.category = ?"
        args.append(category)
    sql += " ORDER BY r.discovered_at DESC, r.id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]