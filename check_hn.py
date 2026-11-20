from backend.fetcher import PoliteFetcher
from backend.sources import fetch_url, validate_source

f = PoliteFetcher()
url = fetch_url("hn_query", "study abroad platform india")
print(url)
print(validate_source(f, "hn_query", url))