"""Only one engine per data folder: two engines would start every job twice."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO


class AlreadyRunning(RuntimeError):
    """Another engine holds the lock for this data folder."""


class SingleInstance:
    """An exclusive lock on a file. The operating system releases it if the process dies."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(f"Another engine is already running for {self.path.parent}") from None
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        if sys.platform == "win32":
            import msvcrt

            self._file.seek(0)
            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self._file.close()
        self._file = None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
