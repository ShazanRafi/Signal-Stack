from datetime import datetime, timedelta, timezone

import pytest

from backend import db
from backend.db import connect, init_db
from backend.feed import get_feed, get_shared_records

NOW = datetime.now(timezone.utc)


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        return conn.execute("INSERT INTO companies (name, url) VALUES ('Acme', 'https://a.test/')").lastrowid


def add(cid, title, category="market", days=1, relevance=4, impact=4,
        verification="reported", classification="threat", priority=50):
    ts = (NOW - timedelta(days=days)).isoformat()
    with connect() as conn:
        rid = conn.execute(
            "INSERT INTO records (company_id, category, kind, title, url, url_key, content_hash, "
            "published_at, discovered_at) VALUES (?, ?, 'article', ?, ?, ?, ?, ?, ?)",
            (cid, category, title, f"https://x.test/{title}", f"k{title}", f"h{title}", ts, ts)).lastrowid
        conn.execute(
            "INSERT INTO signals (company_id, record_id, status, relevance, impact, verification, "
            "classification, priority) VALUES (?, ?, 'analyzed', ?, ?, ?, ?, ?)",
            (cid, rid, relevance, impact, verification, classification, priority))
    return rid


def test_filters_and_ordering(cid):
    add(cid, "new", days=1, priority=10)
    add(cid, "old", days=20, priority=90, category="customers")
    add(cid, "weak", days=2, relevance=1, verification="unverified")
    assert [f["title"] for f in get_feed(cid)] == ["new", "weak", "old"]          # newest first
    assert get_feed(cid, sort="priority")[0]["title"] == "old"
    assert [f["title"] for f in get_feed(cid, since_days=7)] == ["new", "weak"]
    assert [f["title"] for f in get_feed(cid, category="customers")] == ["old"]
    assert "weak" not in [f["title"] for f in get_feed(cid, min_relevance=3)]
    assert [f["title"] for f in get_feed(cid, verification="unverified")] == ["weak"]


def test_shared_records(cid):
    rid = add(cid, "story")
    with connect() as conn:
        for tier in ("a", "b"):
            sid = conn.execute(
                "INSERT INTO sources (company_id, category, type, target, reason, status, dedupe_key) "
                "VALUES (?, 'market', 'rss', ?, 'r', 'approved', ?)", (cid, tier, tier)).lastrowid
            conn.execute("INSERT INTO record_sightings (record_id, source_id, seen_at) VALUES (?, ?, 'x')", (rid, sid))
    assert get_shared_records(cid)[0]["sightings"] == 2