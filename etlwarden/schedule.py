"""Schedules: when a job runs next.

A schedule is a small dict, stored as JSON in the job list:

    {"type": "daily", "time": "06:00", "days": [0, 1, 2, 3, 4]}   Mon-Fri at 06:00
    {"type": "interval", "minutes": 15}                          every 15 minutes

Days are numbered like Python's date.weekday(): Monday is 0, Sunday is 6.

Run times are epoch seconds, like time.time(), but every schedule is about
clock times on this computer: a 06:00 job stays at 06:00 when daylight saving
time starts or ends. Intervals divide the day evenly and start at midnight, so
every 15 minutes means :00, :15, :30 and :45, and every 6 hours means 00:00,
06:00, 12:00 and 18:00.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS = [0, 1, 2, 3, 4]
MINUTES_PER_DAY = 24 * 60
# The intervals the window offers. Any other divisor of a day works too.
INTERVAL_CHOICES = [1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440]


class ScheduleError(ValueError):
    """A schedule is incomplete or impossible. The message is shown to people."""


def daily(at: str, days: list[int] | None = None) -> dict:
    """A schedule that runs at a clock time on the given days (default: every day)."""
    return validate({"type": "daily", "time": at, "days": list(range(7)) if days is None else days})


def every(minutes: int) -> dict:
    """A schedule that runs every few minutes."""
    return validate({"type": "interval", "minutes": minutes})


def validate(schedule: dict) -> dict:
    """Check a schedule and return a clean copy, or raise ScheduleError."""
    kind = schedule.get("type")
    if kind == "daily":
        hour, minute = parse_time(schedule.get("time"))
        try:
            days = sorted({int(day) for day in schedule.get("days") or []})
        except (TypeError, ValueError):
            raise ScheduleError("Days must be numbers from 0 (Mon) to 6 (Sun).") from None
        if not days:
            raise ScheduleError("Pick at least one day.")
        if days[0] < 0 or days[-1] > 6:
            raise ScheduleError("Days must be numbers from 0 (Mon) to 6 (Sun).")
        return {"type": "daily", "time": f"{hour:02d}:{minute:02d}", "days": days}
    if kind == "interval":
        try:
            minutes = int(schedule.get("minutes"))
        except (TypeError, ValueError):
            raise ScheduleError("The interval must be a whole number of minutes.") from None
        if not 1 <= minutes <= MINUTES_PER_DAY or MINUTES_PER_DAY % minutes:
            raise ScheduleError(
                "The interval must divide a day evenly, such as 5, 15 or 30 minutes, or 2, 6 or 12 hours."
            )
        return {"type": "interval", "minutes": minutes}
    raise ScheduleError(f"Unknown schedule type: {kind!r}")


def parse_time(value: object) -> tuple[int, int]:
    """Turn "06:00" into (6, 0)."""
    try:
        hour_text, minute_text = str(value).strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise ScheduleError(f"Times look like 06:00, not {value!r}.") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"{value!r} isn't a time of day.")
    return hour, minute


def next_run_after(schedule: dict, after: float) -> float:
    """The first run time strictly after `after` (both in epoch seconds)."""
    schedule = validate(schedule)
    if schedule["type"] == "daily":
        return _next_daily(schedule, after)
    return _next_interval(schedule, after)


def _next_daily(schedule: dict, after: float) -> float:
    at = time(*parse_time(schedule["time"]))
    first_day = datetime.fromtimestamp(after).date()
    # Today plus the next 7 days always include one of the chosen days.
    for offset in range(8):
        day = first_day + timedelta(days=offset)
        if day.weekday() in schedule["days"]:
            candidate = datetime.combine(day, at).timestamp()
            if candidate > after:
                return candidate
    raise AssertionError("no run time found within 8 days")


def _next_interval(schedule: dict, after: float) -> float:
    # Walk the day's clock times (00:00, then every step) rather than adding seconds,
    # so a day with 23 or 25 hours doesn't shift the grid.
    step = timedelta(minutes=schedule["minutes"])
    today = datetime.fromtimestamp(after).date()
    for day in (today, today + timedelta(days=1)):
        slot = datetime.combine(day, time())
        while slot.date() == day:
            if slot.timestamp() > after:
                return slot.timestamp()
            slot += step
    raise AssertionError("no run time found within 2 days")


def describe(schedule: dict) -> str:
    """A short description for people, such as "Mon–Fri at 06:00"."""
    schedule = validate(schedule)
    if schedule["type"] == "interval":
        minutes = schedule["minutes"]
        if minutes % 60 == 0:
            hours = minutes // 60
            return "Every hour" if hours == 1 else f"Every {hours} hours"
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"
    return f"{_describe_days(schedule['days'])} at {schedule['time']}"


def summary(schedule: dict) -> str:
    """Like describe(), but never fails: for lists that show many jobs at once."""
    try:
        return describe(schedule)
    except ScheduleError:
        return "Invalid schedule, edit the job"


def _describe_days(days: list[int]) -> str:
    if len(days) == 7:
        return "Every day"
    if days == WEEKDAYS:
        return "Mon–Fri"
    return ", ".join(DAY_NAMES[day] for day in days)
