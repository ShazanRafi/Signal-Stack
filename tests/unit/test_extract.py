import pytest

from signalstack.infrastructure.extract import extract_page, normalize_url, pick_key_pages, same_site

BODY = "We make the best anvils in the world, shipped worldwide with a ten year guarantee. " * 8

HTML = f"""
<html><head><title>Acme Co - Home</title>
<meta name="description" content="Acme sells anvils."></head>
<body>
<nav>
  <a href="/about">About us</a>
  <a href="/products/anvils">Products</a>
  <a href="https://other.com/x">External</a>
  <a href="/careers">Careers</a>
  <a href="mailto:a@b.c">mail</a>
  <a href="/files/brochure.pdf">brochure</a>
  <a href="https://acme.com/pricing">Pricing</a>
</nav>
<script>var tracking = 1;</script>
<h1>Anvils for everyone</h1>
<p>{BODY}</p>
<footer>Copyright junk</footer>
</body></html>
"""


def test_basic_extraction():
    page = extract_page(HTML, "https://www.acme.com/")
    assert page.title == "Acme Co - Home"
    assert page.description == "Acme sells anvils."
    assert "Anvils for everyone" in page.text
    assert "best anvils" in page.text
    assert "tracking" not in page.text
    assert "Copyright junk" not in page.text
    assert page.headings == ["Anvils for everyone"]
    assert not page.low_content


def test_low_content_flag_for_thin_pages():
    page = extract_page("<html><body><div id='root'></div></body></html>", "https://x.com/")
    assert page.low_content


def test_key_page_selection():
    page = extract_page(HTML, "https://www.acme.com/")
    picked = pick_key_pages(page.links, "https://www.acme.com/")
    assert "https://www.acme.com/about" in picked
    assert "https://www.acme.com/products/anvils" in picked
    assert "https://acme.com/pricing" in picked
    assert "https://www.acme.com/careers" in picked
    assert not any("other.com" in u or u.endswith(".pdf") or u.startswith("mailto") for u in picked)


def test_key_pages_respect_limit():
    links = [(f"https://x.com/about-{i}", "About") for i in range(10)]
    assert len(pick_key_pages(links, "https://x.com/", limit=3)) == 3


def test_same_site():
    assert same_site("https://www.a.com/x", "https://a.com/y")
    assert not same_site("https://blog.a.com/", "https://a.com/")


def test_normalize_url():
    assert normalize_url("  www.Acme.com/path#frag ") == "https://www.acme.com/path"
    assert normalize_url("acme.com") == "https://acme.com/"
    with pytest.raises(ValueError):
        normalize_url("ftp://acme.com")
    with pytest.raises(ValueError):
        normalize_url("   ")