import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from etlwarden import runners


@pytest.mark.parametrize(
    "name, kind",
    [
        ("Monthly sales.tafl", "tafl"),
        ("transform.lua", "lua"),
        ("clean.py", "python"),
        ("Sales.KNWF", "knime"),
        ("load.hwf", "hop"),
        ("transform.hpl", "hop"),
        ("export.bat", "program"),
        ("notes.txt", None),
    ],
)
def test_detect_kind_from_the_file_name(tmp_path, name, kind):
    path = tmp_path / name
    path.write_text("")
    assert runners.detect_kind(path) == kind


def test_a_knime_workflow_folder_is_recognized(tmp_path):
    workflow = tmp_path / "Monthly report"
    workflow.mkdir()
    (workflow / "workflow.knime").write_text("")
    assert runners.detect_kind(workflow) == "knime"
    assert runners.detect_kind(tmp_path) is None


def test_knime_command(tmp_path):
    workflow = tmp_path / "flow.knwf"
    workflow.write_text("")
    command = runners.build_command("knime", workflow, {"knime_path": "C:/KNIME/knime.exe"})
    assert command[0] == "C:/KNIME/knime.exe"
    assert "org.knime.product.KNIME_BATCH_APPLICATION" in command
    assert command[-1] == f"-workflowFile={workflow}"


def test_tafl_runs_the_whole_project(tmp_path):
    project = tmp_path / "sales.tafl"
    project.write_text("{}")
    command = runners.build_command("tafl", project, {"tafl_path": r"C:\Tools\tafl\tafl.exe"})
    assert command == [r"C:\Tools\tafl\tafl.exe", "run", str(project)]


def test_tafl_and_lua_need_their_program(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # neither is installed there
    for kind, name in [("tafl", "sales.tafl"), ("lua", "job.lua")]:
        job = tmp_path / name
        job.write_text("")
        with pytest.raises(runners.RunnerError, match="Settings"):
            runners.build_command(kind, job, {})


def test_lua_is_found_on_path(tmp_path, monkeypatch):
    lua = tmp_path / ("lua54.exe" if sys.platform == "win32" else "lua54")
    lua.write_bytes(b"")
    lua.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    job = tmp_path / "job.lua"
    job.write_text("print('hi')")
    assert runners.build_command("lua", job, {}) == [str(lua), str(job)]


TAFL_FAILED = """\
Customers
  ✓ 1. Import customers.xlsx (Kunden)   6 rows × 3 columns   0.02s
Orders
  ✗ 1. Import orders.csv
          File not found: C:\\data\\orders.csv
  · 2. Rename Bestellnr → OrderNo
Revenue by customer
  ✗ 1. Start from Orders
          Table 'Orders' has an error: File not found: C:\\data\\orders.csv
Failed in 0.07s
"""


def test_a_tafl_failure_names_the_table_step_and_error():
    assert runners.failure_summary("tafl", TAFL_FAILED) == (
        r"Orders › 1. Import orders.csv: File not found: C:\data\orders.csv"
    )
    plain = TAFL_FAILED.replace("✗", "ERR").replace("✓", "OK ")
    assert runners.failure_summary("tafl", plain).startswith("Orders › 1. Import orders.csv")


def test_other_failures_show_the_last_line():
    traceback = "Traceback (most recent call last):\n  File 'x.py'\nKeyError: 'Kunde'\n"
    assert runners.failure_summary("python", traceback) == "KeyError: 'Kunde'"
    assert runners.failure_summary("tafl", "Error: Can't read sales.tafl\n") == "Error: Can't read sales.tafl"
    assert runners.failure_summary("program", "") is None


def test_knime_needs_its_path(tmp_path):
    workflow = tmp_path / "flow.knwf"
    workflow.write_text("")
    with pytest.raises(runners.RunnerError, match="KNIME"):
        runners.build_command("knime", workflow, {})


def test_missing_file(tmp_path):
    with pytest.raises(runners.RunnerError, match="not found"):
        runners.build_command("python", tmp_path / "gone.py", {})


def test_python_uses_the_configured_interpreter(tmp_path):
    job = tmp_path / "job.py"
    job.write_text("")
    assert runners.build_command("python", job, {"python_path": "C:/Python312/python.exe"}) == [
        "C:/Python312/python.exe",
        str(job),
    ]
    assert runners.build_command("python", job, {})[0] == runners.default_python()


def test_kind_from_name_never_touches_the_disk():
    assert runners.kind_from_name(r"\\unreachable-server\share\flow.knwf") == "knime"
    assert runners.kind_from_name("notes.txt") is None


def test_other_programs_start_directly():
    assert runners.command_line(["python", "job.py"], Path("jobs")) == (["python", "job.py"], Path("jobs"))


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe quoting only matters on Windows")
def test_batch_files_run_through_cmd_with_call_and_quoted_paths():
    line, cwd = runners.command_line([r"C:\My Tools\hop-run.bat", "--file", r"D:\R&D\c.hwf"], Path(r"C:\My Tools"))
    assert line == r'cmd.exe /d /s /c "call "C:\My Tools\hop-run.bat" --file "D:\R&D\c.hwf""'
    assert cwd == Path(r"C:\My Tools")


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe quoting only matters on Windows")
def test_batch_files_on_a_network_share_switch_there_first():
    line, cwd = runners.command_line([r"\\server\etl\jobs\load.bat"], Path(r"\\server\etl\jobs"))
    assert line == r'cmd.exe /d /s /c "pushd "\\server\etl\jobs" && call "\\server\etl\jobs\load.bat""'
    assert cwd is None


def test_jobs_do_not_inherit_etlwarden_own_virtual_environment(monkeypatch, tmp_path):
    venv, elsewhere = tmp_path / "venv", tmp_path / "other"
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(elsewhere))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(scripts), str(elsewhere)]))

    env = runners.job_environment()

    assert "VIRTUAL_ENV" not in env
    assert env["PATH"].split(os.pathsep) == [str(elsewhere)]
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_the_default_python_is_outside_etlwarden_own_environment():
    python = Path(runners.default_python())
    assert python.exists()
    if sys.prefix != sys.base_prefix:
        assert Path(sys.prefix) not in python.parents


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe quoting only matters on Windows")
def test_a_batch_path_with_a_caret_goes_without_call():
    line, _cwd = runners.command_line([r"C:\Q1 ^ Q2\load.bat"], None)
    assert line == r'cmd.exe /d /s /c ""C:\Q1 ^ Q2\load.bat""'


