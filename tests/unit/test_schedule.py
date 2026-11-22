from datetime import datetime, timezone

import pytest

from signalstack.domain import schedule as T

UTC = timezone.utc


def dt(*a):
    return datetime(*a, tzinfo=UTC)


def test_weekly_slot_before_and_after():
    after, before = dt(2026, 11, 23, 9, 0), dt(2026, 11, 23, 7, 0)
    assert T.latest_weekly(after, 0, "08:00", "UTC") == dt(2026, 11, 23, 8, 0)
    assert T.latest_weekly(before, 0, "08:00", "UTC") == dt(2026, 11, 16, 8, 0)
    assert T.next_weekly(after, 0, "08:00", "UTC") == dt(2026, 11, 30, 8, 0)


def test_timezone_is_applied():
    now = dt(2026, 11, 23, 3, 0)
    assert T.latest_weekly(now, 0, "08:00", "Asia/Kolkata") == dt(2026, 11, 23, 2, 30)


def test_monthly_slot_and_year_rollover():
    assert T.latest_monthly(dt(2026, 11, 20, 10, 0), 1, "06:00", "UTC") == dt(2026, 11, 1, 6, 0)
    assert T.next_monthly(dt(2026, 11, 20, 10, 0), 1, "06:00", "UTC") == dt(2026, 12, 1, 6, 0)
    assert T.latest_monthly(dt(2026, 1, 1, 5, 0), 1, "06:00", "UTC") == dt(2025, 12, 1, 6, 0)
    assert T.next_monthly(dt(2026, 12, 15, 0, 0), 1, "06:00", "UTC") == dt(2027, 1, 1, 6, 0)


def test_validation():
    assert T.validate_hhmm("08:05") == "08:05"
    for bad in ("25:00", "8:5", "0800", ""):
        with pytest.raises(ValueError):
            T.validate_hhmm(bad)
    with pytest.raises(ValueError):
        T.get_tz("Mars/Olympus")