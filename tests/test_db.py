import sqlite3
import time

import pytest

from etlwarden import db as db_module
from etlwarden import schedule as sched
from etlwarden.db import SCHEMA_VERSION, Database, NewerJobList, int_setting
from tests.conftest import ts

# The job list as versions 0.1.0 and 0.1.1 left it; 0.1.1 adds the two drive columns.
JOB_LIST_0_1 = """
CREATE TABLE jobs (id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
    schedule TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, timeout_minutes REAL NOT NULL DEFAULT 60,
    catch_up INTEGER NOT NULL DEFAULT 1, next_run REAL, run_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL);
CREATE TABLE runs (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    started_by TEXT NOT NULL, status TEXT NOT NULL, queued_at REAL NOT NULL, started_at REAL, finished_at REAL,
    exit_code INTEGER, log_path TEXT, message TEXT);
CREATE INDEX runs_by_job ON runs (job_id, id);
CREATE INDEX runs_by_status ON runs (status);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE engine (id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, started_at REAL, heartbeat REAL,
    stop_requested INTEGER NOT NULL DEFAULT 0);
INSERT INTO jobs (id, name, path, kind, schedule, created_at)
    VALUES (1, 'Load', 'load.py', 'python', '{"type": "interval", "minutes": 15}', 1);
INSERT INTO runs (id, job_id, started_by, status, queued_at) VALUES (1, 1, 'manual', 'ok', 1), (2, 1, 'manual', 'ok', 1);
"""


@pytest.fixture(params=["0.1.0", "0.1.1"])
def old_list(request, data):
    with sqlite3.connect(data.db) as conn:
        conn.executescript(JOB_LIST_0_1)
        if request.param == "0.1.1":
            conn.execute("ALTER TABLE runs ADD COLUMN drive TEXT")
            conn.execute("ALTER TABLE runs ADD COLUMN share TEXT")
    conn.close()
    return data.db


