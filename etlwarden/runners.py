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


# Programs etlwarden looks for on PATH when Settings doesn't name one.
PROGRAMS = {
    "tafl": ["tafl"],
    "lua": ["lua", "lua54", "lua5.4", "luajit"],
}


def find_tool(kind: str) -> str | None:
    """Where the program for a kind of job is on PATH, e.g. tafl.exe for TAFL projects."""
    return find_program([f"{name}.exe" if sys.platform == "win32" else name for name in PROGRAMS[kind]])


def tafl_command(path: Path, settings: Settings) -> list[str]:
    """`tafl run` calculates all tables of a TAFL project and runs its exports;
    the exit code is 0 when everything worked."""
    tafl = settings.get("tafl_path") or find_tool("tafl")
    if not tafl:
        raise RunnerError("Set the path to TAFL (tafl.exe) in Settings first.")
    return [tafl, "run", str(path)]


def python_command(path: Path, settings: Settings) -> list[str]:
    python = settings.get("python_path") or default_python()
    if not python:
        raise RunnerError("Install Python, or set the path to python.exe in Settings.")
    return [python, str(path)]


def lua_command(path: Path, settings: Settings) -> list[str]:
    lua = settings.get("lua_path") or find_tool("lua")
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
        if sys.platform != "win32":
            return ["sh", str(path)]
        shell = find_program(["sh.exe", "bash.exe"])  # e.g. from Git for Windows
        if not shell:
            raise RunnerError("Shell scripts need sh.exe on PATH, for example from Git for Windows.")
        return [shell, str(path)]
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
    failed step, Lua's error message, or otherwise the last line (for Python, the exception)."""
    if kind == "tafl":
        summary = _tafl_failure(output)
        if summary:
            return summary
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if kind == "lua" and "stack traceback:" in lines:
        # Lua prints the error first and then the traceback, which ends in "[C]: in ?".
        before = lines[: lines.index("stack traceback:")]
        if before:
            return _LUA_PROGRAM.sub("", before[-1], count=1)
    return lines[-1] if lines else None


# The "lua: " or "C:\Lua\lua54.exe: " that lua puts in front of its error messages.
_LUA_PROGRAM = re.compile(r"^(?:[A-Za-z]:)?(?:[^:]*[\\/])?(?:lua|luajit)[\w.]*?(?:\.exe)?: ", re.IGNORECASE)


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
        step = shorten(step.strip(), 80)  # descriptions list every column; the error matters more
        following = lines[index + 1] if index + 1 < len(lines) else ""
        error = following.strip() if following.startswith("      ") else ""
        where = f"{table} › {step}" if table else step
        return f"{where}: {error}" if error else where
    return None


def shorten(text: str, limit: int) -> str:
    """At most `limit` characters, with … where something was cut off."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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
    installed there: the first python on PATH, including the ones the Python install
    manager and the Microsoft Store put there. Then the py launcher, then any Python
    registered with Windows. From the source code, the Python running etlwarden is the
    last resort; the standalone etlwarden.exe has none of its own, so there it can be None."""
    names = ["python.exe"] if sys.platform == "win32" else ["python3", "python"]
    for folder in _path_folders(skip_app_aliases=False):
        for name in names:
            candidate = Path(folder) / name
            if _is_app_alias_folder(folder):
                # App aliases (Python install manager, Store) are links: ask them where Python
                # really is. The Store's placeholder for "not installed" answers nothing.
                if os.path.lexists(candidate) and (real := _python_behind(str(candidate))):
                    return real
            elif candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    if sys.platform == "win32":
        launcher = find_program(["py.exe"])
        found = (launcher and _python_behind(launcher, "-3")) or _registered_python()
        if found:
            return found
    if getattr(sys, "frozen", False):
        return None
    base = Path(sys.base_prefix) / ("python.exe" if sys.platform == "win32" else "bin/python3")
    return str(base) if base.exists() else sys.executable


