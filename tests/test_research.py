import pytest

from backend import db
from backend.fetcher import FetchResult
from backend.llm import LLMError
from backend.research import LLMFact, LLMProfile, get_profile, research_company

BODY = "Acme sells forged steel anvils to professional blacksmiths across Europe. " * 6
HOME = f"""<html><head><title>Acme</title></head><body>
<nav><a href="/about">About us</a></nav><h1>Anvils</h1><p>{BODY}</p></body></html>"""
ABOUT = f"<html><head><title>About</title></head><body><p>{BODY}</p></body></html>"
REAL_QUOTE = "Acme sells forged steel anvils to professional blacksmiths"


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url):
        p = self.pages.get(url)
        if p is None:
            return FetchResult(url=url, final_url=url, status=404, error="HTTP 404")
        if p == "ROBOTS":
            return FetchResult(url=url, final_url=url, blocked_by_robots=True,
                               error="blocked by robots.txt")
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="text/html", text=p)

    def close(self):
        pass


def fact(status="verified", quote=REAL_QUOTE, src="https://acme.test/", claim="Acme sells anvils"):
    return LLMFact(category="products", claim=claim, status=status,
                   source_url=src, evidence_quote=quote)


def llm_returning(*facts):
    calls = []

    def fn(prompt, schema, **kw):
        calls.append(prompt)
        return LLMProfile(industry="Metalwork", summary="Acme sells anvils.",
                          facts=list(facts), open_questions=["Who are the competitors?"])
    fn.calls = calls
    return fn


def never_called(*a, **k):
    raise AssertionError("LLM must not be called")


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")


SITE = {"https://acme.test/": HOME, "https://acme.test/about": ABOUT}


def test_good_fact_verified_and_bad_ones_downgraded():
    llm = llm_returning(
        fact(),                                                         
        fact(quote="Acme is the largest anvil maker in Asia, hands down"),  
        fact(src="https://acme.test/nowhere"),                           
        fact(quote=""),                                                 
        fact(status="assumption", quote="", src="", claim="Likely B2B"),
    )
    r = research_company("Acme", "acme.test", FakeFetcher(SITE), llm)
    assert r.status == "researched"
    assert (r.n_verified, r.n_assumptions) == (1, 4)
    facts = get_profile(r.company_id)["facts"]
    assert facts[0]["status"] == "verified" and facts[0]["source_url"] == "https://acme.test/"
    assert all(f["status"] == "assumption" and f["evidence"] == "" for f in facts[1:])
    assert sum(1 for f in facts if f["check_note"].startswith("downgraded")) == 3
    assert any("downgraded" in l for l in r.limitations)


def test_pages_are_stored_and_prompt_contains_page_text():
    llm = llm_returning(fact())
    r = research_company("Acme", "https://acme.test", FakeFetcher(SITE), llm)
    urls = [p["url"] for p in get_profile(r.company_id)["pages"]]
    assert urls == ["https://acme.test/", "https://acme.test/about"]
    assert "forged steel anvils" in llm.calls[0]


def test_blocked_homepage_means_no_llm_call_and_visible_limitation():
    r = research_company("Acme", "acme.test", FakeFetcher({"https://acme.test/": "ROBOTS"}),
                         never_called)
    assert r.status == "research_failed"
    assert any("robots" in l for l in r.limitations)


def test_js_only_site_is_reported_not_guessed():
    thin = {"https://acme.test/": "<html><body><div id='root'></div></body></html>"}
    r = research_company("Acme", "acme.test", FakeFetcher(thin), never_called)
    assert r.status == "insufficient_content"
    assert any("JavaScript" in l for l in r.limitations)


def test_llm_failure_keeps_pages_and_records_error():
    def boom(*a, **k):
        raise LLMError("quota")
    r = research_company("Acme", "acme.test", FakeFetcher(SITE), boom)
    p = get_profile(r.company_id)
    assert r.status == "research_failed" and len(p["pages"]) == 2
    assert any("LLM error" in l for l in r.limitations)


def test_rerun_replaces_instead_of_duplicating():
    llm = llm_returning(fact())
    a = research_company("Acme", "acme.test", FakeFetcher(SITE), llm)
    b = research_company("Acme", "acme.test", FakeFetcher(SITE), llm)
    assert a.company_id == b.company_id
    p = get_profile(b.company_id)
    assert len(p["facts"]) == 1 and len(p["pages"]) == 2