import os
from datetime import datetime
from pathlib import Path

import pytest

# Window tests render off screen, so nothing pops up while they run.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from etlwarden.paths import DataDir  # noqa: E402


@pytest.fixture
def data(tmp_path: Path) -> DataDir:
    root = tmp_path / "data"
    root.mkdir()
    return DataDir(root)


@pytest.fixture
def script(tmp_path: Path):
    """Write a small job file and return its path."""

    def write(name: str, code: str = "") -> Path:
        path = tmp_path / "jobs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return path

    return write


def ts(text: str) -> float:
    """A local clock time as epoch seconds: ts("2026-10-14T06:00")."""
    return datetime.fromisoformat(text).timestamp()