def find_program(names: list[str]) -> str | None:
    """The first of `names` on the jobs' PATH, as a full path, or None."""
    for folder in _path_folders(skip_app_aliases=True):
        for name in names:
            candidate = Path(folder) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _path_folders(*, skip_app_aliases: bool) -> list[str]:
    """The jobs' PATH: only full paths (Windows would also look in the current folder,
    which can be etlwarden's own) and not etlwarden's own environment."""
    own = _same_path(sys.prefix)
    folders = []
    for folder in job_environment().get("PATH", "").split(os.pathsep):
        if not os.path.isabs(folder) or (skip_app_aliases and _is_app_alias_folder(folder)):
            continue
        if _same_path(folder) == own or _same_path(folder).startswith(own + os.sep):
            continue
        folders.append(folder)
    return folders


def _is_app_alias_folder(folder: str) -> bool:
    return "windowsapps" in folder.lower()


_asked: dict[tuple[str, ...], str | None] = {}


def _python_behind(*command: str) -> str | None:
    """Run a Python (or launcher) once and ask it for its real python.exe. Remembered."""
    if command not in _asked:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        try:
            result = subprocess.run(
                [*command, "-c", "import sys; print(sys.executable)"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                env=job_environment(), **options,
            )
            path = result.stdout.strip()
            _asked[command] = path if result.returncode == 0 and path and Path(path).is_file() else None
        except (OSError, subprocess.SubprocessError):
            _asked[command] = None
    return _asked[command]


def _registered_python() -> str | None:
    """The newest Python that its installer registered with Windows (PEP 514)."""
    import winreg

    found: list[tuple[tuple[int, ...], str]] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            core = winreg.OpenKey(hive, r"Software\Python\PythonCore")
        except OSError:
            continue
        with core:
            for index in range(winreg.QueryInfoKey(core)[0]):
                version = winreg.EnumKey(core, index)
                try:
                    with winreg.OpenKey(core, rf"{version}\InstallPath") as key:
                        try:
                            python = winreg.QueryValueEx(key, "ExecutablePath")[0]
                        except OSError:
                            python = str(Path(winreg.QueryValueEx(key, "")[0]) / "python.exe")
                except OSError:
                    continue
                numbers = tuple(int(n) for n in re.findall(r"\d+", version)[:2])
                if python and Path(python).is_file():
                    found.append((numbers, python))
    return max(found)[1] if found else None


def job_environment() -> dict[str, str]:
    """The environment jobs run in: this one without etlwarden's own virtual environment,
    so a job that calls `python` gets the same one as in a new terminal."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        # The standalone etlwarden.exe sets variables that point into its own folder (Qt
        # plugins, PyInstaller's bookkeeping). Jobs must not see them, and a job that is
        # itself a PyInstaller program (a frozen tafl.exe, say) has to start fresh. The
        # folder can be spelled two ways, e.g. Z:\... and \\server\share\... for a mapped drive.
        spellings = {_same_path(bundle), _same_path(os.path.realpath(bundle))}

        def inside(value: str) -> bool:
            value = _same_path(value)
            return any(value == s or value.startswith(s + os.sep) for s in spellings)

        for name, value in list(env.items()):
            if name.upper() == "PATH":
                env[name] = os.pathsep.join(f for f in value.split(os.pathsep) if f and not inside(f))
            elif name.startswith(("_PYI", "_MEI")) or any(s in _same_path(value) for s in spellings):
                del env[name]
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if sys.prefix != sys.base_prefix:  # etlwarden runs inside a virtual environment
        for name in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"):
            env.pop(name, None)
        own_scripts = _same_path(Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin"))
        folders = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(folder for folder in folders if folder and _same_path(folder) != own_scripts)
    return env


def start(
    command: list[str],
    cwd: Path,
    output: IO[bytes],
    on_drive_mapped: Callable[[str, str], None] | None = None,
) -> ProcessTree:
    """Start a job without a window, writing everything it prints into `output`.

    A batch file in a network folder gets a drive letter for the time it runs; it's
    passed to `on_drive_mapped(letter, share)` so a crashed engine can remove it later.
    """
    options: dict = {}
    drive = None
    if sys.platform == "win32":
        forget_bundle_dll_folder()
        # Suspended until it's in its job object, so nothing it starts can slip out.
        options["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
        )
        if _is_batch(command) and _is_network_path(cwd):
            drive, share, cwd = _map_network_folder(cwd)
            if on_drive_mapped:
                on_drive_mapped(drive, share)
    else:
        options["start_new_session"] = True  # its own process group, so kill() reaches its children
    try:
        process = subprocess.Popen(
            command_line(command),
            cwd=cwd,
            env=job_environment(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            **options,
        )
        return ProcessTree(process, drive)
    except BaseException:
        _unmap(drive)
        raise


def command_line(command: list[str]) -> list[str] | str:
    """What to hand to Popen.

    Windows runs .bat and .cmd files through cmd.exe, which needs care: cmd.exe is
    named with its full path, so no other cmd.exe can stand in; paths are quoted, so
    folders like R&D work; and `call` makes cmd return the batch file's own exit code,
    so errors aren't recorded as OK. `call` doubles every ^ inside quotes, so a path with
    ^ in it runs without, and an explicit `exit /b` keeps its exit code instead.
    """
    if sys.platform != "win32" or not _is_batch(command):
        return command
    parts = " ".join(_cmd_quote(part) for part in [f'"{command[0]}"', *command[1:]])
    line = f"{parts} & call exit /b %^errorlevel%" if "^" in "".join(command) else f"call {parts}"
    return f'"{_system_program("cmd.exe")}" /d /s /c "{line}"'


def _system_program(name: str) -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name)


_dll_folder_forgotten = False


def forget_bundle_dll_folder() -> None:
    """The standalone etlwarden.exe tells Windows to look for DLLs in its own folder first,
    and every program it starts would inherit that. Jobs (and the programs they start,
    such as sqlcmd) must find their DLLs as usual, so undo it once everything is loaded."""
    global _dll_folder_forgotten
    if sys.platform == "win32" and getattr(sys, "frozen", False) and not _dll_folder_forgotten:
        _kernel32.SetDllDirectoryW(None)
        _dll_folder_forgotten = True


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
            job = None
            try:
                job = _job_object_for(process)
            finally:
                try:
                    _resume(process)  # start() created it suspended
                except BaseException:
                    if job:
                        _kernel32.CloseHandle(job)
                    raise
            self._job = job

    def kill(self) -> None:
        """Stop the job and everything it started."""
        if self._job is not None:
            _kernel32.TerminateJobObject(self._job, 1)
        elif self.process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    [_system_program("taskkill.exe"), "/T", "/F", "/PID", str(self.process.pid)],
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


def remove_leftover_drive(drive: str, share: str) -> bool:
    """Remove a drive letter that a stopped engine mapped, if it still points to its share."""
    if sys.platform != "win32":
        return False
    buffer = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buffer))
    if _mpr.WNetGetConnectionW(drive, buffer, ctypes.byref(size)) != 0:
        return False
    if _same_path(buffer.value) != _same_path(share):
        return False  # someone mapped the letter to something else meanwhile
    return _mpr.WNetCancelConnection2W(drive, 0, True) == 0


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
    _mpr.WNetGetConnectionW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _mpr.WNetGetConnectionW.restype = wintypes.DWORD
    _kernel32.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    _kernel32.SetDllDirectoryW.restype = wintypes.BOOL
    _map_lock = threading.Lock()
    # WNetAddConnection2W errors that only mean "this letter is taken, try the next one".
    _LETTER_TAKEN = {85, 1202}  # ERROR_ALREADY_ASSIGNED, ERROR_DEVICE_ALREADY_REMEMBERED

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

    def _map_network_folder(folder: Path) -> tuple[str, str, Path]:
        """Map the share of a \\\\server\\share\\... folder to a free drive letter, because
        cmd.exe can't start in a network folder. Returns the letter, the share and the folder
        on that letter. ProcessTree.release() removes it again, also after the job was stopped."""
        share, rest = os.path.splitdrive(str(folder))
        with _map_lock:
            used = _kernel32.GetLogicalDrives()
            for letter in "ZYXWVUTSRQPONMLKJIHGFED":
                if used & (1 << (ord(letter) - ord("A"))):
                    continue
                resource = _NetResource(dwType=_RESOURCETYPE_DISK, lpLocalName=f"{letter}:", lpRemoteName=share)
                error = _mpr.WNetAddConnection2W(ctypes.byref(resource), None, None, 0)
                if error == 0:
                    return f"{letter}:", share, Path(f"{letter}:{rest or os.sep}")
                if error not in _LETTER_TAKEN:
                    raise OSError(f"Couldn't connect to {share} (Windows error {error})")
        raise OSError(f"No free drive letter to run a batch file in {share}")
