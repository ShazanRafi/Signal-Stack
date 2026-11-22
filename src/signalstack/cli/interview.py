import sys

from signalstack.infrastructure.db import connect, init_db
from signalstack.domain.interview import get_state, retry_pending, start_interview, submit_answer
from signalstack.infrastructure.llm import LLMError


def show_profile(state: dict) -> None:
    print("\n=== PROFILE ===")
    for e in state["entries"]:
        print(f"[{e['topic']}]{' (edited)' if e['edited_by_user'] else ''} {e['content']}")
    print("\nentities:", [(x["name"], x["kind"]) for x in state["entities"]] or "none")
    skipped = [t["topic"] for t in state["turns"] if t["status"] == "skipped"]
    print("skipped topics:", skipped or "none")


def main() -> None:
    init_db()
    if len(sys.argv) < 2:
        with connect() as conn:
            rows = conn.execute("SELECT id, name, url, status FROM companies").fetchall()
        print("usage: python interview_cli.py <company_id>\ncompanies:")
        for r in rows:
            print(f"  {r['id']}: {r['name']} ({r['url']}) [{r['status']}]")
        return

    cid = int(sys.argv[1])
    try:
        state = start_interview(cid)
        while not state["complete"]:
            if state["pending"]:
                state = retry_pending(cid)
                continue
            cur = state["current"]
            tag = cur["topic"] + (" - follow-up" if cur["is_followup"] else "")
            print(f"\n[{state['core_done']}/{state['core_total']} done] ({tag})")
            print(cur["question"])
            text = input("> (/skip or /quit) ").strip()
            if text == "/quit":
                print("Progress is saved. Run the same command to resume.")
                return
            if not text:
                continue
            state = submit_answer(cid, cur["id"], "" if text == "/skip" else text,
                                  skipped=(text == "/skip"))
    except LLMError as e:
        print(f"\nLLM error: {e}\nYour answer is saved. Re-run the same command to retry.")
        return
    show_profile(get_state(cid))


if __name__ == "__main__":
    main()