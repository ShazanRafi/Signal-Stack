import sqlite3
from contextlib import contextmanager

from backend.config import DB_PATH

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
    status TEXT NOT NULL,          -- 'verified' or 'assumption'
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
    status TEXT NOT NULL DEFAULT 'asked',      -- asked | answered | skipped
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


