"""Where etlwarden keeps its job list, run history and logs."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "etlwarden"


@dataclass(frozen=True)
class DataDir:
    """The data folder. The window and the engine must use the same one."""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "etlwarden.db"

    @property
    def run_logs(self) -> Path:
        return self.root / "logs"

    @property
    def engine_log(self) -> Path:
        return self.root / "engine.log"

    @property
    def engine_lock(self) -> Path:
        return self.root / "engine.lock"

    def job_logs(self, job_id: int) -> Path:
        return self.run_logs / f"job-{job_id}"

    def run_log(self, job_id: int, run_id: int) -> Path:
        return self.job_logs(job_id) / f"run-{run_id}.log"


def default_root() -> Path:
    """The folder used when neither --data-dir nor ETLWARDEN_HOME is set."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


class DataDirError(Exception):
    """The data folder can't be used. The message is shown to people."""


def resolve(override: str | os.PathLike[str] | None = None, *, create: bool = True) -> DataDir:
    """Pick the data folder: the argument, then ETLWARDEN_HOME, then the default.

    With create=False a missing folder is an error rather than a new, empty job list,
    so a typo doesn't quietly show "no jobs".
    """
    if override:
        root, source = Path(_clean(override)).expanduser().resolve(), "--data-dir"
    elif os.environ.get("ETLWARDEN_HOME", "").strip():
        root, source = Path(_clean(os.environ["ETLWARDEN_HOME"])).expanduser(), "ETLWARDEN_HOME"
        if not root.is_absolute():  # the window, the engine and Task Scheduler start in different folders
            raise DataDirError(f"ETLWARDEN_HOME must be a full path such as C:\\etlwarden-data, not {root}")
    else:
        root, source = default_root(), "the default"
    if not root.is_dir():
        if not create:
            raise DataDirError(f"There's no data folder at {root} (from {source}).")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DataDirError(f"Can't create the data folder {root} (from {source}): {exc}") from None
    return DataDir(root)


def _clean(value: str | os.PathLike[str]) -> str:
    """Drop spaces and the quotes people often type around paths."""
    return str(value).strip().strip('"').strip("'").strip()
