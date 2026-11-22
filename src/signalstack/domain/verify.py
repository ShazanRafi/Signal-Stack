import re

_CHAR_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ",
})


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(_CHAR_MAP)).strip().lower()


def quote_in_text(quote: str, text: str, min_words: int = 4) -> bool:
    """True only if `quote` appears contiguously in `text` (ignoring case,
    whitespace and curly quotes). Stitched quotes ("a ... b") and very short
    quotes are rejected, because they are too easy to fake."""
    q = normalize(quote).strip(" .\"'\u2026")
    if "..." in q or "\u2026" in q:
        return False
    if len(q.split()) < min_words:
        return False
    return q in normalize(text)