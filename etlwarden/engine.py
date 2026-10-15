"""The engine: runs in the background without a window and starts the jobs.

Every few seconds it looks at the job list and
  1. queues the jobs whose run time has come, and the ones someone clicked "Run now" for,
  2. starts queued jobs while fewer than "max_parallel" are running,
  3. writes each result back, so the window can show it.

The queue lives in the job list too (runs with status "queued"), so nothing
waiting in it is lost when the engine stops.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import runners, textfmt
from . import schedule as sched
from .db import Database, Job, Run, int_setting
from .lock import AlreadyRunning, SingleInstance
from .paths import DataDir

log = logging.getLogger("etlwarden.engine")

POLL_SECONDS = 5.0
# The engine looks every few seconds, so a run time it only notices much later
# was missed: the engine wasn't running, or the computer was off or asleep.
MISSED_AFTER_SECONDS = 120.0
PRUNE_EVERY_SECONDS = 3600.0
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Engine:
    def __init__(
        self,
        data: DataDir,
        *,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.data = data
        self.db = Database(data.db)
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.started_at = clock()
        self._stop = threading.Event()
        self._workers: dict[int, Worker] = {}
        self._last_prune = 0.0

    def run_forever(self) -> None:
        self.recover()
        self.db.reset_engine_stop()
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("Something went wrong while checking the job list")
                if self.db.engine_stop_requested():
                    log.info("Stop requested from the window")
                    break
                self._stop.wait(self.poll_seconds)
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: float | None = None) -> None:
        """One look at the job list."""
        now = self.clock() if now is None else now
        self.db.engine_beat(os.getpid(), self.started_at, now)
        self._reap()
        if self.db.engine_stop_requested():
            return  # start nothing new; run_forever stops right after this
        self.db.fill_missing_next_runs(now)
        self._queue_due_jobs(now)
        self._queue_requested_runs(now)
        self._start_queued(now)
        if now - self._last_prune >= PRUNE_EVERY_SECONDS:
            self._prune()
            self._last_prune = now

    def recover(self) -> None:
        """Tidy up after an engine that stopped: its running jobs were stopped with it
        (Windows ends them together), and scheduled runs still waiting in its queue are
        dropped for jobs that have catching up switched off."""
        now = self.clock()
        count = self.db.mark_interrupted(now)
        if count:
            log.warning("Marked %d run(s) from a previous engine as interrupted", count)
        count = self.db.skip_stale_queued(now)
        if count:
            log.info("Skipped %d run(s) left waiting by a previous engine (catching up is off)", count)

    def shutdown(self) -> None:
        for worker in list(self._workers.values()):
            worker.cancel()
        for worker in list(self._workers.values()):
            worker.join(timeout=30)
        self._workers.clear()
        self.db.engine_stopped()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Wait until every started job has finished. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        for worker in list(self._workers.values()):
            worker.join(max(0.0, deadline - time.monotonic()))
        self._reap()
        return not self._workers

    def _queue_due_jobs(self, now: float) -> None:
        for job in self.db.due_jobs(now):
            try:
                next_run = sched.next_run_after(job.schedule, now)
            except sched.ScheduleError as exc:  # saved by an older version; don't block the others
                self.db.record_due(job.id, None, "schedule", "failed",
                                   message=f"The schedule needs fixing: {exc} Edit the job to set it again.", now=now)
                log.warning("%s: invalid schedule: %s", job.name, exc)
                continue
            missed = now - job.next_run > MISSED_AFTER_SECONDS
            started_by = "catch-up" if missed else "schedule"
            message = None
            if missed and not job.catch_up:
                status = "skipped"
                message = (f"Missed the run at {textfmt.when(job.next_run, now)}"
                           " and catching up is off for this job.")
            elif self.db.has_active_run(job.id):
                status = "skipped"
                message = "The previous run hadn't finished yet."
            else:
                status = "queued"
            self.db.record_due(job.id, next_run, started_by, status, message=message, now=now)
            log.info("%s: %s (%s)", job.name, status, started_by)

    def _queue_requested_runs(self, now: float) -> None:
        for job in self.db.take_run_requests():
            if self.db.has_active_run(job.id):
                self.db.add_run(job.id, "manual", "skipped",
                                message="This job was already waiting or running.", now=now)
            else:
                self.db.add_run(job.id, "manual", "queued", now=now)
                log.info("%s: queued (run now)", job.name)

    def _start_queued(self, now: float) -> None:
        queued = self.db.queued_runs()
        if not queued:
            return
        settings = self.db.settings()
        free_slots = max(1, int_setting(settings, "max_parallel")) - len(self._workers)
        for run in queued:
            if free_slots <= 0:
                break
            job = self.db.get_job(run.job_id)
            if job is None:
                continue
            try:
                command = runners.build_command(job.kind, job.path, settings)
            except runners.RunnerError as exc:
                self.db.finish_run(run.id, "failed", message=str(exc), now=now)
                log.warning("%s: can't start: %s", job.name, exc)
                continue
            log_path = self.data.run_log(job.id, run.id)
            self.db.mark_running(run.id, str(log_path), now)
            worker = Worker(self.db, run, job, command, log_path, self.clock)
            self._workers[run.id] = worker
            worker.start()
            free_slots -= 1
            log.info("%s: started", job.name)

    def _reap(self) -> None:
        for run_id, worker in list(self._workers.items()):
            if not worker.is_alive():
                del self._workers[run_id]

    def _prune(self) -> None:
        keep = max(1, int_setting(self.db.settings(), "keep_runs"))
        for log_path in self.db.prune_runs(keep):
            try:
                Path(log_path).unlink(missing_ok=True)
            except OSError as exc:  # e.g. still open in an editor; the rest still gets cleaned up
                log.warning("Couldn't delete old log %s: %s", log_path, exc)


class Worker(threading.Thread):
    """Runs one job in its own thread, so the engine keeps watching the clock meanwhile."""

    def __init__(
        self,
        db: Database,
        run: Run,
        job: Job,
        command: list[str],
        log_path: Path,
        clock: Callable[[], float],
    ) -> None:
        super().__init__(name=f"run-{run.id}", daemon=True)
        self.db = db
        self.run_id = run.id
        self.job = job
        self.command = command
        self.log_path = log_path
        self.clock = clock
        self.tree: runners.ProcessTree | None = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Stop the job, because the engine is shutting down."""
        self._cancelled.set()
        if self.tree is not None:
            self.tree.kill()

    def run(self) -> None:
        status, exit_code, message = "failed", None, None
        try:
            status, exit_code, message = self._run_job()
        except OSError as exc:
            message = f"Couldn't start: {exc}"
        except Exception as exc:  # a broken job must never take the engine down
            log.exception("Run %s failed unexpectedly", self.run_id)
            message = f"Unexpected error: {exc}"
        finally:
            if self.tree is not None:
                self.tree.release()
            self.db.finish_run(self.run_id, status, exit_code=exit_code, message=message,
                               job_id=self.job.id, now=self.clock())
            log.info("%s: %s", self.job.name, status)

    def _run_job(self) -> tuple[str, int | None, str | None]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        header = (f"Job:     {self.job.name}\n"
                  f"Command: {runners.display(self.command)}\n"
                  f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n").encode("utf-8")
        with open(self.log_path, "wb") as output:
            output.write(header)
            output.flush()
            path = Path(self.job.path)
            self.tree = runners.start(self.command, path if path.is_dir() else path.parent, output)
            if self._cancelled.is_set():
                self.tree.kill()
            status, exit_code, message = self._wait()
        if status == "failed":
            summary = runners.failure_summary(self.job.kind, _output_tail(self.log_path, skip=len(header)))
            if summary:
                message = f"{message}: {summary[:200]}"
        return status, exit_code, message

    def _wait(self) -> tuple[str, int | None, str | None]:
        limit = self.job.timeout_minutes * 60 or None
        process = self.tree.process
        try:
            exit_code = process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            self.tree.kill()
            exit_code = process.wait()
            return "timeout", exit_code, f"Stopped after the time limit of {textfmt.duration(limit)}."
        if self._cancelled.is_set():
            return "interrupted", exit_code, "Stopped because the engine was shut down."
        if exit_code == 0:
            return "ok", 0, None
        return "failed", exit_code, f"Exit code {exit_code}"


def _output_tail(path: Path, skip: int, size: int = 64 * 1024) -> str:
    """The end of what a job printed (after the log header), where the reason for a failure is."""
    try:
        with open(path, "rb") as log_file:
            start = max(skip, log_file.seek(0, os.SEEK_END) - size)
            log_file.seek(start)
            tail = log_file.read()
    except OSError:
        return ""
    if start > skip:
        tail = tail.partition(b"\n")[2]  # drop the line we landed in the middle of
    return textfmt.decode_output(tail)


def engine_command(data: DataDir) -> tuple[list[str], Path]:
    """The command that starts the engine as its own process, and the folder to start it in."""
    if getattr(sys, "frozen", False):  # the standalone etlwarden.exe
        program = Path(sys.executable)
        windowless = program.with_name("etlwarden.exe")  # rather than etlwarden-console.exe
        if windowless.exists():
            program = windowless
        return [str(program), "--data-dir", str(data.root), "engine"], program.parent
    python = Path(sys.executable)
    if sys.platform == "win32" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")  # no console window
    return [str(python), "-m", "etlwarden", "--data-dir", str(data.root), "engine"], PROJECT_ROOT


def start_in_background(data: DataDir) -> subprocess.Popen:
    """Start the engine as its own process. It keeps running after the window closes."""
    command, cwd = engine_command(data)
    options: dict = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )


def main(data: DataDir) -> int:
    """`etlwarden engine`: run until Ctrl+C or until the window asks it to stop."""
    _setup_logging(data.engine_log)
    try:
        with SingleInstance(data.engine_lock):
            engine = Engine(data)
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), lambda *_: engine.stop())
            log.info("Engine started, data folder: %s", data.root)
            engine.run_forever()
            log.info("Engine stopped")
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 1
    except Exception:  # the window only says "stopped right away", so the reason goes here
        log.exception("The engine stopped because of an error")
        return 1
    return 0


def _setup_logging(path: Path) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logger = logging.getLogger("etlwarden")
    logger.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
