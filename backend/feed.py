from datetime import datetime, timedelta, timezone

from backend.db import connect

EVENT_AT = "COALESCE(NULLIF(r.published_at, ''), r.discovered_at)"


def get_feed(company_id: int, category=None, competitor_id=None, since_days=None,
             min_relevance: int = 1, min_impact: int = 1, verification=None,
             classification=None, sort: str = "date", limit: int = 200) -> list[dict]:
    sql = ("SELECT g.id, g.classification, g.urgency, g.impact, g.relevance, g.verification, "
           "g.priority, g.what_happened, g.why_it_matters, g.suggested_action, "
           "g.evidence_snippet, g.customer_quote, g.explanation, g.affected_area, "
           "r.title, r.url, r.publisher, r.published_at, r.discovered_at, r.category, r.kind, "
           f"c.name AS competitor_name, {EVENT_AT} AS event_at "
           "FROM signals g JOIN records r ON r.id = g.record_id "
           "LEFT JOIN competitors c ON c.id = r.competitor_id "
           "WHERE g.company_id = ? AND g.status = 'analyzed' "
           "AND g.relevance >= ? AND g.impact >= ?")
    args: list = [company_id, min_relevance, min_impact]
    if category:
        sql += " AND r.category = ?"
        args.append(category)
    if competitor_id:
        sql += " AND r.competitor_id = ?"
        args.append(competitor_id)
    if verification:
        sql += " AND g.verification = ?"
        args.append(verification)
    if classification:
        sql += " AND g.classification = ?"
        args.append(classification)
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        sql += f" AND {EVENT_AT} >= ?"
        args.append(cutoff)
    sql += " ORDER BY g.priority DESC, g.id" if sort == "priority" else f" ORDER BY {EVENT_AT} DESC, g.id DESC"
    sql += " LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def competitor_options(company_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, name FROM competitors WHERE company_id = ? AND status = 'active' "
            "ORDER BY name", (company_id,))]


def get_shared_records(company_id: int, limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.id, r.title, r.publisher, COUNT(x.id) AS sightings "
            "FROM records r JOIN record_sightings x ON x.record_id = r.id "
            "WHERE r.company_id = ? GROUP BY r.id HAVING COUNT(x.id) > 1 "
            "ORDER BY sightings DESC, r.id DESC LIMIT ?", (company_id, limit))]