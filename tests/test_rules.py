"""Pure business-rule tests (R1-R5, R7). No database needed."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.main import attendance_date, derived_fields, from_millis, late_minutes, overtime_minutes, round_half_up, to_millis, work_hours, working_days
from tests.conftest import ist, ist_ms

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def test_millis_round_trip_truncates_to_whole_seconds():
    assert to_millis(from_millis(ist_ms(2026, 7, 6, 9, 40, 0, 900))) == ist_ms(2026, 7, 6, 9, 40, 0)


@pytest.mark.parametrize(
    "punch, expected",
    [((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45), ((9, 0, 0), 0), ((9, 30, 0), 0)],
)
def test_late_minutes_r2_examples(punch, expected):
    assert late_minutes(ist(2026, 7, 6, *punch), date(2026, 7, 6), DAY) == expected


def test_late_minutes_ignores_sub_second_part_after_truncation():
    punched = from_millis(ist_ms(2026, 7, 6, 9, 40, 0, 900))
    assert late_minutes(punched, date(2026, 7, 6), DAY) == 0


def test_attendance_date_uses_ist_not_utc():
    # 00:10 IST on the 6th is still the 5th in UTC.
    assert attendance_date(ist(2026, 7, 6, 0, 10), DAY) == date(2026, 7, 6)


@pytest.mark.parametrize(
    "punch, expected_day",
    [((2026, 7, 6, 21, 55), date(2026, 7, 6)), ((2026, 7, 7, 0, 30), date(2026, 7, 6)), ((2026, 7, 7, 5, 59, 59), date(2026, 7, 6)), ((2026, 7, 7, 6, 0), date(2026, 7, 7))],
)
def test_attendance_date_overnight_shift(punch, expected_day):
    assert attendance_date(ist(*punch), NIGHT) == expected_day


def test_overnight_late_counts_from_previous_day_shift_start():
    assert late_minutes(ist(2026, 7, 7, 0, 30), date(2026, 7, 6), NIGHT) == 150


@pytest.mark.parametrize("punch_out, expected", [((18, 59, 59), 0), ((19, 0, 0), 30), ((19, 10, 30), 40), ((18, 35, 0), 0)])
def test_overtime_threshold_r3(punch_out, expected):
    assert overtime_minutes(ist(2026, 7, 6, *punch_out), date(2026, 7, 6), DAY) == expected


def test_overtime_overnight_shift_ends_next_day():
    assert overtime_minutes(ist(2026, 7, 7, 6, 40), date(2026, 7, 6), NIGHT) == 40


def test_work_hours_is_half_up_not_float_round():
    start = ist(2026, 7, 6, 9, 0)
    # 8h02m42s = 8.045 h exactly; round(8.045, 2) gives 8.04 because of binary floats.
    assert work_hours(start, start + timedelta(hours=8, minutes=2, seconds=42)) == Decimal("8.05")
    assert work_hours(start, start + timedelta(seconds=54)) == Decimal("0.02")


def test_half_day_uses_rounded_work_hours():
    start = ist(2026, 7, 6, 9, 0)
    just_rounds_up = derived_fields("PRESENT", start, start + timedelta(seconds=16182), date(2026, 7, 6), DAY)  # 4.495 -> 4.50
    below = derived_fields("PRESENT", start, start + timedelta(seconds=16181), date(2026, 7, 6), DAY)  # 4.4947 -> 4.49
    assert just_rounds_up["work_hours"] == 4.5 and just_rounds_up["half_day"] is False
    assert below["work_hours"] == 4.49 and below["half_day"] is True


def test_absent_and_leave_have_default_derived_fields():
    assert derived_fields("LEAVE", None, None, date(2026, 7, 6), DAY) == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_working_days_r7():
    assert working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert working_days(date(2026, 7, 15), date(2026, 7, 31)) == 13
    assert working_days(date(2026, 7, 4), date(2026, 7, 5)) == 0  # a weekend


def test_round_half_up():
    assert round_half_up(Decimal("8.245"), 2) == Decimal("8.25")
    assert round_half_up(Decimal("0.03125"), 4) == Decimal("0.0313")
