import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

# <header> is deliberately kept: many sites put their hero/positioning text in it.
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "form",
              "nav", "footer", "aside", "template"]

LOW_CONTENT_CHARS = 400  # below this the page is probably JS-rendered or nearly empty

# Generic on purpose: this must work for any company, not just the demo one.
KEYWORDS = {
    "about": 3, "who-we-are": 3, "our-story": 3, "mission": 2, "company": 2, "team": 1,
    "product": 3, "service": 3, "solution": 3, "platform": 2, "feature": 2,
    "how-it-works": 3, "pricing": 3, "plans": 2, "customer": 2, "case-stud": 2,
    "partner": 2, "press": 2, "newsroom": 2, "news": 1, "blog": 1, "careers": 1,
    "faq": 2, "investor": 1,
}
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip",
            ".css", ".js", ".ico", ".mp4", ".mp3", ".xml")


@dataclass
class ExtractedPage:
    url: str
    title: str
    description: str
    headings: list[str]
    text: str
    links: list[tuple[str, str]]  # (absolute url, anchor text)
    low_content: bool


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty URL")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"not a valid web URL: {raw}")
    return urlunparse((p.scheme, p.netloc.lower(), p.path or "/", p.params, p.query, ""))


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def same_site(a: str, b: str) -> bool:
    return _host(a) == _host(b)


def _collect_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        if urlparse(absolute).scheme not in ("http", "https") or absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, " ".join(a.get_text(" ", strip=True).split())[:80]))
    return out


def extract_page(html: str, base_url: str, max_chars: int = 12000) -> ExtractedPage:
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc_tag = (soup.find("meta", attrs={"name": "description"})
                or soup.find("meta", attrs={"property": "og:description"}))
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""

    links = _collect_links(soup, base_url)  # before nav is removed: menus hold the best links

    for tag in soup(NOISE_TAGS):
        tag.decompose()

    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])]
    headings = [h for h in headings if h][:30]

    lines, prev = [], None
    for raw in soup.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 2 or line == prev:
            continue
        lines.append(line)
        prev = line
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0]

    return ExtractedPage(
        url=base_url, title=title, description=description, headings=headings,
        text=text, links=links, low_content=len(text) < LOW_CONTENT_CHARS,
    )


def pick_key_pages(links: list[tuple[str, str]], base_url: str, limit: int = 5) -> list[str]:
    scored: list[tuple[float, str]] = []
    seen_paths = set()
    for url, anchor in links:
        if not same_site(url, base_url):
            continue
        path = urlparse(url).path.lower()
        if path in ("", "/") or path.endswith(SKIP_EXT) or path in seen_paths:
            continue
        haystack = f"{path} {anchor.lower()}"
        score = sum(w for k, w in KEYWORDS.items() if k in haystack)
        score -= path.strip("/").count("/") * 0.5  # prefer shallow pages
        if score > 0:
            scored.append((score, url))
            seen_paths.add(path)
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:limit]]