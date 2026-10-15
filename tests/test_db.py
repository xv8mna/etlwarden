import time

from etlwarden import schedule as sched
from etlwarden.db import Database, int_setting
from tests.conftest import ts


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
