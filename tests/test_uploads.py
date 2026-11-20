import io

import pytest
from openpyxl import Workbook

from backend import db, uploads as U
from backend.db import connect, init_db

LONG = "The onboarding was confusing and nobody replied to my support emails for days. "


def csv_bytes(rows, header="id,comment,region"):
    lines = [header] + [f"{i},\"{r}\",IN" for i, r in enumerate(rows, 1)]
    return "\n".join(lines).encode("utf-8")


def test_csv_auto_detects_the_long_text_column():
    p = U.parse_csv(csv_bytes([LONG + str(i) for i in range(5)]))
    assert p.text_columns == ["comment"] and len(p.rows) == 5 and p.total_rows == 5


def test_csv_explicit_columns_and_missing_column_error():
    data = csv_bytes([LONG] * 3)
    assert U.parse_csv(data, ["comment"]).text_columns == ["comment"]
    with pytest.raises(ValueError, match="not found"):
        U.parse_csv(data, ["nope"])


def test_csv_without_long_text_column_gives_a_helpful_error():
    data = b"id,region\n1,IN\n2,US\n3,UK\n4,IN\n"
    with pytest.raises(ValueError, match="text-cols"):
        U.parse_csv(data)


def test_rows_without_text_are_skipped_and_reported():
    rows = [LONG, "", LONG, "ok", LONG]
    p = U.parse_csv(csv_bytes(rows))
    assert len(p.rows) == 3 and p.total_rows == 5 and any("skipped" in n for n in p.notes)


def test_redaction_masks_contact_details_but_not_dates():
    t = U.redact("mail me at a.b@example.com or call +91 98765 43210, 555-123-4567, "
                 "ticket on 2026-11-20")
    assert "[email]" in t and t.count("[phone]") == 2 and "2026-11-20" in t


def test_excel_first_sheet():
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "feedback"])
    for i in range(4):
        ws.append([i, LONG + str(i)])
    buf = io.BytesIO()
    wb.save(buf)
    p = U.parse_excel(buf.getvalue())
    assert p.text_columns == ["feedback"] and len(p.rows) == 4
    assert any("first sheet" in n for n in p.notes)


def test_text_is_split_into_passages_and_pdf_errors_are_clear():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(12)).encode()
    p = U.parse_text(text)
    assert len(p.rows) >= 2 and all(len(r) <= U.PASSAGE_CHARS + 50 for r in p.rows)
    with pytest.raises(ValueError, match="PDF"):
        U.parse_pdf(b"definitely not a pdf")


def test_unsupported_extension_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        U.parse_file("data.xls", b"x")


def test_sampling_is_even_and_disclosed():
    rows = [f"row {i} " + "x" * 30 for i in range(100)]
    picked, note = U.sample_rows(rows, 10)
    assert len(picked) == 10 and picked[0] == rows[0] and picked[-1] == rows[90]
    assert "evenly spaced" in note
    assert U.sample_rows(rows, 200) == (rows, "")


@pytest.fixture
def cid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        return conn.execute("INSERT INTO companies (name, url, status) VALUES "
                            "('Acme', 'https://acme.test/', 'stack_built')").lastrowid


def test_ingest_stores_rows_activates_slot_and_caps_the_sample(cid, tmp_path):
    r = U.ingest_bytes(cid, "customer_feedback", "fb.csv",
                       csv_bytes([LONG + str(i) for i in range(30)]), note="PUBLIC PROXY",
                       max_rows=10)
    assert r["rows_to_analyze"] == 10 and r["rows_with_text"] == 30
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM upload_rows WHERE upload_id = ?",
                         (r["upload_id"],)).fetchone()[0]
        src = conn.execute("SELECT status, validation FROM sources WHERE company_id = ? AND "
                           "type = 'upload' AND target = 'customer_feedback'", (cid,)).fetchone()
    assert n == 10 and (src["status"], src["validation"]) == ("approved", "valid")
    assert list((tmp_path / "uploads" / str(cid)).iterdir())          # raw file kept


def test_ingest_validates_inputs(cid):
    with pytest.raises(ValueError):
        U.ingest_bytes(cid, "bad_kind", "x.csv", b"x")
    with pytest.raises(KeyError):
        U.ingest_bytes(999, "crm_export", "x.csv", csv_bytes([LONG] * 3))