import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific behaviour")

from etlwarden import engine as engine_module
from etlwarden import schedule as sched
from etlwarden.db import Database
from etlwarden.engine import Engine
from etlwarden.lock import AlreadyRunning, SingleInstance


def make_engine(data) -> Engine:
    return Engine(data, poll_seconds=0.05)


def run_now(engine: Engine, job_id: int):
    """Click "Run now", let the engine pick it up and wait for the result."""
    engine.db.request_run(job_id)
    engine.tick()
    assert engine.wait_idle(30)
    return engine.db.runs(job_id)[0]


def wait_for(condition, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_a_successful_job_is_logged(data, script):
    path = script("hello.py", "print('hello from the job')")
    engine = make_engine(data)
    job_id = engine.db.add_job("Hello", path, "python", sched.every(60))

    run = run_now(engine, job_id)

    assert (run.status, run.exit_code, run.started_by) == ("ok", 0, "manual")
    log = Path(run.log_path).read_text(encoding="utf-8")
    assert "hello from the job" in log
    assert "Job:     Hello" in log


def test_a_failing_job_keeps_its_exit_code(data, script):
    path = script("fail.py", "import sys\nprint('oops')\nsys.exit(3)")
    engine = make_engine(data)
    job_id = engine.db.add_job("Fail", path, "python", sched.every(60))

    run = run_now(engine, job_id)

    assert (run.status, run.exit_code) == ("failed", 3)
    assert run.message == "Exit code 3: oops"  # the last thing the job printed says why


def test_a_silent_failure_shows_only_the_exit_code(data, script):
    path = script("quiet.py", "import sys\nsys.exit(2)")
    engine = make_engine(data)
    job_id = engine.db.add_job("Quiet", path, "python", sched.every(60))

    run = run_now(engine, job_id)

    assert run.message == "Exit code 2"


def test_the_time_limit_stops_a_hanging_job(data, script):
    path = script("hang.py", "import time\ntime.sleep(60)")
    engine = make_engine(data)
    job_id = engine.db.add_job("Hang", path, "python", sched.every(60), timeout_minutes=0.02)

    run = run_now(engine, job_id)

    assert run.status == "timeout"
    assert run.finished_at - run.started_at < 15


def test_a_due_job_runs_and_its_next_run_moves_on(data, script):
    engine = make_engine(data)
    job_id = engine.db.add_job("Tick", script("ok.py"), "python", sched.every(60))
    now = engine.db.get_job(job_id).next_run + 1

    engine.tick(now)
    assert engine.wait_idle(30)

    run = engine.db.runs(job_id)[0]
    assert (run.status, run.started_by) == ("ok", "schedule")
    assert engine.db.get_job(job_id).next_run > now


def test_a_missed_run_is_skipped_when_catching_up_is_off(data, script):
    engine = make_engine(data)
    job_id = engine.db.add_job("Nightly", script("ok.py"), "python", sched.every(60), catch_up=False)
    an_hour_late = engine.db.get_job(job_id).next_run + 3600

    engine.tick(an_hour_late)

    run = engine.db.runs(job_id)[0]
    assert run.status == "skipped"
    assert "Missed" in run.message
    assert engine.wait_idle(1)


def test_missed_runs_catch_up_once(data, script):
    engine = make_engine(data)
    job_id = engine.db.add_job("Nightly", script("ok.py"), "python", sched.every(60))
    three_days_late = engine.db.get_job(job_id).next_run + 3 * 86400

    engine.tick(three_days_late)
    assert engine.wait_idle(30)

    runs = engine.db.runs(job_id)
    assert [(run.started_by, run.status) for run in runs] == [("catch-up", "ok")]


def test_a_job_that_is_still_running_is_not_started_twice(data, script):
    engine = make_engine(data)
    job_id = engine.db.add_job("Slow", script("slow.py", "import time\ntime.sleep(3)"), "python", sched.every(1))
    engine.tick(engine.db.get_job(job_id).next_run + 1)
    engine.tick(engine.db.get_job(job_id).next_run + 1)  # due again while the first run is busy

    assert [run.status for run in engine.db.runs(job_id)] == ["skipped", "running"]
    assert engine.wait_idle(30)


def test_the_parallel_limit_queues_the_rest(data, script):
    engine = make_engine(data)
    engine.db.save_settings({"max_parallel": 1})
    slow = script("slow.py", "import time\ntime.sleep(2)")
    first = engine.db.add_job("First", slow, "python", sched.every(60))
    second = engine.db.add_job("Second", slow, "python", sched.every(60))
    engine.db.request_run(first)
    engine.db.request_run(second)

    engine.tick()
    assert engine.db.runs(first)[0].status == "running"
    assert engine.db.runs(second)[0].status == "queued"

    def second_done() -> bool:
        engine.tick()
        return engine.db.runs(second)[0].status == "ok"

    assert wait_for(second_done)
    assert engine.wait_idle(30)


def test_a_missing_program_path_fails_with_a_clear_message(data, script):
    engine = make_engine(data)
    job_id = engine.db.add_job("Flow", script("flow.knwf"), "knime", sched.every(60))

    run = run_now(engine, job_id)

    assert run.status == "failed"
    assert "KNIME" in run.message


@pytest.mark.skipif(sys.platform != "win32", reason="batch files are a Windows thing")
def test_a_batch_file_in_a_folder_with_spaces(data, tmp_path):
    folder = tmp_path / "my jobs"
    folder.mkdir()
    batch = folder / "say hello.bat"
    batch.write_text("@echo off\r\necho hello from cmd\r\n", encoding="ascii")
    engine = make_engine(data)
    job_id = engine.db.add_job("Batch", batch, "program", sched.every(60))

    run = run_now(engine, job_id)

    assert run.status == "ok"
    assert "hello from cmd" in Path(run.log_path).read_text(encoding="utf-8", errors="replace")


@windows_only
@pytest.mark.parametrize("exit_code, status", [(0, "ok"), (1, "failed")])
def test_a_tafl_project_runs_and_its_failure_is_explained(data, tmp_path, exit_code, status):
    # A stand-in for tafl.exe: a .cmd that calls a Python script printing what TAFL prints.
    fake = tmp_path / "fake_tafl.py"
    fake.write_text(
        "import sys\n"
        "print('called with', sys.argv[1:])\n"
        "print('Orders')\n"
        f"print('  {'✓' if exit_code == 0 else '✗'} 1. Import orders.csv')\n"
        "print('          File not found: orders.csv')\n"
        f"print('{'Done' if exit_code == 0 else 'Failed'} in 0.07s')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    tafl = tmp_path / "tafl.cmd"
    tafl.write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="ascii")
    project = tmp_path / "sales.tafl"
    project.write_text("{}")
    engine = make_engine(data)
    engine.db.save_settings({"tafl_path": str(tafl)})
    job_id = engine.db.add_job("Sales", project, "tafl", sched.daily("06:00"))

    run = run_now(engine, job_id)

    assert run.status == status
    log = Path(run.log_path).read_text(encoding="utf-8")
    assert "called with ['run'," in log and "sales.tafl" in log
    if status == "failed":
        assert run.message == "Exit code 1: Orders › 1. Import orders.csv: File not found: orders.csv"


def test_a_restart_marks_unfinished_runs_as_interrupted(data, script):
    db = Database(data.db)
    job_id = db.add_job("Hello", script("ok.py"), "python", sched.every(60))
    run_id = db.add_run(job_id, "manual", "queued")
    db.mark_running(run_id, "run.log")

    Engine(data).recover()

    assert db.get_run(run_id).status == "interrupted"


def test_only_one_engine_per_data_folder(data):
    with SingleInstance(data.engine_lock):
        with pytest.raises(AlreadyRunning):
            SingleInstance(data.engine_lock).acquire()
    with SingleInstance(data.engine_lock):  # free again once released
        pass


def test_the_window_can_stop_the_engine(data):
    engine = make_engine(data)
    loop = threading.Thread(target=engine.run_forever)
    loop.start()
    assert wait_for(engine.db.engine_alive, timeout=10)

    engine.db.request_engine_stop()
    loop.join(10)

    assert not loop.is_alive()
    assert not engine.db.engine_alive()


def test_the_background_engine_runs_jobs_and_stops(data, script):
    """The real thing: a separate engine process, as the window's Start button launches it."""
    db = Database(data.db)
    job_id = db.add_job("Hello", script("hello.py", "print('from the background')"), "python", sched.every(60))
    db.request_run(job_id)

    process = engine_module.start_in_background(data)
    try:
        assert wait_for(lambda: db.runs(job_id) and db.runs(job_id)[0].status == "ok", timeout=60)
        assert db.engine_alive()
        db.request_engine_stop()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert not db.engine_alive()
    assert "Engine started" in data.engine_log.read_text(encoding="utf-8")


@windows_only
@pytest.mark.parametrize(
    "body",
    [
        "dir C:\\does-not-exist >nul 2>nul || exit /b",
        "dir C:\\does-not-exist >nul 2>nul\r\nif errorlevel 1 goto :eof\r\necho not reached",
    ],
)
def test_a_failing_batch_file_is_recorded_as_failed(data, tmp_path, body):
    batch = tmp_path / "fails.bat"
    batch.write_text(f"@echo off\r\n{body}\r\n", encoding="ascii")
    engine = make_engine(data)
    job_id = engine.db.add_job("Fails", batch, "program", sched.every(60))

    run = run_now(engine, job_id)

    assert run.status == "failed"
    assert run.exit_code != 0


@windows_only
def test_a_batch_file_in_a_folder_with_an_ampersand(data, tmp_path):
    folder = tmp_path / "R&D"
    folder.mkdir()
    batch = folder / "load.bat"
    batch.write_text("@echo off\r\necho loaded\r\n", encoding="ascii")
    engine = make_engine(data)
    job_id = engine.db.add_job("R&D load", batch, "program", sched.every(60))

    run = run_now(engine, job_id)

    assert run.status == "ok"
    assert "loaded" in Path(run.log_path).read_text(encoding="utf-8", errors="replace")


def test_a_stop_request_starts_nothing_new(data, script):
    engine = make_engine(data)
    job_id = engine.db.add_job("Hello", script("ok.py"), "python", sched.every(60))
    engine.db.request_run(job_id)
    engine.db.request_engine_stop()

    engine.tick()

    assert engine.db.runs(job_id) == []
    assert engine.db.get_job(job_id).run_requested  # still waiting for the next engine


def test_a_restart_drops_queued_scheduled_runs_when_catching_up_is_off(data, script):
    db = Database(data.db)
    strict = db.add_job("Strict", script("a.py"), "python", sched.every(60), catch_up=False)
    relaxed = db.add_job("Relaxed", script("b.py"), "python", sched.every(60))
    stale = db.add_run(strict, "schedule", "queued")
    manual = db.add_run(strict, "manual", "queued")
    kept = db.add_run(relaxed, "schedule", "queued")

    Engine(data).recover()

    assert db.get_run(stale).status == "skipped"
    assert db.get_run(manual).status == "queued"
    assert db.get_run(kept).status == "queued"


def test_a_broken_schedule_does_not_block_other_jobs(data, script):
    engine = make_engine(data)
    good = engine.db.add_job("Good", script("good.py"), "python", sched.every(60))
    bad = engine.db.add_job("Bad", script("bad.py"), "python", sched.every(60))
    with engine.db.connect() as conn:  # as an older version might have saved it
        conn.execute("UPDATE jobs SET schedule = ? WHERE id = ?", ('{"type": "interval", "minutes": 7}', bad))

    now = engine.db.get_job(good).next_run + 1
    engine.tick(now)
    assert engine.wait_idle(30)
    engine.tick(now + 5)  # and the next look at the job list works too

    assert engine.db.runs(good)[0].status == "ok"
    bad_run = engine.db.runs(bad)[0]
    assert bad_run.status == "failed"
    assert "schedule" in bad_run.message
    assert engine.db.get_job(bad).next_run is None
    assert len(engine.db.runs(bad)) == 1


@windows_only
def test_pruning_carries_on_past_a_log_that_is_in_use(data, script):
    engine = make_engine(data)
    engine.db.save_settings({"keep_runs": 1})
    job_id = engine.db.add_job("Hello", script("ok.py"), "python", sched.every(60))
    logs = []
    for number in range(3):
        run_id = engine.db.add_run(job_id, "manual", "queued")
        log = data.root / f"run-{number}.log"
        log.write_text("output")
        engine.db.mark_running(run_id, str(log))
        engine.db.finish_run(run_id, "ok", exit_code=0)
        logs.append(log)

    with open(logs[0], "rb"):  # Windows won't delete a file that is open
        engine._prune()

    assert [log.exists() for log in logs] == [True, False, True]
    assert len(engine.db.runs(job_id)) == 1


def _alive(pid: int) -> bool:
    found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in found


@windows_only
def test_jobs_end_with_the_engine_even_when_it_is_killed(data, script, tmp_path):
    pid_file = tmp_path / "job.pid"
    job = script("long.py", f"import os, time\nopen(r'{pid_file}', 'w').write(str(os.getpid()))\ntime.sleep(60)")
    db = Database(data.db)
    job_id = db.add_job("Long", job, "python", sched.every(60))
    db.request_run(job_id)

    process = engine_module.start_in_background(data)
    engine_pid = None
    try:
        assert wait_for(lambda: pid_file.exists() and pid_file.read_text(), timeout=60)
        job_pid = int(pid_file.read_text())
        engine_pid = db.engine_state()["pid"]
        subprocess.run(["taskkill", "/F", "/PID", str(engine_pid)], capture_output=True)  # like "End task"

        assert wait_for(lambda: not _alive(job_pid), timeout=15)
    finally:
        if engine_pid and _alive(engine_pid):
            subprocess.run(["taskkill", "/F", "/PID", str(engine_pid)], capture_output=True)
        if process.poll() is None:
            process.kill()

    Engine(data).recover()  # the next engine finds the run cut short, and nothing else running
    assert db.runs(job_id)[0].status == "interrupted"
