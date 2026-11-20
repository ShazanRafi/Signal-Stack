import json
import re
from datetime import datetime, timezone

from backend import config
from backend.db import connect
from backend.verify import normalize

STOP = {
    "that", "this", "with", "from", "have", "will", "your", "their", "they", "them", "been",
    "were", "also", "more", "than", "into", "about", "which", "what", "when", "where", "while",
    "such", "other", "some", "most", "many", "each", "over", "only", "just", "like", "then",
    "https", "http", "company", "website", "verified", "unverified", "facts", "assumptions",
    "founder", "told", "names", "mentioned", "summary", "industry", "next", "after", "before",
}
TIER_RELIABILITY = {"official": 1.0, "primary": 0.9, "internal": 0.8, "news": 0.7,
                    "community": 0.4, "unrated": 0.4}
URGENCY = {"high": 1.0, "medium": 0.6, "low": 0.2}
UNKNOWN_DATE_RECENCY = 0.3


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{4,}", normalize(text)) if t not in STOP}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _parse(ts: str):
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recency_score(published_at: str, discovered_at: str, kind: str, now: datetime) -> float:
    dt = _parse(published_at or (discovered_at if kind == "page_change" else ""))
    if dt is None:
        return UNKNOWN_DATE_RECENCY
    days = max(0.0, (now - dt).total_seconds() / 86400)
    return 0.5 ** (days / config.RECENCY_HALF_LIFE_DAYS)


def evidence_strength(verified: bool, tier: str, sightings: int) -> float:
    s = 0.5 if verified else 0.0
    if tier in ("primary", "official"):
        s += 0.3
    if sightings >= 2:
        s += 0.2
    return min(1.0, s)


def compute_priority(c: dict) -> tuple[float, dict]:
    contrib = {k: round(w * c[k], 4) for k, w in config.PRIORITY_WEIGHTS.items()}
    return round(100 * sum(contrib.values()), 1), contrib


def explain(score, sig, tier, sightings, c) -> str:
    ev = "quote verified in stored text" if sig["evidence_verified"] else "no verified quote"
    return (f"Priority {score:.0f}/100. AI judgement: relevance {sig['relevance']}/5, impact "
            f"{sig['impact']}/5, urgency {sig['urgency']}. Source tier '{tier or 'unrated'}' "
            f"(reliability {c['reliability']:.2f}). Evidence: {ev}, {sightings} source(s) "
            f"(strength {c['evidence']:.2f}). Recency {c['recency']:.2f}, novelty {c['novelty']:.2f}.")


def rank_company(company_id: int, now: datetime | None = None) -> int:
    """(Re)compute all scores for analyzed signals. Deterministic; no LLM. Returns count."""
    now = now or datetime.now(timezone.utc)
    with connect() as conn:
        sigs = [dict(r) for r in conn.execute(
            "SELECT g.*, r.published_at, r.discovered_at, r.kind, s.tier AS tier, "
            "(SELECT COUNT(*) FROM record_sightings x WHERE x.record_id = r.id) AS n_sightings "
            "FROM signals g JOIN records r ON r.id = g.record_id "
            "LEFT JOIN sources s ON s.id = r.source_id "
            "WHERE g.company_id = ? AND g.status = 'analyzed'", (company_id,))]
        recs = [(r["id"], tokens(f"{r['title']}\n{r['text']}")) for r in conn.execute(
            "SELECT id, title, text FROM records WHERE company_id = ? ORDER BY id", (company_id,))]
        for g in sigs:
            earlier = [t for rid, t in recs if rid < g["record_id"]]
            mine = next(t for rid, t in recs if rid == g["record_id"])
            novelty = 1.0 - max((jaccard(mine, t) for t in earlier), default=0.0)
            comp = {
                "relevance": (g["relevance"] - 1) / 4,
                "impact": (g["impact"] - 1) / 4,
                "urgency": URGENCY.get(g["urgency"], 0.2),
                "reliability": TIER_RELIABILITY.get(g["tier"], 0.4),
                "evidence": evidence_strength(bool(g["evidence_verified"]), g["tier"],
                                              g["n_sightings"]),
                "recency": recency_score(g["published_at"], g["discovered_at"], g["kind"], now),
                "novelty": novelty,
            }
            score, contrib = compute_priority(comp)
            conn.execute(
                "UPDATE signals SET reliability=?, evidence_strength=?, recency=?, novelty=?, "
                "priority=?, breakdown=?, explanation=? WHERE id=?",
                (comp["reliability"], comp["evidence"], comp["recency"], novelty, score,
                 json.dumps({"components": comp, "contributions": contrib}),
                 explain(score, g, g["tier"], g["n_sightings"], comp), g["id"]))
    return len(sigs)