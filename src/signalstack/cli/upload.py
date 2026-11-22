import sys

from signalstack.services.internal import analyze_upload, get_findings
from signalstack.infrastructure.llm import LLMError
from signalstack.domain.uploads import KINDS, ingest_path, list_uploads

USAGE = """usage (replace UPPERCASE words with real values; quote paths and notes):
  python upload_cli.py add COMPANY_ID KIND "PATH" [--note "text"] [--text-cols "A,B"] [--max-rows N]
  python upload_cli.py analyze UPLOAD_ID
  python upload_cli.py list COMPANY_ID
  python upload_cli.py findings UPLOAD_ID
KIND is one of: """ + ", ".join(KINDS) + """
Use --note to label public/proxy data, e.g. --note "PUBLIC PROXY DATASET: <name>, not company data"
"""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = sys.argv[1:]
    if len(a) < 2:
        print(USAGE)
        return
    cmd = a[0]
    try:
        if cmd == "add":
            opts, i = {}, 4
            while i < len(a):
                flag = a[i]
                if flag not in ("--note", "--text-cols", "--max-rows") or i + 1 >= len(a):
                    raise ValueError(f"bad option: {flag}")
                opts[flag], i = a[i + 1], i + 2
            r = ingest_path(
                int(a[1]), a[2], a[3], note=opts.get("--note", ""),
                text_cols=[c.strip() for c in opts["--text-cols"].split(",")]
                if "--text-cols" in opts else None,
                max_rows=int(opts["--max-rows"]) if "--max-rows" in opts else None)
            print(f"upload {r['upload_id']}: {r['rows_total']} rows, {r['rows_with_text']} with "
                  f"text, {r['rows_to_analyze']} to analyse; text columns: {r['text_columns']}")
            print("notes:", r["notes"] or "none")
            print(f"next: python upload_cli.py analyze {r['upload_id']}")
        elif cmd == "analyze":
            r = analyze_upload(int(a[1]))
            print(f"rows analysed={r['rows_analyzed']} themes={r['themes']} "
                  f"records created={r['records']}")
            for e in r["errors"]:
                print("note:", e)
        elif cmd == "list":
            for u in list_uploads(int(a[1])):
                print(f"upload {u['id']} [{u['status']}] {u['kind']} {u['filename']} "
                      f"rows={u['rows_analyzed']}/{u['rows_with_text']} {u['note'][:60]}")
        elif cmd == "findings":
            for f in get_findings(int(a[1])):
                tag = f"-> record {f['record_id']}" if f["record_id"] else "(not enough evidence for a record)"
                print(f"[{f['category']}] {f['theme']}: {f['row_count']} rows {tag}")
                for q in f["quotes"]:
                    print(f'    "{q}"')
        else:
            print(USAGE)
    except (LLMError, KeyError, ValueError, IndexError) as e:
        print(f"error: {e}")


if __name__ == "__main__":
    main()