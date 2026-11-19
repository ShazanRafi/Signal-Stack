import sys

from backend.extract import extract_page, normalize_url, pick_key_pages
from backend.fetcher import PoliteFetcher


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python fetch_cli.py <url>")
        return
    url = normalize_url(sys.argv[1])
    fetcher = PoliteFetcher()
    res = fetcher.fetch(url)
    print(f"status={res.status} type={res.content_type!r} final={res.final_url}")
    print(f"blocked_by_robots={res.blocked_by_robots} error={res.error!r} bytes={len(res.text)}")
    if not res.ok:
        return

    page = extract_page(res.text, res.final_url)
    print(f"\ntitle: {page.title}")
    print(f"description: {page.description}")
    print(f"headings: {page.headings[:10]}")
    print(f"text chars: {len(page.text)}   low_content: {page.low_content}")
    print("\n--- first 600 chars of text ---")
    print(page.text[:600])
    print(f"\nlinks found: {len(page.links)}")
    print("key pages we would fetch next:")
    for u in pick_key_pages(page.links, res.final_url):
        print("  ", u)


if __name__ == "__main__":
    main()