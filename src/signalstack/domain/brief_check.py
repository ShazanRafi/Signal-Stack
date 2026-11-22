import re

from signalstack.domain.verify import normalize

MARKER = re.compile(r"\[S(\d+)\]")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_YEAR = re.compile(r"20\d\d")


def marker_ids(text: str) -> list[int]:
    return [int(x) for x in MARKER.findall(text)]


def unsupported_numbers(text: str, corpus: str) -> list[str]:
    haystack = normalize(corpus).replace(",", "")
    bad = []
    for m in _NUM.findall(MARKER.sub(" ", text)):
        raw = m.replace(",", "")
        digits = re.sub(r"\D", "", raw)
        if (len(digits) < 2 and "%" not in raw) or _YEAR.fullmatch(raw):
            continue
        if raw.rstrip("%") not in haystack:
            bad.append(m)
    return bad


def check_text(text: str, allowed: set[int], corpus: str) -> str | None:
    ids = marker_ids(text)
    if not ids:
        return "no source markers"
    if any(i not in allowed for i in ids):
        return "cites a signal that is not in this section"
    bad = unsupported_numbers(text, corpus)
    if bad:
        return "unsupported number(s): " + ", ".join(bad[:3])
    return None


def valid_ids(ids: list[int], allowed: set[int]) -> list[int] | None:
    uniq = list(dict.fromkeys(ids))
    return uniq if uniq and all(i in allowed for i in uniq) else None