import sys

from signalstack.services.research import get_profile, research_company


def main() -> None:
    if len(sys.argv) < 3:
        print('usage: python research_cli.py "Company Name" <url>')
        return
    r = research_company(sys.argv[1], sys.argv[2])
    p = get_profile(r.company_id)
    c = p["company"]
    print(f"status={r.status}  verified={r.n_verified}  assumptions={r.n_assumptions}")
    print(f"industry: {c['industry']}\nsummary: {c['summary']}\n")
    print("pages:")
    for pg in p["pages"]:
        print(f"  [{'ok' if pg['ok'] else 'FAIL'}] {pg['url']} {pg['error']}")
    for label in ("verified", "assumption"):
        print(f"\n== {label.upper()} ==")
        for f in p["facts"]:
            if f["status"] == label:
                print(f"- [{f['category']}] {f['claim']}")
                if label == "verified":
                    print(f"    source: {f['source_url']}\n    quote:  {f['evidence']!r}")
                elif f["check_note"]:
                    print(f"    ({f['check_note']})")
    print("\nlimitations:", c["limitations"] or "none")
    print("open questions for the interview:")
    for q in c["open_questions"]:
        print("  -", q)


if __name__ == "__main__":
    main()