from datetime import date, datetime, timezone

import pytest

from app.operations.dates import resolve, today_ist


def test_today_uses_ist_not_utc():
    # 20:00 UTC on 31 Oct is already 1 Nov in India (UTC+5:30).
    assert today_ist(datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)) == date(2026, 11, 1)
    assert today_ist(datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)) == date(2026, 10, 31)


@pytest.mark.parametrize("today,start,end", [
    (date(2026, 11, 20), date(2026, 10, 1), date(2026, 10, 31)),
    (date(2026, 1, 5), date(2025, 12, 1), date(2025, 12, 31)),
    (date(2024, 3, 31), date(2024, 2, 1), date(2024, 2, 29)),   # leap year
])
def test_last_month(today, start, end):
    r = resolve("last_month", today)
    assert (r.start, r.end) == (start, end)


def test_weeks_run_monday_to_sunday():
    r = resolve("this_week", date(2026, 11, 20))  # a Friday
    assert (r.start, r.end) == (date(2026, 11, 16), date(2026, 11, 22))
    r = resolve("last_week", date(2026, 11, 20))
    assert (r.start, r.end) == (date(2026, 11, 9), date(2026, 11, 15))


def test_next_and_last_n_days():
    assert (lambda r: (r.start, r.end))(resolve("next_n_days", date(2026, 11, 20), n=30)) == (date(2026, 11, 20), date(2026, 12, 20))
    assert (lambda r: (r.start, r.end))(resolve("last_n_days", date(2026, 11, 20), n=7)) == (date(2026, 11, 14), date(2026, 11, 20))


def test_label_shows_exact_range():
    assert resolve("last_month", date(2026, 11, 20)).label == "1 Oct 2026 – 31 Oct 2026 (IST)"


def test_bad_inputs():
    with pytest.raises(ValueError):
        resolve("next_n_days", date(2026, 1, 1))
    with pytest.raises(ValueError):
        resolve("fortnight", date(2026, 1, 1))
