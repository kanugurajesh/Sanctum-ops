"""Relative dates are resolved by code, in Asia/Kolkata. The LLM only names a preset."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.operations.model import DateRange

IST = ZoneInfo("Asia/Kolkata")

PRESETS = (
    "today", "yesterday", "tomorrow", "this_week", "last_week", "this_month", "last_month",
    "this_year", "last_year", "last_n_days", "next_n_days", "on_date", "custom",
)


def today_ist(now: datetime | None = None) -> date:
    now = now or datetime.now(tz=IST)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(IST).date()


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def resolve(
    preset: str, today: date, n: int | None = None,
    start: date | None = None, end: date | None = None,
) -> DateRange:
    """Inclusive range for a preset. Weeks run Monday–Sunday."""
    if preset == "today":
        s = e = today
    elif preset == "yesterday":
        s = e = today - timedelta(days=1)
    elif preset == "tomorrow":
        s = e = today + timedelta(days=1)
    elif preset == "this_week":
        s = today - timedelta(days=today.weekday())
        e = s + timedelta(days=6)
    elif preset == "last_week":
        s = today - timedelta(days=today.weekday() + 7)
        e = s + timedelta(days=6)
    elif preset == "this_month":
        s, e = _month_bounds(today.year, today.month)
    elif preset == "last_month":
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        s, e = _month_bounds(y, m)
    elif preset == "this_year":
        s, e = date(today.year, 1, 1), date(today.year, 12, 31)
    elif preset == "last_year":
        s, e = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif preset == "last_n_days":
        if not n or n < 1:
            raise ValueError("last_n_days needs n >= 1")
        s, e = today - timedelta(days=n - 1), today
    elif preset == "next_n_days":
        if not n or n < 1:
            raise ValueError("next_n_days needs n >= 1")
        s, e = today, today + timedelta(days=n)
    elif preset == "on_date":
        if not start:
            raise ValueError("on_date needs a date")
        s = e = start
    elif preset == "custom":
        if not start or not end:
            raise ValueError("custom range needs start and end")
        s, e = start, end
    else:
        raise ValueError(f"unknown date preset {preset!r}")
    if s > e:
        s, e = e, s
    return DateRange(start=s, end=e, preset=preset)
