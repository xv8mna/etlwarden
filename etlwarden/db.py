"""The job list: one SQLite file that the window and the engine share.

The window adds and edits jobs and reads the results. The engine reads the
jobs, records each run and writes the results. SQLite lets both programs use
the file at the same time, so they need no other connection to each other.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from . import schedule as sched

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    path            TEXT    NOT NULL,
    kind            TEXT    NOT NULL,             -- see runners.KINDS
    schedule        TEXT    NOT NULL,             -- JSON, see schedule.py
    enabled         INTEGER NOT NULL DEFAULT 1,
    timeout_minutes REAL    NOT NULL DEFAULT 60,  -- 0 means no time limit
    catch_up        INTEGER NOT NULL DEFAULT 1,   -- run once after a missed run time
    next_run        REAL,                         -- epoch seconds; NULL while disabled
    run_requested   INTEGER NOT NULL DEFAULT 0,   -- set by "Run now"
    created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    started_by  TEXT    NOT NULL,  -- schedule, manual or catch-up
    status      TEXT    NOT NULL,  -- queued, running, ok, failed, timeout, skipped, interrupted
    queued_at   REAL    NOT NULL,
    started_at  REAL,
    finished_at REAL,
    exit_code   INTEGER,
    log_path    TEXT,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS runs_by_job ON runs (job_id, id);
CREATE INDEX IF NOT EXISTS runs_by_status ON runs (status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engine (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    pid            INTEGER,
    started_at     REAL,
    heartbeat      REAL,
    stop_requested INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "max_parallel": "2",  # jobs that may run at the same time
    "keep_runs": "100",  # history entries kept per job
    "tafl_path": "",  # tafl.exe; empty means the one found on PATH
    "python_path": "",  # Python for .py jobs; empty means the python found on PATH (runners.default_python)
    "lua_path": "",  # lua.exe; empty means the one found on PATH
    "knime_path": "",  # knime.exe
    "hop_run_path": "",  # hop-run.bat or hop-run.sh
}

ACTIVE = ("queued", "running")

# The engine writes a heartbeat every few seconds; older than this means it stopped.
ENGINE_STALE_SECONDS = 20.0


@dataclass
class Job:
    id: int
    name: str
    path: str
    kind: str
    schedule: dict
    enabled: bool
    timeout_minutes: float
    catch_up: bool
    next_run: float | None
    run_requested: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            kind=row["kind"],
            schedule=json.loads(row["schedule"]),
            enabled=bool(row["enabled"]),
            timeout_minutes=row["timeout_minutes"],
            catch_up=bool(row["catch_up"]),
            next_run=row["next_run"],
            run_requested=bool(row["run_requested"]),
        )


@dataclass
class Run:
    id: int
    job_id: int
    started_by: str
    status: str
    queued_at: float
    started_at: float | None
    finished_at: float | None
    exit_code: int | None
    log_path: str | None
    message: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    @property
    def duration(self) -> float | None:
        """Seconds the run took, or has taken so far."""
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            # WAL lets the window read while the engine writes. It stays on in the file.
            conn.execute("PRAGMA journal_mode = WAL")
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                DEFAULT_SETTINGS.items(),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A short-lived connection that commits at the end and rolls back on errors."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # Jobs ---------------------------------------------------------------

    def add_job(
        self,
        name: str,
        path: str | Path,
        kind: str,
        schedule: dict,
        *,
        enabled: bool = True,
        timeout_minutes: float = 60,
        catch_up: bool = True,
        now: float | None = None,
    ) -> int:
        schedule = sched.validate(schedule)
        next_run = _first_run(schedule, enabled, now)
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO jobs (name, path, kind, schedule, enabled, timeout_minutes,
                                     catch_up, next_run, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, str(path), kind, json.dumps(schedule), int(enabled), timeout_minutes,
                 int(catch_up), next_run, time.time() if now is None else now),
            )
            return cursor.lastrowid

    def update_job(
        self,
        job_id: int,
        name: str,
        path: str | Path,
        kind: str,
        schedule: dict,
        *,
        enabled: bool,
        timeout_minutes: float,
        catch_up: bool,
        now: float | None = None,
    ) -> None:
        """Save an edited job. The next run time only changes if the schedule or the
        on/off switch did, so a missed run waiting for catch-up isn't lost by an edit."""
        schedule = sched.validate(schedule)
        old = self.get_job(job_id)
        fields = (name, str(path), kind, json.dumps(schedule), int(enabled), timeout_minutes, int(catch_up))
        with self.connect() as conn:
            if old is not None and old.enabled == enabled and _same_schedule(old.schedule, schedule):
                # Leave next_run alone: the engine may move it on at this very moment.
                conn.execute(
                    """UPDATE jobs SET name = ?, path = ?, kind = ?, schedule = ?, enabled = ?,
                                       timeout_minutes = ?, catch_up = ?
                       WHERE id = ?""",
                    (*fields, job_id),
                )
            else:
                conn.execute(
                    """UPDATE jobs SET name = ?, path = ?, kind = ?, schedule = ?, enabled = ?,
                                       timeout_minutes = ?, catch_up = ?, next_run = ?
                       WHERE id = ?""",
                    (*fields, _first_run(schedule, enabled, now), job_id),
                )

    def set_enabled(self, job_id: int, enabled: bool, now: float | None = None) -> None:
        job = self.get_job(job_id)
        if job is None:
            return
        try:
            next_run = _first_run(job.schedule, enabled, now)
        except sched.ScheduleError:
            next_run = None  # a broken schedule never comes due; editing the job fixes it
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET enabled = ?, next_run = ? WHERE id = ?",
                (int(enabled), next_run, job_id),
            )

    def delete_job(self, job_id: int, *, only_if_idle: bool = False) -> bool:
        """Delete a job and its history. With only_if_idle, nothing happens while one of its
        runs is waiting or running; the check and the delete are one step. Returns whether
        the job was deleted."""
        with self.connect() as conn:
            if only_if_idle:
                cursor = conn.execute(
                    """DELETE FROM jobs WHERE id = ?
                         AND NOT EXISTS (SELECT 1 FROM runs WHERE job_id = ? AND status IN (?, ?))""",
                    (job_id, job_id, *ACTIVE),
                )
            else:
                cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cursor.rowcount > 0

    def get_job(self, job_id: int) -> Job | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY name COLLATE NOCASE, id").fetchall()
        return [Job.from_row(row) for row in rows]

    def request_run(self, job_id: int) -> None:
        """Ask the engine to run a job as soon as possible ("Run now")."""
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET run_requested = 1 WHERE id = ?", (job_id,))

    # Used by the engine -------------------------------------------------

    def due_jobs(self, now: float) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ?
                   ORDER BY next_run, id""",
                (now,),
            ).fetchall()
        return [Job.from_row(row) for row in rows]

    def fill_missing_next_runs(self, now: float) -> None:
        """Give enabled jobs without a next run time one (normally the window sets it)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE enabled = 1 AND next_run IS NULL"
            ).fetchall()
            for row in rows:
                job = Job.from_row(row)
                try:
                    next_run = sched.next_run_after(job.schedule, now)
                except sched.ScheduleError:
                    continue  # waits for someone to fix the schedule in the window
                conn.execute("UPDATE jobs SET next_run = ? WHERE id = ?", (next_run, job.id))

    def take_run_requests(self) -> list[Job]:
        """Jobs someone clicked "Run now" for. Each request is handed out once."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE run_requested = 1 ORDER BY id").fetchall()
            conn.executemany(
                "UPDATE jobs SET run_requested = 0 WHERE id = ?", [(row["id"],) for row in rows]
            )
        return [Job.from_row(row) for row in rows]

    def record_due(
        self,
        job_id: int,
        next_run: float | None,
        started_by: str,
        status: str,
        *,
        message: str | None = None,
        now: float | None = None,
    ) -> int:
        """Handle a run time that has come: record the run and move next_run on, together."""
        with self.connect() as conn:
            run_id = _insert_run(conn, job_id, started_by, status, message, now)
            conn.execute("UPDATE jobs SET next_run = ? WHERE id = ?", (next_run, job_id))
        return run_id

    def add_run(
        self,
        job_id: int,
        started_by: str,
        status: str,
        *,
        message: str | None = None,
        now: float | None = None,
    ) -> int:
        with self.connect() as conn:
            return _insert_run(conn, job_id, started_by, status, message, now)

    def has_active_run(self, job_id: int) -> bool:
        """Whether the job is already waiting in the queue or running."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE job_id = ? AND status IN (?, ?) LIMIT 1",
                (job_id, *ACTIVE),
            ).fetchone()
        return row is not None

    def queued_runs(self) -> list[Run]:
        """The queue, oldest first."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY id").fetchall()
        return [Run.from_row(row) for row in rows]

    def mark_running(self, run_id: int, log_path: str, now: float | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = ?, log_path = ? WHERE id = ?",
                (time.time() if now is None else now, log_path, run_id),
            )

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        exit_code: int | None = None,
        message: str | None = None,
        job_id: int | None = None,
        now: float | None = None,
    ) -> None:
        """Record a run's result. With job_id, only if the run still belongs to that job."""
        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET status = ?, exit_code = ?, message = ?, finished_at = ?
                   WHERE id = ? AND (? IS NULL OR job_id = ?)""",
                (status, exit_code, message, time.time() if now is None else now, run_id, job_id, job_id),
            )

    def mark_interrupted(self, now: float | None = None) -> int:
        """Runs still marked running belong to an engine that stopped. Returns how many."""
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE runs SET status = 'interrupted', finished_at = ?,
                                   message = 'The engine stopped while this job was running.'
                   WHERE status = 'running'""",
                (time.time() if now is None else now,),
            )
            return cursor.rowcount

    def skip_stale_queued(self, now: float | None = None) -> int:
        """Scheduled runs left in the queue by a stopped engine are dropped for jobs with
        catching up switched off. "Run now" requests stay. Returns how many were dropped."""
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE runs SET status = 'skipped', finished_at = ?,
                                   message = 'Was waiting in the queue when the engine stopped, and catching up is off for this job.'
                   WHERE status = 'queued' AND started_by != 'manual'
                     AND job_id IN (SELECT id FROM jobs WHERE catch_up = 0)""",
                (time.time() if now is None else now,),
            )
            return cursor.rowcount

    def prune_runs(self, keep: int) -> list[str]:
        """Delete finished runs beyond the newest `keep` per job; return their log files."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, log_path FROM (
                       SELECT id, log_path, status,
                              ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY id DESC) AS position
                       FROM runs)
                   WHERE position > ? AND status NOT IN (?, ?)""",
                (keep, *ACTIVE),
            ).fetchall()
            conn.executemany("DELETE FROM runs WHERE id = ?", [(row["id"],) for row in rows])
        return [row["log_path"] for row in rows if row["log_path"]]

    # History ------------------------------------------------------------

    def runs(self, job_id: int | None = None, limit: int = 200) -> list[Run]:
        """Newest first, for one job or for all of them."""
        with self.connect() as conn:
            if job_id is None:
                rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit)
                ).fetchall()
        return [Run.from_row(row) for row in rows]

    def get_run(self, run_id: int) -> Run | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.from_row(row) if row else None

    def latest_runs(self) -> dict[int, Run]:
        """The run that best describes each job's state, by job id: a run that's waiting
        or running, otherwise the newest real run, and only then a skipped one."""
        with self.connect() as conn:
            # Starting from jobs sorts each job's runs once, which keeps this fast
            # (the window asks every 2 seconds).
            rows = conn.execute(
                """SELECT r.* FROM jobs AS j
                   JOIN runs AS r ON r.id = (SELECT x.id FROM runs AS x WHERE x.job_id = j.id
                                             ORDER BY x.status IN ('queued', 'running') DESC,
                                                      x.status != 'skipped' DESC, x.id DESC
                                             LIMIT 1)"""
            ).fetchall()
        return {row["job_id"]: Run.from_row(row) for row in rows}

    def count_running(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()[0]

    # Settings -----------------------------------------------------------

    def settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {**DEFAULT_SETTINGS, **{row["key"]: row["value"] for row in rows}}

    def save_settings(self, values: dict[str, object]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                [(key, str(value)) for key, value in values.items()],
            )

    # Engine state -------------------------------------------------------

    def engine_beat(self, pid: int, started_at: float, now: float) -> None:
        """The engine says "still here" on every check."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO engine (id, pid, started_at, heartbeat) VALUES (1, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET pid = excluded.pid,
                       started_at = excluded.started_at, heartbeat = excluded.heartbeat""",
                (pid, started_at, now),
            )

    def engine_state(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM engine WHERE id = 1").fetchone()
        return dict(row) if row else None

    def engine_alive(self, now: float | None = None) -> bool:
        state = self.engine_state()
        if not state or state["heartbeat"] is None:
            return False
        now = time.time() if now is None else now
        return now - state["heartbeat"] < ENGINE_STALE_SECONDS

    def request_engine_stop(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO engine (id, stop_requested) VALUES (1, 1)
                   ON CONFLICT (id) DO UPDATE SET stop_requested = 1"""
            )

    def engine_stop_requested(self) -> bool:
        state = self.engine_state()
        return bool(state and state["stop_requested"])

    def reset_engine_stop(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE engine SET stop_requested = 0 WHERE id = 1")

    def engine_stopped(self) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE engine SET pid = NULL, heartbeat = NULL, stop_requested = 0 WHERE id = 1"
            )


def int_setting(settings: dict[str, str], key: str) -> int:
    """A numeric setting, or its default when the stored value isn't a number."""
    try:
        return int(settings[key])
    except (KeyError, ValueError):
        return int(DEFAULT_SETTINGS[key])


def _same_schedule(stored: dict, new: dict) -> bool:
    try:
        return sched.validate(stored) == new
    except sched.ScheduleError:
        return False


def _first_run(schedule: dict, enabled: bool, now: float | None) -> float | None:
    if not enabled:
        return None
    return sched.next_run_after(schedule, time.time() if now is None else now)


def _insert_run(
    conn: sqlite3.Connection,
    job_id: int,
    started_by: str,
    status: str,
    message: str | None,
    now: float | None,
) -> int:
    now = time.time() if now is None else now
    # Runs that never start (skipped) are finished the moment they're recorded.
    finished_at = None if status in ACTIVE else now
    cursor = conn.execute(
        """INSERT INTO runs (job_id, started_by, status, queued_at, finished_at, message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (job_id, started_by, status, now, finished_at, message),
    )
    return cursor.lastrowid
