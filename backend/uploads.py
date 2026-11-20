import csv
import io
import itertools
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backend import db as _db
from backend.db import connect, init_db
from backend.stack import _ensure_internal_slots

KINDS = {
    "crm_export": "CRM / sales export",
    "customer_feedback": "customer feedback and support conversations",
    "transcripts_reports": "sales-call / meeting transcripts and internal reports",
}
FILE_TYPES = {".csv": "csv", ".tsv": "csv", ".xlsx": "excel", ".xlsm": "excel",
              ".txt": "text", ".md": "text", ".pdf": "pdf"}
MAX_FILE_BYTES = 15_000_000
MAX_ROWS_READ = 20_000
MAX_ANALYZE_ROWS = 150
MAX_PDF_PAGES = 60
MIN_ROW_CHARS = 20
ROW_CHARS = 800
PASSAGE_CHARS = 1500

csv.field_size_limit(10_000_000)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_INTL = re.compile(r"\+\d[\d ()-]{8,}\d")
_PHONE_PLAIN = re.compile(r"(?<![\w-])(?:\(\d{3}\)\s?|\d{3}[ -]?)\d{3}[ -]?\d{4}(?![\w-])")


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE_INTL.sub("[phone]", text)
    return _PHONE_PLAIN.sub("[phone]", text)


@dataclass
class ParsedFile:
    rows: list[str]
    total_rows: int
    text_columns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _auto_text_columns(cols: list[str], body: list[list[str]]) -> list[str]:
    sample = body[:500]
    scored = []
    for i, c in enumerate(cols):
        vals = [str(r[i]).strip() for r in sample if i < len(r) and str(r[i]).strip()]
        if len(vals) < max(3, 0.1 * len(sample)):
            continue
        med = statistics.median(len(v) for v in vals)
        if med >= 30:
            scored.append((med, c))
    scored.sort(reverse=True)
    return [c for _, c in scored[:2]]


def _table_to_rows(header, body, text_cols) -> ParsedFile:
    cols = [str(h).strip() if h not in (None, "") else f"col{i + 1}"
            for i, h in enumerate(header)]
    if text_cols:
        missing = [c for c in text_cols if c not in cols]
        if missing:
            raise ValueError(f"column(s) not found: {missing}. Available columns: {cols}")
        chosen = list(text_cols)
    else:
        chosen = _auto_text_columns(cols, body)
        if not chosen:
            raise ValueError("no long-text column found; pass --text-cols with the column "
                             f"name(s) to analyse. Available columns: {cols}")
    idx = [cols.index(c) for c in chosen]
    rows = []
    for r in body:
        parts = []
        for i in idx:
            v = str(r[i]).strip() if i < len(r) and r[i] is not None else ""
            if v:
                parts.append(f"{cols[i]}: {v}" if len(idx) > 1 else v)
        text = " | ".join(parts)
        if len(text) >= MIN_ROW_CHARS:
            rows.append(redact(text))
    notes = []
    if len(rows) < len(body):
        notes.append(f"{len(body) - len(rows)} of {len(body)} rows had no usable text and "
                     "were skipped.")
    return ParsedFile(rows, len(body), chosen, notes)


def parse_csv(data: bytes, text_cols: list[str] | None = None, tsv: bool = False) -> ParsedFile:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if tsv else csv.excel
    try:
        rows = list(itertools.islice(csv.reader(io.StringIO(text), dialect), MAX_ROWS_READ + 1))
    except csv.Error as e:
        raise ValueError(f"could not read the CSV: {e}") from None
    if len(rows) < 2:
        raise ValueError("the file has no data rows")
    parsed = _table_to_rows(rows[0], rows[1:], text_cols)
    if len(rows) > MAX_ROWS_READ:
        parsed.notes.append(f"Only the first {MAX_ROWS_READ} rows were read.")
    return parsed


def parse_excel(data: bytes, text_cols: list[str] | None = None) -> ParsedFile:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"could not read the Excel file: {type(e).__name__}") from None
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        body = [["" if v is None else str(v) for v in r]
                for r in itertools.islice(it, MAX_ROWS_READ)]
        title = ws.title
    finally:
        wb.close()
    if not header or not body:
        raise ValueError("the first sheet has no data rows")
    parsed = _table_to_rows(header, body, text_cols)
    parsed.notes.insert(0, f"Read the first sheet ('{title}').")
    return parsed