def stored(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_a_new_job_gets_its_next_run(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "C:/jobs/sales.py", "python", sched.daily("06:00"), now=ts("2026-10-14T05:00"))
    job = db.get_job(job_id)
    assert job.next_run == ts("2026-10-14T06:00")
    assert job.enabled and job.catch_up
    assert job.schedule == {"type": "daily", "time": "06:00", "days": [0, 1, 2, 3, 4, 5, 6]}


def test_disabling_clears_the_next_run_and_enabling_sets_it_again(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "sales.py", "python", sched.every(15))
    db.set_enabled(job_id, False)
    assert db.get_job(job_id).next_run is None
    db.set_enabled(job_id, True, now=ts("2026-10-14T06:07"))
    assert db.get_job(job_id).next_run == ts("2026-10-14T06:15")


def test_editing_a_job(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "sales.py", "python", sched.every(15))
    db.update_job(job_id, "Sales (daily)", "sales.py", "python", sched.daily("07:00"),
                  enabled=True, timeout_minutes=5, catch_up=False, now=ts("2026-10-14T05:00"))
    job = db.get_job(job_id)
    assert (job.name, job.timeout_minutes, job.catch_up) == ("Sales (daily)", 5, False)
    assert job.next_run == ts("2026-10-14T07:00")


def test_run_requests_are_handed_out_once(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "sales.py", "python", sched.every(15))
    db.request_run(job_id)
    assert [job.id for job in db.take_run_requests()] == [job_id]
    assert db.take_run_requests() == []


def test_deleting_a_job_deletes_its_history(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "sales.py", "python", sched.every(15))
    db.add_run(job_id, "manual", "skipped")
    db.delete_job(job_id)
    assert db.runs() == []


def test_settings_have_defaults_and_can_be_changed(data):
    db = Database(data.db)
    assert int_setting(db.settings(), "max_parallel") == 2
    db.save_settings({"max_parallel": 4, "knime_path": "C:/KNIME/knime.exe"})
    settings = db.settings()
    assert int_setting(settings, "max_parallel") == 4
    assert settings["knime_path"] == "C:/KNIME/knime.exe"
    db.save_settings({"max_parallel": "lots"})
    assert int_setting(db.settings(), "max_parallel") == 2


def test_pruning_keeps_the_newest_runs_and_anything_unfinished(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "sales.py", "python", sched.every(15))
    finished = []
    for number in range(5):
        run_id = db.add_run(job_id, "schedule", "queued")
        db.mark_running(run_id, f"run-{number}.log")
        db.finish_run(run_id, "ok", exit_code=0)
        finished.append(run_id)
    waiting = db.add_run(job_id, "manual", "queued")

    deleted_logs = db.prune_runs(keep=2)

    assert sorted(deleted_logs) == ["run-0.log", "run-1.log", "run-2.log", "run-3.log"]
    assert [run.id for run in db.runs(job_id)] == [waiting, finished[4]]


def test_editing_keeps_a_missed_run_waiting_for_catch_up(data):
    db = Database(data.db)
    job_id = db.add_job("Sales", "sales.py", "python", sched.daily("06:00"), now=ts("2026-10-14T05:00"))
    # The PC was off at 06:00; at 08:00 only the time limit is changed.
    db.update_job(job_id, "Sales", "sales.py", "python", sched.daily("06:00"),
                  enabled=True, timeout_minutes=90, catch_up=True, now=ts("2026-10-14T08:00"))
    assert db.get_job(job_id).next_run == ts("2026-10-14T06:00")
    # A new time does move the next run.
    db.update_job(job_id, "Sales", "sales.py", "python", sched.daily("07:00"),
                  enabled=True, timeout_minutes=90, catch_up=True, now=ts("2026-10-14T08:00"))
    assert db.get_job(job_id).next_run == ts("2026-10-15T07:00")


def test_the_job_list_shows_what_a_job_is_really_doing(data):
    db = Database(data.db)
    job_id = db.add_job("Slow", "slow.py", "python", sched.every(15))
    running = db.add_run(job_id, "schedule", "queued")
    db.mark_running(running, "run.log")
    db.add_run(job_id, "schedule", "skipped", message="The previous run hadn't finished yet.")
    assert db.latest_runs()[job_id].status == "running"
    db.finish_run(running, "ok", exit_code=0)
    assert db.latest_runs()[job_id].status == "ok"  # not the newer skipped one


def test_a_result_only_lands_on_the_job_it_belongs_to(data):
    db = Database(data.db)
    first = db.add_job("A", "a.py", "python", sched.every(15))
    second = db.add_job("B", "b.py", "python", sched.every(15))
    run_id = db.add_run(first, "manual", "queued")
    db.finish_run(run_id, "ok", exit_code=0, job_id=second)
    assert db.get_run(run_id).status == "queued"


def test_latest_run_per_job(data):
    db = Database(data.db)
    first = db.add_job("A", "a.py", "python", sched.every(15))
    second = db.add_job("B", "b.py", "python", sched.every(15))
    db.add_run(first, "manual", "skipped")
    newest = db.add_run(first, "manual", "queued")
    assert {job_id: run.id for job_id, run in db.latest_runs().items()} == {first: newest}
    assert second not in db.latest_runs()


def test_the_latest_runs_stay_quick_with_a_long_history(data):
    db = Database(data.db)
    with db.connect() as conn:
        for number in range(20):
            job_id = conn.execute(
                "INSERT INTO jobs (name, path, kind, schedule, created_at) VALUES (?, 'x.py', 'python', ?, 0)",
                (f"Job {number}", '{"type": "interval", "minutes": 1}'),
            ).lastrowid
            conn.executemany(
                "INSERT INTO runs (job_id, started_by, status, queued_at, finished_at) VALUES (?, 'schedule', ?, 0, 1)",
                [(job_id, "skipped" if n % 3 else "ok") for n in range(500)],
            )
    started = time.perf_counter()
    latest = db.latest_runs()
    assert len(latest) == 20
    assert time.perf_counter() - started < 0.5  # the window asks every 2 seconds


def test_deleting_only_if_idle_leaves_a_busy_job_alone(data):
    db = Database(data.db)
    job_id = db.add_job("Busy", "busy.py", "python", sched.every(15))
    db.add_run(job_id, "manual", "queued")
    assert not db.delete_job(job_id, only_if_idle=True)
    assert db.get_job(job_id) is not None
    assert db.delete_job(job_id)
    assert db.get_job(job_id) is None


def test_a_job_list_from_version_0_1_0_gets_the_new_columns(data):
    db = Database(data.db)
    job_id = db.add_job("Load", "load.bat", "program", sched.every(15))
    run_id = db.add_run(job_id, "manual", "queued")
    with db.connect() as conn:  # as 0.1.0 created it
        conn.execute("ALTER TABLE runs DROP COLUMN drive")
        conn.execute("ALTER TABLE runs DROP COLUMN share")
        conn.execute("PRAGMA user_version = 0")

    db = Database(data.db)
    db.mark_running(run_id, "run.log")
    db.set_run_drive(run_id, "Z:", r"\\server\etl")

    assert db.drives_of_running_runs() == [("Z:", r"\\server\etl")]
    assert db.get_run(run_id).status == "running"


def test_the_job_list_message_prefers_running_over_waiting(data):
    db = Database(data.db)
    job_id = db.add_job("Slow", "slow.py", "python", sched.every(1))
    assert db.active_status(job_id) is None
    db.add_run(job_id, "schedule", "queued")
    assert db.active_status(job_id) == "queued"
    running = db.add_run(job_id, "schedule", "queued")
    db.mark_running(running, "run.log")
    assert db.active_status(job_id) == "running"


def test_an_old_job_list_is_upgraded_with_everything_in_it(old_list):
    db = Database(old_list)

    assert stored(old_list, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert stored(old_list, "PRAGMA integrity_check") == [("ok",)]
    assert stored(old_list, "PRAGMA foreign_key_check") == []
    assert {name for (name,) in stored(old_list, "SELECT name FROM sqlite_master WHERE type = 'index'")} >= {
        "runs_by_job", "runs_by_status"}
    assert [run.id for run in db.runs(1)] == [2, 1]
    assert db.get_job(1).nothing_code is None
    db.delete_job(1)  # the link between jobs and runs survived the rebuild
    assert db.runs() == []


def test_a_run_id_never_comes_back_after_the_upgrade(old_list):
    db = Database(old_list)
    other = db.add_job("Other", "other.py", "python", sched.every(15))
    newest = db.add_run(other, "manual", "queued")
    db.delete_job(other)

    assert db.add_run(1, "manual", "queued") == newest + 1


def test_a_run_id_never_comes_back_in_a_new_job_list(data):
    db = Database(data.db)
    first = db.add_job("A", "a.py", "python", sched.every(15))
    second = db.add_job("B", "b.py", "python", sched.every(15))
    db.add_run(first, "manual", "queued")
    gone = db.add_run(second, "manual", "queued")
    db.delete_job(second)

    assert db.add_run(first, "manual", "queued") == gone + 1


def test_opening_a_job_list_again_changes_nothing(old_list):
    Database(old_list)
    before = stored(old_list, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    Database(old_list)
    assert stored(old_list, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == before


def test_a_failed_upgrade_changes_nothing_and_the_next_start_repairs_it(old_list, monkeypatch):
    columns = {row[1] for row in stored(old_list, "PRAGMA table_info(runs)")}
    monkeypatch.setattr(db_module, "ADDED_COLUMNS", (*db_module.ADDED_COLUMNS, ("missing", "column", "TEXT")))

    with pytest.raises(sqlite3.Error):
        Database(old_list)

    assert stored(old_list, "PRAGMA user_version") == [(0,)]
    assert {row[1] for row in stored(old_list, "PRAGMA table_info(runs)")} == columns
    assert "nothing_code" not in {row[1] for row in stored(old_list, "PRAGMA table_info(jobs)")}
    assert "AUTOINCREMENT" not in stored(old_list, "SELECT sql FROM sqlite_master WHERE name = 'runs'")[0][0]

    monkeypatch.undo()
    assert [run.id for run in Database(old_list).runs(1)] == [2, 1]


def test_a_job_list_from_a_newer_version_is_refused_and_left_alone(data):
    db = Database(data.db)
    job_id = db.add_job("Load", "load.py", "python", sched.every(15))
    with db.connect() as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(NewerJobList, match="newer version"):
        Database(data.db)

    assert stored(data.db, "PRAGMA user_version") == [(SCHEMA_VERSION + 1,)]
    assert stored(data.db, "SELECT id FROM jobs") == [(job_id,)]


def test_an_engine_says_which_job_list_it_knows(data):
    db = Database(data.db)
    assert not db.engine_outdated()  # no engine yet
    db.engine_beat(1, 2.0, 3.0)
    assert not db.engine_outdated()
    with db.connect() as conn:  # an engine from 0.1 writes no number
        conn.execute("UPDATE engine SET schema_version = NULL")
    assert db.engine_outdated()
    db.engine_beat(1, 2.0, 3.0)
    db.engine_stopped()
    assert db.engine_state()["schema_version"] is None


def test_the_nothing_to_do_code_is_saved_with_the_job(data):
    db = Database(data.db)
    job_id = db.add_job("Daily load", "load.py", "python", sched.every(15), nothing_code=100)
    assert db.get_job(job_id).nothing_code == 100
    db.update_job(job_id, "Daily load", "load.py", "python", sched.every(15),
                  enabled=True, timeout_minutes=60, catch_up=True, nothing_code=7)
    assert db.get_job(job_id).nothing_code == 7
    db.update_job(job_id, "Daily load", "load.py", "python", sched.every(30),
                  enabled=True, timeout_minutes=60, catch_up=True)
    assert db.get_job(job_id).nothing_code is None


@pytest.mark.parametrize("newer, shown", [
    ("skipped", "failed"), ("nothing", "failed"), ("ok", "ok"), ("timeout", "timeout"), ("queued", "queued"),
])
def test_only_a_real_result_replaces_a_failure_in_the_job_list(data, newer, shown):
    db = Database(data.db)
    job_id = db.add_job("Daily load", "load.py", "python", sched.every(15))
    db.finish_run(db.add_run(job_id, "schedule", "running"), "failed", exit_code=1)
    db.add_run(job_id, "schedule", newer)
    assert db.latest_runs()[job_id].status == shown


def test_nothing_to_do_does_not_replace_an_earlier_success(data):
    db = Database(data.db)
    job_id = db.add_job("Daily load", "load.py", "python", sched.every(15))
    db.finish_run(db.add_run(job_id, "schedule", "running"), "ok", exit_code=0)
    db.add_run(job_id, "schedule", "nothing")
    assert db.latest_runs()[job_id].status == "ok"


@pytest.mark.parametrize("filler", ["skipped", "nothing"])
def test_pruning_keeps_a_failure_the_job_list_still_shows(data, filler):
    db = Database(data.db)
    job_id = db.add_job("Daily load", "load.py", "python", sched.every(15))
    db.finish_run(db.add_run(job_id, "schedule", "running"), "failed", exit_code=1)
    for _ in range(150):
        db.add_run(job_id, "schedule", filler)

    db.prune_runs(keep=100)

    assert db.latest_runs()[job_id].status == "failed"
    assert len(db.runs(job_id)) == 101
