"""Runners: how each kind of job file gets started.

Every kind has a label for the window and a function that turns the file into
the command the engine runs. To support a new kind of file, write one more
function like the ones below and add it to KINDS (and its file ending to
SUFFIXES, so the window recognizes it).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

log = logging.getLogger("etlwarden.runners")

Settings = dict[str, str]


class RunnerError(Exception):
    """A job can't be started, e.g. because a program path isn't set. Shown to people."""


def tafl_command(path: Path, settings: Settings) -> list[str]:
    """`tafl run` calculates all tables of a TAFL project and runs its exports;
    the exit code is 0 when everything worked."""
    tafl = settings.get("tafl_path") or find_program(["tafl.exe"] if sys.platform == "win32" else ["tafl"])
    if not tafl:
        raise RunnerError("Set the path to TAFL (tafl.exe) in Settings first.")
    return [tafl, "run", str(path)]


def python_command(path: Path, settings: Settings) -> list[str]:
    python = settings.get("python_path") or default_python()
    if not python:
        raise RunnerError("Install Python, or set the path to python.exe in Settings.")
    return [python, str(path)]


def lua_command(path: Path, settings: Settings) -> list[str]:
    lua = settings.get("lua_path") or find_program(
        [f"{name}.exe" if sys.platform == "win32" else name for name in ("lua", "lua54", "lua5.4", "luajit")]
    )
    if not lua:
        raise RunnerError("Set the path to Lua (lua.exe) in Settings first.")
    return [lua, str(path)]


def knime_command(path: Path, settings: Settings) -> list[str]:
    knime = settings.get("knime_path")
    if not knime:
        raise RunnerError("Set the path to KNIME (knime.exe) in Settings first.")
    workflow = f"-workflowDir={path}" if path.is_dir() else f"-workflowFile={path}"
    return [knime, "-nosplash", "-reset", "-nosave",
            "-application", "org.knime.product.KNIME_BATCH_APPLICATION", workflow]


def hop_command(path: Path, settings: Settings) -> list[str]:
    hop_run = settings.get("hop_run_path")
    if not hop_run:
        raise RunnerError("Set the path to Apache Hop's hop-run in Settings first.")
    return [hop_run, "--file", str(path), "--runconfig", "local"]


def program_command(path: Path, settings: Settings) -> list[str]:
    if path.suffix.lower() == ".sh":
        return ["sh", str(path)]
    return [str(path)]


KINDS: dict[str, tuple[str, Callable[[Path, Settings], list[str]]]] = {
    "tafl": ("TAFL project", tafl_command),
    "python": ("Python script", python_command),
    "lua": ("Lua script", lua_command),
    "knime": ("KNIME workflow", knime_command),
    "hop": ("Apache Hop workflow or pipeline", hop_command),
    "program": ("Program or batch file", program_command),
}

SUFFIXES = {
    ".tafl": "tafl",
    ".py": "python",
    ".pyw": "python",
    ".lua": "lua",
    ".knwf": "knime",
    ".hwf": "hop",
    ".hpl": "hop",
    ".exe": "program",
    ".bat": "program",
    ".cmd": "program",
    ".sh": "program",
}


def failure_summary(kind: str, output: str) -> str | None:
    """The part of a failed job's output that best says what went wrong: TAFL's first
    failed step, or otherwise the last line (for Python, the exception)."""
    if kind == "tafl":
        summary = _tafl_failure(output)
        if summary:
            return summary
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _tafl_failure(output: str) -> str | None:
    """TAFL prints each table's name, then its steps. A failed step is marked ✗ (ERR in
    plain-text consoles) and its error follows on the next, further indented line."""
    lines = output.splitlines()
    table = None
    for index, line in enumerate(lines):
        if line.strip() and not line.startswith(" "):
            table = line.strip()
            continue
        mark, _, step = line.strip().partition(" ")
        if mark not in ("✗", "ERR"):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        error = following.strip() if following.startswith("      ") else ""
        where = f"{table} › {step.strip()}" if table else step.strip()
        return f"{where}: {error}" if error else where
    return None


def detect_kind(path: str | os.PathLike[str]) -> str | None:
    """Guess the kind of a file or folder. Only KNIME workflows can be folders."""
    path = Path(path)
    if path.is_dir():
        return "knime" if is_knime_workflow(path) else None
    return kind_from_name(path)


def kind_from_name(path: str | os.PathLike[str]) -> str | None:
    """Guess the kind from the file ending alone, without touching the disk."""
    return SUFFIXES.get(Path(path).suffix.lower())


def is_knime_workflow(folder: str | os.PathLike[str]) -> bool:
    return (Path(folder) / "workflow.knime").is_file()


def build_command(kind: str, path: str | os.PathLike[str], settings: Settings) -> list[str]:
    if kind not in KINDS:
        raise RunnerError(f"Unknown job type: {kind}")
    path = Path(path)
    if not path.exists():
        raise RunnerError(f"File not found: {path}")
    return KINDS[kind][1](path, settings)


def default_python() -> str | None:
    """The Python you get by typing `python` in a new terminal, so jobs find the packages
    installed there. Falls back to the Python that etlwarden itself was installed with;
    the standalone etlwarden.exe has none to offer, so there it can be None."""
    found = find_program(["python.exe"] if sys.platform == "win32" else ["python3", "python"])
    if found or getattr(sys, "frozen", False):
        return found
    base = Path(sys.base_prefix) / ("python.exe" if sys.platform == "win32" else "bin/python3")
    return str(base) if base.exists() else sys.executable


def find_program(names: list[str]) -> str | None:
    """The first of `names` on the jobs' PATH, as a full path, or None."""
    own = _same_path(sys.prefix)
    for folder in job_environment().get("PATH", "").split(os.pathsep):
        # Only full paths (Windows would also look in the current folder, which can be
        # etlwarden's own), not etlwarden's own environment, and not the Microsoft
        # Store placeholders.
        if not os.path.isabs(folder) or "windowsapps" in folder.lower():
            continue
        if _same_path(folder) == own or _same_path(folder).startswith(own + os.sep):
            continue
        for name in names:
            candidate = Path(folder) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def job_environment() -> dict[str, str]:
    """The environment jobs run in: this one without etlwarden's own virtual environment,
    so a job that calls `python` gets the same one as in a new terminal."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        # The standalone etlwarden.exe sets variables that point into its own folder (Qt
        # plugins, PyInstaller's bookkeeping). Jobs must not see them, and a job that is
        # itself a PyInstaller program (a frozen tafl.exe, say) has to start fresh.
        inside = _same_path(bundle)
        for name, value in list(env.items()):
            if name.upper() == "PATH":
                folders = value.split(os.pathsep)
                env[name] = os.pathsep.join(f for f in folders if f and not _same_path(f).startswith(inside))
            elif name.startswith(("_PYI", "_MEI")) or inside in _same_path(value):
                del env[name]
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if sys.prefix != sys.base_prefix:  # etlwarden runs inside a virtual environment
        for name in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"):
            env.pop(name, None)
        own_scripts = _same_path(Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin"))
        folders = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(folder for folder in folders if folder and _same_path(folder) != own_scripts)
    return env


def start(command: list[str], cwd: Path, output: IO[bytes]) -> ProcessTree:
    """Start a job without a window, writing everything it prints into `output`."""
    options: dict = {}
    drive = None
    if sys.platform == "win32":
        # Suspended until it's in its job object, so nothing it starts can slip out.
        options["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
        )
        if _is_batch(command) and _is_network_path(cwd):
            drive, cwd = _map_network_folder(cwd)
    else:
        options["start_new_session"] = True  # its own process group, so kill() reaches its children
    args, cwd = command_line(command, cwd)
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=job_environment(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            **options,
        )
    except BaseException:
        _unmap(drive)
        raise
    return ProcessTree(process, drive)


def command_line(command: list[str], cwd: Path | None = None) -> tuple[list[str] | str, Path | None]:
    """What to hand to Popen, and the folder to start in.

    Windows runs .bat and .cmd files through cmd.exe, which needs care: paths
    are quoted, so folders like R&D work; `call` makes cmd return the batch
    file's own exit code, so errors aren't recorded as OK; and cmd can't start
    in a network folder (\\\\server\\share). start() maps such a folder to a
    drive letter first; if that fails, cmd switches there itself with pushd.
    """
    if sys.platform != "win32" or not _is_batch(command):
        return command, cwd
    parts = " ".join(_cmd_quote(part) for part in [f'"{command[0]}"', *command[1:]])
    # `call` doubles every ^ inside quotes, so a path with ^ in it goes without.
    line = parts if "^" in "".join(command) else f"call {parts}"
    if _is_network_path(cwd):
        line = f'pushd "{cwd}" && {line}'
        cwd = None
    return f'cmd.exe /d /s /c "{line}"', cwd


def _is_batch(command: list[str]) -> bool:
    return command[0].lower().endswith((".bat", ".cmd"))


def _is_network_path(path: Path | None) -> bool:
    return path is not None and str(path).startswith("\\\\")


def _cmd_quote(part: str) -> str:
    if part.startswith('"') or re.fullmatch(r"[\w.:=/+,@-]+", part):
        return part
    return f'"{part}"'


def display(command: list[str]) -> str:
    """The command as you'd type it, for the top of the log."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _same_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


