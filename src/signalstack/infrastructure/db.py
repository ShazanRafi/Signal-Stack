import sqlite3
from contextlib import contextmanager

from signalstack.infrastructure.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    cached INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    industry TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    open_questions TEXT NOT NULL DEFAULT '[]',
    limitations TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    claim TEXT NOT NULL,
    status TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    check_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interview_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    topic TEXT NOT NULL,
    question TEXT NOT NULL,
    is_followup INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'asked',
    answer TEXT NOT NULL DEFAULT '',
    processed INTEGER NOT NULL DEFAULT 0,
    asked_at TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS profile_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    edited_by_user INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, topic)
);

CREATE TABLE IF NOT EXISTS profile_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    turn_id INTEGER NOT NULL,
    UNIQUE (company_id, name, kind)
);

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    url TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'competitor',
    why_relevant TEXT NOT NULL DEFAULT '',
    named_by TEXT NOT NULL DEFAULT 'llm',
    site_status TEXT NOT NULL DEFAULT 'none',
    site_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'unrated',
    competitor_id INTEGER REFERENCES competitors(id) ON DELETE SET NULL,
    origin TEXT NOT NULL DEFAULT 'llm',
    status TEXT NOT NULL DEFAULT 'proposed',
    validation TEXT NOT NULL DEFAULT 'unchecked',
    validation_note TEXT NOT NULL DEFAULT '',
    items_found INTEGER NOT NULL DEFAULT 0,
    last_item_at TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    run_type TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    sources_checked INTEGER NOT NULL DEFAULT 0,
    new_records INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS crawl_source_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_duplicate INTEGER NOT NULL DEFAULT 0,
    items_old INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    competitor_id INTEGER REFERENCES competitors(id) ON DELETE SET NULL,
    crawl_run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    url_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    UNIQUE (company_id, url_key),
    UNIQUE (company_id, content_hash)
);

CREATE TABLE IF NOT EXISTS record_sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    seen_at TEXT NOT NULL,
    UNIQUE (record_id, source_id)
);

CREATE TABLE IF NOT EXISTS page_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    crawl_run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    what_happened TEXT NOT NULL DEFAULT '',
    affected_area TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT 'neutral',
    urgency TEXT NOT NULL DEFAULT 'low',
    impact INTEGER NOT NULL DEFAULT 1,
    relevance INTEGER NOT NULL DEFAULT 1,
    why_it_matters TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    evidence_snippet TEXT NOT NULL DEFAULT '',
    evidence_verified INTEGER NOT NULL DEFAULT 0,
    customer_quote TEXT NOT NULL DEFAULT '',
    theme TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT 'unverified',
    reliability REAL NOT NULL DEFAULT 0,
    evidence_strength REAL NOT NULL DEFAULT 0,
    recency REAL NOT NULL DEFAULT 0,
    novelty REAL NOT NULL DEFAULT 0,
    priority REAL NOT NULL DEFAULT 0,
    breakdown TEXT NOT NULL DEFAULT '{}',
    explanation TEXT NOT NULL DEFAULT '',
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    run_type TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    markdown TEXT NOT NULL,
    narrative_json TEXT NOT NULL DEFAULT '{}',
    quality_notes TEXT NOT NULL DEFAULT '[]',
    n_signals INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS brief_signals (
    brief_id INTEGER NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
    signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    ref INTEGER NOT NULL,
    PRIMARY KEY (brief_id, signal_id)
);

CREATE TABLE IF NOT EXISTS brief_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id INTEGER NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    owner TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    signal_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_settings (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    brief_enabled INTEGER NOT NULL DEFAULT 1,
    brief_weekday INTEGER NOT NULL DEFAULT 0,
    brief_time TEXT NOT NULL DEFAULT '08:00',
    brief_days INTEGER NOT NULL DEFAULT 7,
    monitor_enabled INTEGER NOT NULL DEFAULT 1,
    crawl_every_hours INTEGER NOT NULL DEFAULT 12,
    audit_enabled INTEGER NOT NULL DEFAULT 1,
    audit_day INTEGER NOT NULL DEFAULT 1,
    audit_time TEXT NOT NULL DEFAULT '06:00',
    effective_from TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    scheduled_for TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_job
    ON job_runs (company_id, job_type) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    markdown TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    stored_path TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    text_columns TEXT NOT NULL DEFAULT '[]',
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_with_text INTEGER NOT NULL DEFAULT 0,
    rows_analyzed INTEGER NOT NULL DEFAULT 0,
    sampling_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'uploaded',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    analyzed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS upload_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (upload_id, ref)
);

CREATE TABLE IF NOT EXISTS upload_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    theme TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    quotes TEXT NOT NULL DEFAULT '[]',
    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS context_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    changes_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    markdown TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at TEXT NOT NULL DEFAULT ''
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect():
    """Commit on success, roll back on error, always close."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


