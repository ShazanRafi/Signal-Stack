from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from backend.parsers import page_diff, parse_feed

NOW = datetime.now(timezone.utc)


def test_future_dates_are_cleared_not_trusted():
    xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>F</title><item>'
           '<title>Future post</title><link>https://b.test/f</link>'
           f'<pubDate>{format_datetime(NOW + timedelta(days=10))}</pubDate></item></channel></rss>')
    assert parse_feed(xml, now=NOW)[0]["published_at"] == ""


def test_full_content_preferred_and_publisher_is_feed_title():
    xml = ('<?xml version="1.0"?><rss version="2.0" '
           'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>My Blog</title>'
           '<item><title>Big news</title><link>https://b.test/1</link>'
           '<description>short teaser</description><content:encoded><![CDATA['
           '<p>The full story: we now accept UPI payments across all plans.</p>]]>'
           '</content:encoded></item></channel></rss>')
    it = parse_feed(xml)[0]
    assert "accept UPI payments" in it["text"] and it["publisher"] == "My Blog"
    assert it["published_at"] == ""


def test_page_diff_ignores_volatile_lines():
    old = "Standard plan: Rs 499 per month\nLast updated 10:15\nCopyright 2025 Globex"
    new = "Standard plan: Rs 599 per month\nLast updated 12:45\nCopyright 2026 Globex"
    added, removed = page_diff(old, new)
    assert added == ["Standard plan: Rs 599 per month"]
    assert removed == ["Standard plan: Rs 499 per month"]