import os
import sys
import time

import pytest

from etlwarden import notify
from etlwarden import schedule as sched
from etlwarden.engine import Engine

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific behaviour")


def alerting_engine(data, sent, **options) -> Engine:
    return Engine(data, poll_seconds=0.05, notify=lambda title, text: sent.append((title, text)), **options)


def run_now(engine: Engine, job_id: int):
    engine.db.request_run(job_id)
    engine.tick()
    assert engine.wait_idle(30)
    return engine.db.runs(job_id)[0]


def run_on_schedule(engine: Engine, job_id: int):
    """A run the clock started, not a click."""
    engine.db.add_run(job_id, "schedule", "queued")
    engine.tick()
    assert engine.wait_idle(30)
    return engine.db.runs(job_id)[0]


def test_a_failed_job_sends_its_name_and_the_reason(data, script):
    path = script("fail.py", "import sys\nprint('the server is gone')\nsys.exit(1)")
    sent = []
    engine = alerting_engine(data, sent)
    job_id = engine.db.add_job("Sales export", path, "python", sched.every(60))

    run = run_now(engine, job_id)

    assert run.status == "failed"
    assert sent == [("etlwarden: Sales export", "Exit code 1: the server is gone")]


def test_the_time_limit_sends_a_notification_that_says_so(data, script):
    path = script("hang.py", "import time\ntime.sleep(60)")
    sent = []
    engine = alerting_engine(data, sent)
    job_id = engine.db.add_job("Hang", path, "python", sched.every(60), timeout_minutes=0.02)

    run_now(engine, job_id)

    assert [title for title, _ in sent] == ["etlwarden: Hang"]
    assert "time limit" in sent[0][1]


def test_a_job_that_cannot_start_sends_a_notification(data):
    sent = []
    engine = alerting_engine(data, sent)
    job_id = engine.db.add_job("Unknown", "x.abc", "no-such-kind", sched.every(60))

    run_now(engine, job_id)

    assert [title for title, _ in sent] == ["etlwarden: Unknown"]


@pytest.mark.parametrize("code, options, status", [
    ("print('fine')", {}, "ok"),
    ("import sys\nsys.exit(100)", {"nothing_code": 100}, "nothing"),
])
def test_a_run_that_did_not_fail_sends_nothing(data, script, code, options, status):
    sent = []
    engine = alerting_engine(data, sent)
    job_id = engine.db.add_job("Fine", script("fine.py", code), "python", sched.every(60), **options)

    assert run_now(engine, job_id).status == status
    assert sent == []


def test_the_engine_stopping_a_job_sends_nothing(data, script):
    path = script("hang.py", "import time\ntime.sleep(60)")
    sent = []
    engine = alerting_engine(data, sent)
    job_id = engine.db.add_job("Hang", path, "python", sched.every(60))
    engine.db.request_run(job_id)
    engine.tick()
    time.sleep(0.5)

    engine.shutdown()

    assert engine.db.runs(job_id)[0].status == "interrupted"
    assert sent == []


def test_the_setting_switches_it_off(data, script):
    path = script("fail.py", "import sys\nsys.exit(1)")
    sent = []
    engine = alerting_engine(data, sent)
    engine.db.save_settings({"notify_failures": 0})
    job_id = engine.db.add_job("Fail", path, "python", sched.every(60))

    assert run_now(engine, job_id).status == "failed"
    assert sent == []


