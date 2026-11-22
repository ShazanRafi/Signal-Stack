import httpx
import pytest

from signalstack.infrastructure.fetcher import PoliteFetcher, RobotsPolicy

ROBOTS_STAR = """
User-agent: *
Disallow: /private/
Disallow: /*?session=
Allow: /private/public-page
"""

ROBOTS_OURS = """
User-agent: *
Disallow: /private/

User-agent: SignalStackBot
Disallow: /secret/
Crawl-delay: 3
"""

HTML = "<html><head><title>T</title></head><body><h1>Hello</h1></body></html>"


def test_robots_404_allows_everything():
    assert RobotsPolicy.from_response(404, "").allowed("https://x.com/anything")


@pytest.mark.parametrize("status", [None, 301, 401, 403, 500, 503])
def test_robots_unavailable_or_forbidden_blocks_everything(status):
    assert not RobotsPolicy.from_response(status, "").allowed("https://x.com/")


def test_robots_star_rules_and_wildcards():
    p = RobotsPolicy.from_response(200, ROBOTS_STAR)
    assert p.allowed("https://x.com/about")
    assert not p.allowed("https://x.com/private/data")
    assert p.allowed("https://x.com/private/public-page")
    assert not p.allowed("https://x.com/page?session=1")


def test_robots_specific_group_overrides_star():
    p = RobotsPolicy.from_response(200, ROBOTS_OURS)
    assert not p.allowed("https://x.com/secret/a")
    assert p.allowed("https://x.com/private/a")
    assert p.delay == 3


def make_fetcher(robots_status=200, robots_text="User-agent: *\nDisallow: /private/\n"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        html_headers = {"content-type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return httpx.Response(robots_status, text=robots_text)
        if path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        if path in ("/", "/new", "/private/x"):
            return httpx.Response(200, headers=html_headers, text=HTML)
        if path == "/file.pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        return httpx.Response(404, headers=html_headers, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return PoliteFetcher(client=client, default_delay=0), seen


def test_fetch_ok():
    f, _ = make_fetcher()
    r = f.fetch("https://site.test/")
    assert r.ok and "Hello" in r.text and r.content_type == "text/html"


def test_robots_block_means_no_request_to_the_page():
    f, seen = make_fetcher()
    r = f.fetch("https://site.test/private/x")
    assert r.blocked_by_robots and not r.ok
    assert "/private/x" not in seen


def test_redirect_followed_and_final_url_recorded():
    f, _ = make_fetcher()
    r = f.fetch("https://site.test/old")
    assert r.ok and r.final_url == "https://site.test/new"


def test_non_html_rejected():
    f, _ = make_fetcher()
    r = f.fetch("https://site.test/file.pdf")
    assert not r.ok and "content-type" in r.error


def test_404_is_an_error():
    f, _ = make_fetcher()
    r = f.fetch("https://site.test/missing")
    assert not r.ok and "404" in r.error


def test_robots_fetched_once_per_host():
    f, seen = make_fetcher()
    f.fetch("https://site.test/")
    f.fetch("https://site.test/new")
    assert seen.count("/robots.txt") == 1


def test_unreachable_robots_means_do_not_crawl():
    f, seen = make_fetcher(robots_status=503)
    r = f.fetch("https://site.test/")
    assert r.blocked_by_robots
    assert "/" not in seen