@pytest.mark.skipif(sys.platform != "win32", reason="Windows looks in the current folder first")
def test_the_default_python_ignores_a_python_exe_in_the_current_folder(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    assert Path(runners.default_python()).parent != tmp_path
    assert Path(runners.default_python()).is_absolute()


def _alive(pid: int) -> bool:
    found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in found


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")
def test_stopping_a_job_also_stops_what_it_started_right_away(tmp_path):
    pid_file = tmp_path / "child.pid"
    job = tmp_path / "starts_a_child.py"
    job.write_text(
        "import subprocess, time\n"
        "child = subprocess.Popen(['ping', '-n', '60', '127.0.0.1'], stdout=subprocess.DEVNULL)\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    with open(tmp_path / "out.log", "wb") as output:
        tree = runners.start([runners.default_python(), str(job)], tmp_path, output)
        try:
            deadline = time.monotonic() + 30
            while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
                time.sleep(0.1)
            child = int(pid_file.read_text())
            tree.kill()
            tree.process.wait(10)
            deadline = time.monotonic() + 10
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not _alive(child)
        finally:
            tree.kill()
            tree.release()


def _network_path(path: Path) -> Path:
    return Path(r"\\localhost" + "\\" + path.drive[0] + "$" + str(path)[2:])


@pytest.mark.skipif(sys.platform != "win32", reason="network folders are about Windows shares here")
def test_a_batch_job_in_a_network_folder_runs_there_and_leaves_no_drive_letter(tmp_path):
    folder = _network_path(tmp_path)
    if not folder.exists():
        pytest.skip(r"the administrative share \\localhost\C$ isn't available")
    (tmp_path / "input.txt").write_text("hello from the share\n")
    (tmp_path / "read.bat").write_text("@echo off\r\ntype input.txt\r\nping -n 30 127.0.0.1 >nul\r\n", encoding="ascii")
    import ctypes

    drives_before = ctypes.windll.kernel32.GetLogicalDrives()
    log = tmp_path / "out.log"
    with open(log, "wb") as output:
        tree = runners.start([str(folder / "read.bat")], folder, output)
        try:
            deadline = time.monotonic() + 20
            while "hello" not in log.read_text(errors="replace") and time.monotonic() < deadline:
                time.sleep(0.2)
            tree.kill()  # stopped, like a time limit would
            tree.process.wait(10)
        finally:
            tree.release()
    assert "hello from the share" in log.read_text(errors="replace")
    assert ctypes.windll.kernel32.GetLogicalDrives() == drives_before
