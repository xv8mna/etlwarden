from datetime import datetime

import pytest

from etlwarden import schedule as sched
from tests.conftest import ts


def at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="minutes")


# 2026-10-14 is a Wednesday.


def test_daily_runs_later_today():
    assert at(sched.next_run_after(sched.daily("06:00"), ts("2026-10-14T05:59"))) == "2026-10-14T06:00"


def test_daily_moves_to_tomorrow_once_the_time_has_passed():
    assert at(sched.next_run_after(sched.daily("06:00"), ts("2026-10-14T06:00"))) == "2026-10-15T06:00"


def test_weekdays_skip_the_weekend():
    friday_evening = ts("2026-10-16T18:00")
    assert at(sched.next_run_after(sched.daily("06:00", sched.WEEKDAYS), friday_evening)) == "2026-10-19T06:00"


def test_a_single_day_waits_a_week():
    wednesday_morning = ts("2026-10-14T07:00")
    assert at(sched.next_run_after(sched.daily("06:00", [2]), wednesday_morning)) == "2026-10-21T06:00"


@pytest.mark.parametrize(
    "now, expected",
    [
        ("2026-10-14T06:07", "2026-10-14T06:15"),
        ("2026-10-14T06:15", "2026-10-14T06:30"),
        ("2026-10-14T23:50", "2026-10-15T00:00"),
    ],
)
def test_intervals_line_up_with_the_clock(now, expected):
    assert at(sched.next_run_after(sched.every(15), ts(now))) == expected


def test_intervals_in_hours():
    assert at(sched.next_run_after(sched.every(120), ts("2026-10-14T05:30"))) == "2026-10-14T06:00"


@pytest.mark.parametrize(
    "schedule, message",
    [
        ({"type": "daily", "time": "25:00", "days": [0]}, "isn't a time"),
        ({"type": "daily", "time": "6 am", "days": [0]}, "look like"),
        ({"type": "daily", "time": "06:00", "days": []}, "at least one day"),
        ({"type": "daily", "time": "06:00", "days": [7]}, "0 \\(Mon\\) to 6"),
        ({"type": "interval", "minutes": 0}, "divide a day evenly"),
        ({"type": "interval", "minutes": 7}, "divide a day evenly"),
        ({"type": "interval", "minutes": 1000}, "divide a day evenly"),
        ({"type": "interval", "minutes": "often"}, "whole number"),
        ({"type": "hourly"}, "Unknown"),
    ],
)
def test_invalid_schedules_say_what_is_wrong(schedule, message):
    with pytest.raises(sched.ScheduleError, match=message):
        sched.validate(schedule)


def test_every_offered_interval_divides_a_day():
    for minutes in sched.INTERVAL_CHOICES:
        sched.every(minutes)  # raises if not


def test_a_six_hour_interval_runs_at_fixed_clock_times():
    times = []
    moment = ts("2026-10-14T00:00")
    for _ in range(5):
        moment = sched.next_run_after(sched.every(360), moment)
        times.append(at(moment))
    assert times == ["2026-10-14T06:00", "2026-10-14T12:00", "2026-10-14T18:00",
                     "2026-10-15T00:00", "2026-10-15T06:00"]


# These only mean something where clocks change on 2026-03-29 and 2026-10-25,
# as in most of Europe. Elsewhere they are skipped.
_falls_back = ts("2026-10-25T04:00") - ts("2026-10-25T00:00") == 5 * 3600
_springs_forward = ts("2026-03-29T04:00") - ts("2026-03-29T00:00") == 3 * 3600


@pytest.mark.skipif(not _falls_back, reason="no daylight-saving change here on 2026-10-25")
def test_a_daily_interval_runs_once_on_the_25_hour_day():
    after_midnight = ts("2026-10-25T00:00") + 1
    assert at(sched.next_run_after(sched.every(1440), after_midnight)) == "2026-10-26T00:00"
    assert at(sched.next_run_after(sched.every(720), ts("2026-10-25T11:00"))) == "2026-10-25T12:00"


@pytest.mark.skipif(not _springs_forward, reason="no daylight-saving change here on 2026-03-29")
def test_intervals_skip_the_missing_hour_without_running_twice():
    first = sched.next_run_after(sched.every(120), ts("2026-03-29T01:00"))
    assert at(first) == "2026-03-29T03:00"  # 02:00 doesn't exist that day
    assert at(sched.next_run_after(sched.every(120), first + 1)) == "2026-03-29T04:00"


def test_validate_cleans_up():
    assert sched.validate({"type": "daily", "time": "6:5", "days": [4, 0, 0]}) == {
        "type": "daily",
        "time": "06:05",
        "days": [0, 4],
    }


@pytest.mark.parametrize(
    "schedule, text",
    [
        (sched.daily("06:00"), "Every day at 06:00"),
        (sched.daily("06:00", sched.WEEKDAYS), "Mon–Fri at 06:00"),
        (sched.daily("07:30", [0, 2, 4]), "Mon, Wed, Fri at 07:30"),
        (sched.every(1), "Every minute"),
        (sched.every(15), "Every 15 minutes"),
        (sched.every(60), "Every hour"),
        (sched.every(180), "Every 3 hours"),
    ],
)
def test_describe(schedule, text):
    assert sched.describe(schedule) == text
