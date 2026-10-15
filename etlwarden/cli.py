"""Command line: `etlwarden` opens the window, `etlwarden engine` runs the engine."""

from __future__ import annotations

import argparse
import io
import sys
import time

from . import __version__, paths, textfmt
from . import schedule as sched
from .db import Database
from .paths import DataDir, DataDirError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="etlwarden", description="Pick a file, set a time, and it runs in the background."
    )
    parser.add_argument(
        "--data-dir",
        metavar="FOLDER",
        help="where the job list and logs live (default: ETLWARDEN_HOME, or your user data folder)",
    )
    parser.add_argument("--version", action="version", version=f"etlwarden {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("app", help="open the window (the default)")
    commands.add_parser("engine", help="run the engine in this console; Ctrl+C stops it")
    commands.add_parser("status", help="show whether the engine runs, and the job list")
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        # Redirected output uses the Windows code page; never crash on a job name it lacks.
        sys.stdout.reconfigure(errors="replace")
    try:
        data = paths.resolve(args.data_dir, create=args.command != "status")
    except DataDirError as exc:
        print(f"etlwarden: {exc}", file=sys.stderr)
        return 2

    if args.command == "engine":
        from .engine import main as engine_main

        return engine_main(data)
    if args.command == "status":
        return print_status(data)
    from .app import main as app_main  # PySide6 is only needed for the window

    return app_main(data)


def print_status(data: DataDir) -> int:
    if not data.db.exists():  # don't quietly start an empty job list in a mistyped folder
        print(f"etlwarden: there's no job list in {data.root}", file=sys.stderr)
        return 2
    db = Database(data.db)
    now = time.time()
    state = db.engine_state()
    if db.engine_alive(now):
        ago = textfmt.duration(now - state["heartbeat"])
        print(f"Engine: running (process {state['pid']}, last check {ago} ago)")
    else:
        print("Engine: not running")
    print(f"Data folder: {data.root}")
    print()

    jobs = db.list_jobs()
    if not jobs:
        print("No jobs yet.")
        return 0
    latest = db.latest_runs()
    rows = [("ID", "On", "Name", "Runs", "Next run", "Last result")]
    for job in jobs:
        run = latest.get(job.id)
        rows.append((
            str(job.id),
            "yes" if job.enabled else "no",
            job.name,
            sched.summary(job.schedule),
            textfmt.when(job.next_run, now) if job.enabled else textfmt.NOTHING,
            f"{run.status}, {textfmt.when(run.finished_at or run.queued_at, now)}" if run else textfmt.NOTHING,
        ))
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return 0
