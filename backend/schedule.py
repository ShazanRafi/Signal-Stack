import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_hhmm(value: str) -> str:
    if not isinstance(value, str) or not _HHMM.match(value.strip()):
        raise ValueError(f"time must look like 08:00 (24-hour), got {value!r}")
    return value.strip()


def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError(
            f"unknown timezone {name!r} (examples: UTC, Asia/Kolkata, Europe/London). "
            "On Windows you also need: pip install tzdata") from None


def _at(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(d, time(int(h), int(m)), tzinfo=tz).astimezone(timezone.utc)


def latest_weekly(now: datetime, weekday: int, hhmm: str, tz_name: str) -> datetime:
    tz = get_tz(tz_name)
    local = now.astimezone(tz)
    d = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
    occ = _at(d, hhmm, tz)
    if occ > now:
        occ = _at(d - timedelta(days=7), hhmm, tz)
    return occ


def next_weekly(now: datetime, weekday: int, hhmm: str, tz_name: str) -> datetime:
    tz = get_tz(tz_name)
    latest = latest_weekly(now, weekday, hhmm, tz_name)
    return _at(latest.astimezone(tz).date() + timedelta(days=7), hhmm, tz)


def latest_monthly(now: datetime, day: int, hhmm: str, tz_name: str) -> datetime:
    tz = get_tz(tz_name)
    local = now.astimezone(tz)
    occ = _at(date(local.year, local.month, day), hhmm, tz)
    if occ > now:
        y, m = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
        occ = _at(date(y, m, day), hhmm, tz)
    return occ


def next_monthly(now: datetime, day: int, hhmm: str, tz_name: str) -> datetime:
    tz = get_tz(tz_name)
    ld = latest_monthly(now, day, hhmm, tz_name).astimezone(tz).date()
    y, m = (ld.year + 1, 1) if ld.month == 12 else (ld.year, ld.month + 1)
    return _at(date(y, m, day), hhmm, tz)