def _passages(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        while len(p) > PASSAGE_CHARS:
            cut = p.rfind(" ", 0, PASSAGE_CHARS)
            cut = cut if cut > 200 else PASSAGE_CHARS
            piece, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                out.append(cur)
                cur = ""
            out.append(piece)
        if not p:
            continue
        if cur and len(cur) + len(p) + 2 > PASSAGE_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return [redact(x) for x in out if len(x) >= MIN_ROW_CHARS]


def parse_text(data: bytes) -> ParsedFile:
    passages = _passages(_decode(data))
    return ParsedFile(passages, len(passages), [], ["Split into passages of about "
                                                    f"{PASSAGE_CHARS} characters."])


def parse_pdf(data: bytes) -> ParsedFile:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("the PDF is password-protected")
        text = "\n\n".join((p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES])
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"could not read the PDF: {type(e).__name__}") from None
    if len(text.strip()) < 50:
        raise ValueError("no extractable text in the PDF (a scanned document? "
                         "OCR is not supported)")
    passages = _passages(text)
    return ParsedFile(passages, len(passages), [], ["Extracted text only; layout and images "
                                                    "are ignored."])


def parse_file(filename: str, data: bytes, text_cols: list[str] | None = None) -> ParsedFile:
    ext = Path(filename).suffix.lower()
    kind = FILE_TYPES.get(ext)
    if kind == "csv":
        return parse_csv(data, text_cols, tsv=ext == ".tsv")
    if kind == "excel":
        return parse_excel(data, text_cols)
    if kind == "pdf":
        return parse_pdf(data)
    if kind == "text":
        return parse_text(data)
    hint = " (save .xls files as .xlsx)" if ext == ".xls" else ""
    raise ValueError(f"unsupported file type {ext or '(none)'}: use CSV, XLSX, PDF or TXT{hint}")


def sample_rows(rows: list[str], max_rows: int) -> tuple[list[str], str]:
    if len(rows) <= max_rows:
        return rows, ""
    step = len(rows) / max_rows
    picked = [rows[int(i * step)] for i in range(max_rows)]
    return picked, (f"{len(rows)} rows had text; analysed an evenly spaced sample of "
                    f"{max_rows} (about every {step:.1f}th row).")


def ingest_bytes(company_id: int, kind: str, filename: str, data: bytes,
                 text_cols: list[str] | None = None, note: str = "",
                 max_rows: int | None = None) -> dict:
    init_db()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file is larger than {MAX_FILE_BYTES // 1_000_000} MB")
    parsed = parse_file(filename, data, text_cols)       
    if not parsed.rows:
        raise ValueError("no usable text rows were found in the file")
    rows, sampling = sample_rows(parsed.rows, max_rows or MAX_ANALYZE_ROWS)
    with connect() as conn:
        if conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise KeyError(f"no company {company_id}")
    _ensure_internal_slots(company_id)

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:80]
    folder = _db.DB_PATH.parent / "uploads" / str(company_id)
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = folder / f"{now:%Y%m%d%H%M%S}_{safe}"
    path.write_bytes(data)

    ftype = FILE_TYPES[Path(filename).suffix.lower()]
    notes = " ".join(parsed.notes + ([sampling] if sampling else []))
    with connect() as conn:
        slot = conn.execute("SELECT id FROM sources WHERE company_id = ? AND type = 'upload' "
                            "AND target = ?", (company_id, kind)).fetchone()
        uid = conn.execute(
            "INSERT INTO uploads (company_id, source_id, kind, filename, file_type, stored_path, "
            "note, text_columns, rows_total, rows_with_text, rows_analyzed, sampling_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, slot["id"] if slot else None, kind, Path(filename).name, ftype,
             str(path), note.strip(), __import__("json").dumps(parsed.text_columns),
             parsed.total_rows, len(parsed.rows), len(rows), notes)).lastrowid
        conn.executemany("INSERT INTO upload_rows (upload_id, ref, text) VALUES (?, ?, ?)",
                         [(uid, f"R{i}", t[:ROW_CHARS]) for i, t in enumerate(rows, 1)])
        if slot:
            conn.execute("UPDATE sources SET status = 'approved', validation = 'valid', "
                         "validation_note = ?, checked_at = ? WHERE id = ?",
                         (f"{Path(filename).name} uploaded", now.isoformat(), slot["id"]))
    return {"upload_id": uid, "rows_total": parsed.total_rows, "rows_with_text": len(parsed.rows),
            "rows_to_analyze": len(rows), "text_columns": parsed.text_columns, "notes": notes}


def ingest_path(company_id: int, kind: str, path: str, **kw) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"file not found: {path}")
    return ingest_bytes(company_id, kind, p.name, p.read_bytes(), **kw)


def list_uploads(company_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, filename, file_type, note, rows_total, rows_with_text, "
            "rows_analyzed, status, error, created_at, analyzed_at FROM uploads "
            "WHERE company_id = ? ORDER BY id DESC", (company_id,))]