class ProcessTree:
    """A started job together with everything it starts.

    On Windows the job runs inside a job object. Windows closes it when the
    engine ends, however that happens (even "End task" in Task Manager), and
    closing it stops every process in it. So a job never outlives the engine,
    and can't end up running twice after the engine restarts.
    """

    def __init__(self, process: subprocess.Popen, drive: str | None = None) -> None:
        self.process = process
        self._drive = drive  # a drive letter mapped for a batch job in a network folder
        self._job = None
        if sys.platform == "win32":
            try:
                self._job = _job_object_for(process)
            finally:
                _resume(process)  # start() created it suspended

    def kill(self) -> None:
        """Stop the job and everything it started."""
        if self._job is not None:
            _kernel32.TerminateJobObject(self._job, 1)
        elif self.process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def release(self) -> None:
        """The job is done: let go of it, without stopping anything it started on purpose."""
        if self._job is not None:
            _set_kill_on_close(self._job, False)
            _kernel32.CloseHandle(self._job)
            self._job = None
        _unmap(self._drive)
        self._drive = None


def _unmap(drive: str | None) -> None:
    if drive is not None:
        result = _mpr.WNetCancelConnection2W(drive, 0, True)
        if result:
            log.warning("Couldn't remove the drive letter %s (Windows error %s)", drive, result)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetLogicalDrives.restype = wintypes.DWORD
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    class _NetResource(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPWSTR),
            ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR),
            ("lpProvider", wintypes.LPWSTR),
        ]

    _mpr = ctypes.WinDLL("mpr")
    _mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(_NetResource), wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         wintypes.DWORD]
    _mpr.WNetAddConnection2W.restype = wintypes.DWORD
    _mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    _mpr.WNetCancelConnection2W.restype = wintypes.DWORD
    _map_lock = threading.Lock()

    _EXTENDED_LIMIT_INFORMATION = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _RESOURCETYPE_DISK = 1
    _CREATE_SUSPENDED = 0x4  # not in the subprocess module

    def _set_kill_on_close(job: int, on: bool) -> bool:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE if on else 0
        return bool(_kernel32.SetInformationJobObject(
            job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ))

    def _job_object_for(process: subprocess.Popen) -> int | None:
        """Put a just-started process into a new job object that ends together with this process."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            log.warning("Couldn't create a job object (Windows error %s)", ctypes.get_last_error())
            return None
        if _set_kill_on_close(job, True) and _kernel32.AssignProcessToJobObject(job, int(process._handle)):
            return job
        log.warning("Couldn't tie the job to the engine (Windows error %s)", ctypes.get_last_error())
        _kernel32.CloseHandle(job)
        return None

    def _resume(process: subprocess.Popen) -> None:
        status = _ntdll.NtResumeProcess(int(process._handle))
        if status != 0:
            process.kill()
            raise OSError(f"Couldn't resume the job's process (status {status:#x})")

    def _map_network_folder(folder: Path) -> tuple[str | None, Path]:
        """Map the share of a \\\\server\\share\\... folder to a free drive letter, because
        cmd.exe can't start in a network folder. ProcessTree.release() removes it again,
        also after the job was stopped."""
        share, rest = os.path.splitdrive(str(folder))
        with _map_lock:
            used = _kernel32.GetLogicalDrives()
            for letter in "ZYXWVUTSRQPONMLKJIHG":
                if used & (1 << (ord(letter) - ord("A"))):
                    continue
                resource = _NetResource(dwType=_RESOURCETYPE_DISK, lpLocalName=f"{letter}:", lpRemoteName=share)
                if _mpr.WNetAddConnection2W(ctypes.byref(resource), None, None, 0) == 0:
                    return f"{letter}:", Path(f"{letter}:{rest or os.sep}")
        log.warning("Couldn't map %s to a drive letter; the job switches there itself", share)
        return None, folder