def repeated_failures(data, *, minutes_apart: int, hours: int, started_by: str = "schedule") -> int:
    """The limit alone, with a fake clock: one failure every few minutes."""
    now = [1000.0]
    sent = []
    engine = alerting_engine(data, sent, clock=lambda: now[0])
    job = engine.db.get_job(engine.db.add_job("Fail", "fail.py", "python", sched.every(60)))
    for _ in range(hours * 60 // minutes_apart):
        engine.alert(job, started_by, "failed", "Exit code 1")
        now[0] += minutes_apart * 60
    return len(sent)


@pytest.mark.parametrize("minutes_apart, hours, expected", [(5, 72, 4), (1, 48, 3), (24 * 60, 240, 10)])
def test_a_job_that_keeps_failing_is_limited(data, minutes_apart, hours, expected):
    assert repeated_failures(data, minutes_apart=minutes_apart, hours=hours) == expected


def test_run_now_always_sends(data):
    assert repeated_failures(data, minutes_apart=1, hours=1, started_by="manual") == 60


def test_the_limit_is_per_job(data):
    sent = []
    engine = alerting_engine(data, sent, clock=lambda: 5000.0)
    first = engine.db.get_job(engine.db.add_job("A", "a.py", "python", sched.every(60)))
    second = engine.db.get_job(engine.db.add_job("B", "b.py", "python", sched.every(60)))

    engine.alert(first, "schedule", "failed", "Exit code 1")
    engine.alert(second, "schedule", "failed", "Exit code 1")

    assert [title for title, _ in sent] == ["etlwarden: A", "etlwarden: B"]


def test_a_try_that_did_not_work_does_not_count(data):
    calls = []

    def flaky(title, text):
        calls.append(title)
        if len(calls) == 1:
            raise OSError("no notification today")

    engine = Engine(data, notify=flaky, clock=lambda: 5000.0)
    job = engine.db.get_job(engine.db.add_job("Fail", "fail.py", "python", sched.every(60)))

    for _ in range(3):
        engine.alert(job, "schedule", "failed", "Exit code 1")

    assert len(calls) == 2  # the failed try, then one that worked, then the limit


def test_a_notification_that_breaks_harms_nothing(data, script):
    path = script("fail.py", "import sys\nsys.exit(1)")

    def broken(title, text):
        raise RuntimeError("no notification today")

    engine = Engine(data, poll_seconds=0.05, notify=broken)
    job_id = engine.db.add_job("Fail", path, "python", sched.every(60))

    assert run_now(engine, job_id).status == "failed"
    assert run_now(engine, job_id).status == "failed"


def test_the_result_is_saved_before_the_notification(data, script):
    path = script("fail.py", "import sys\nsys.exit(1)")
    seen = []
    engine = Engine(data, poll_seconds=0.05, notify=lambda title, text: seen.append(engine.db.runs(job_id)[0].status))
    job_id = engine.db.add_job("Fail", path, "python", sched.every(60))

    run_on_schedule(engine, job_id)

    assert seen == ["failed"]


def test_a_broken_schedule_sends_one_notification(data):
    now = [time.time()]
    sent = []
    engine = alerting_engine(data, sent, clock=lambda: now[0])
    job_id = engine.db.add_job("Bad", "x.py", "python", sched.every(15))
    with engine.db.connect() as conn:  # as a newer version could leave it
        conn.execute("UPDATE jobs SET schedule = ?, next_run = ? WHERE id = ?",
                     ('{"type": "from-the-future"}', now[0] - 1, job_id))

    engine.tick()
    engine.tick()

    assert [title for title, _ in sent] == ["etlwarden: Bad"]


def toast_xml(title, text):
    """The real PowerShell path, up to the point where Windows would show it."""
    try:
        return notify.toast_xml(title, text)
    except RuntimeError as exc:
        if os.environ.get("GITHUB_ACTIONS"):  # GitHub's Windows has no desktop to notify
            pytest.skip(f"No notifications here: {exc}")
        raise


@windows_only
def test_the_message_reaches_windows_as_plain_text():
    xml = toast_xml("etlwarden: R&D <load>", 'Exit code 1: "quotes" & $(Write-Output hacked) `n')

    assert "<text>etlwarden: R&amp;D &lt;load&gt;</text>" in xml
    assert "$(Write-Output hacked)" in xml  # as text, nothing ran it


@windows_only
def test_umlauts_survive_the_trip():
    assert "Größe €" in toast_xml("Größe €", "ok")


@windows_only
def test_characters_windows_would_refuse_are_dropped():
    xml = toast_xml("title", "a\x00b\x1bc￾d￿e\ud800f")

    assert "abcdef" in xml


@windows_only
def test_a_long_reason_is_cut_off():
    xml = toast_xml("title", "x" * 1000)

    assert "x" * 199 + "…" in xml
    assert "x" * 201 not in xml
