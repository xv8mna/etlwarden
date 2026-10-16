import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from etlwarden import app as app_module  # noqa: E402
from etlwarden import schedule as sched  # noqa: E402
from etlwarden.app import JobDialog, MainWindow  # noqa: E402
from etlwarden.db import Database  # noqa: E402
from etlwarden.lock import SingleInstance  # noqa: E402

# One Qt application for the whole test run: if it went away while windows still
# existed, Qt would crash as Python exits, and the test run would count as failed.
_QT = QApplication.instance() or QApplication([])


@pytest.fixture
def qapp():
    yield _QT
    for widget in QApplication.topLevelWidgets():  # what the test left open
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_the_window_shows_jobs_and_their_history(qapp, data, script):
    db = Database(data.db)
    job_id = db.add_job("Sales export", script("sales.py"), "python", sched.daily("06:00", sched.WEEKDAYS))
    run_id = db.add_run(job_id, "manual", "queued")
    db.mark_running(run_id, "run.log", time.time() - 5)
    db.finish_run(run_id, "ok", exit_code=0)

    window = MainWindow(data)

    assert window.jobs_table.rowCount() == 1
    assert window.jobs_table.item(0, 1).text() == "Sales export"
    assert window.jobs_table.item(0, 2).text() == "Mon–Fri at 06:00"
    assert window.jobs_table.item(0, 4).text().startswith("OK · today")
    assert window.runs_table.rowCount() == 1
    assert window.runs_table.item(0, 2).text() == "5 s"
    assert "not running" in window.engine_label.text()
    window.close()


def test_unticking_a_job_pauses_it(qapp, data, script):
    db = Database(data.db)
    job_id = db.add_job("Sales export", script("sales.py"), "python", sched.every(15))
    window = MainWindow(data)

    window.jobs_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)

    job = db.get_job(job_id)
    assert not job.enabled and job.next_run is None
    window.close()


def test_the_job_dialog_shows_a_job_as_it_was_saved(qapp, data, script):
    path = script("report.py")
    db = Database(data.db)
    job_id = db.add_job("Report", path, "python", sched.every(30), timeout_minutes=5, catch_up=False)

    dialog = JobDialog(job=db.get_job(job_id))

    assert dialog.values() == {
        "name": "Report",
        "path": str(path),
        "kind": "python",
        "schedule": {"type": "interval", "minutes": 30},
        "enabled": True,
        "timeout_minutes": 5,
        "catch_up": False,
    }


def test_choosing_a_file_fills_in_type_and_name(qapp, tmp_path):
    workflow = tmp_path / "Monthly sales.knwf"
    workflow.write_text("")
    dialog = JobDialog()

    dialog.path_edit.setText(str(workflow))

    assert dialog.kind_combo.currentData() == "knime"
    assert dialog.name_edit.text() == "Monthly sales"
    assert dialog.values()["schedule"] == {"type": "daily", "time": "06:00", "days": sched.WEEKDAYS}


def test_the_job_dialog_refuses_a_missing_file(qapp, tmp_path):
    dialog = JobDialog()
    dialog.name_edit.setText("Ghost")
    dialog.path_edit.setText(str(tmp_path / "missing.py"))

    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    assert not dialog.result()
    assert dialog.error_label.text() == "That file doesn't exist."


@pytest.fixture
def dialogs(monkeypatch):
    """Answers the window's questions with Yes and collects its messages, instead of waiting for a click."""
    shown = []
    monkeypatch.setattr(app_module.QMessageBox, "information", lambda *args: shown.append(args[2]))
    monkeypatch.setattr(app_module.QMessageBox, "question", lambda *args: app_module.QMessageBox.StandardButton.Yes)
    return shown


def test_a_running_job_cannot_be_deleted(qapp, data, script, dialogs):
    db = Database(data.db)
    job_id = db.add_job("Busy", script("busy.py"), "python", sched.every(15))
    db.add_run(job_id, "manual", "queued")
    window = MainWindow(data)
    window.jobs_table.selectRow(0)

    with SingleInstance(data.engine_lock):  # an engine is running
        window.delete_job()

    assert db.get_job(job_id) is not None
    assert "running or waiting" in dialogs[0]
    window.close()


def test_a_job_the_engine_starts_while_asking_is_not_deleted(qapp, data, script, monkeypatch, dialogs):
    db = Database(data.db)
    job_id = db.add_job("Busy", script("busy.py"), "python", sched.every(15))
    window = MainWindow(data)
    window.jobs_table.selectRow(0)

    def engine_starts_it(*args):
        db.add_run(job_id, "schedule", "queued")
        return app_module.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(app_module.QMessageBox, "question", engine_starts_it)
    with SingleInstance(data.engine_lock):
        window.delete_job()

    assert db.get_job(job_id) is not None
    assert "running or waiting" in dialogs[0]
    window.close()


def test_an_idle_job_is_deleted_while_the_engine_runs(qapp, data, script, dialogs):
    db = Database(data.db)
    job_id = db.add_job("Idle", script("idle.py"), "python", sched.every(15))
    window = MainWindow(data)
    window.jobs_table.selectRow(0)

    with SingleInstance(data.engine_lock):
        window.delete_job()

    assert db.get_job(job_id) is None
    assert window.jobs_table.rowCount() == 0
    window.close()


def test_the_job_list_says_running_even_after_a_skipped_run(qapp, data, script):
    db = Database(data.db)
    job_id = db.add_job("Slow", script("slow.py"), "python", sched.every(1))
    run_id = db.add_run(job_id, "schedule", "queued")
    db.mark_running(run_id, "run.log")
    db.add_run(job_id, "schedule", "skipped", message="The previous run hadn't finished yet.")

    window = MainWindow(data)

    assert window.jobs_table.item(0, 4).text() == "Running…"
    window.close()


def test_show_all_jobs_brings_back_the_full_history(qapp, data, script):
    db = Database(data.db)
    db.add_job("Alpha", script("alpha.py"), "python", sched.every(15))
    window = MainWindow(data)
    window.jobs_table.selectRow(0)
    assert window.history_label.text() == "History of Alpha"

    window.show_all_button.click()

    assert window.selected_job_id() is None
    assert window.history_label.text() == "History of all jobs"
    window.close()


def test_a_failed_engine_start_is_reported(qapp, data, monkeypatch):
    class StoppedRightAway:
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(app_module.engine, "start_in_background", lambda data: StoppedRightAway())
    window = MainWindow(data)

    window.toggle_engine()

    assert "stopped right away (exit code 1)" in window.statusBar().currentMessage()
    assert window.engine_button.isEnabled()
    window.close()


def test_a_relative_or_quoted_path_is_saved_as_a_full_path(qapp, tmp_path, monkeypatch):
    job = tmp_path / "jobs" / "report.py"
    job.parent.mkdir()
    job.write_text("")
    monkeypatch.chdir(tmp_path)
    dialog = JobDialog()

    dialog.path_edit.setText(f'"{Path("jobs") / "report.py"}"')

    assert dialog.values()["path"] == str(job)
    assert dialog.name_edit.text() == "report"


def test_a_folder_that_is_not_a_knime_workflow_is_refused(qapp, tmp_path):
    folder = tmp_path / "Sales export v2.1"
    folder.mkdir()
    dialog = JobDialog()

    dialog.path_edit.setText(str(folder))
    dialog._check_on_disk()
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    assert dialog.name_edit.text() == "Sales export v2.1"
    assert "isn't a KNIME workflow" in dialog.hint_label.text()
    assert not dialog.result()
    assert dialog.error_label.text().startswith("A folder only works as a KNIME workflow")


def test_intervals_are_picked_from_a_list(qapp):
    dialog = JobDialog()
    dialog.interval_radio.setChecked(True)

    dialog.interval_combo.setCurrentIndex(dialog.interval_combo.findData(360))

    assert dialog.interval_combo.currentText() == "6 hours"
    assert dialog.schedule() == {"type": "interval", "minutes": 360}


def test_a_job_left_waiting_by_a_stopped_engine_can_be_deleted(qapp, data, script, dialogs):
    db = Database(data.db)
    job_id = db.add_job("Unwanted", script("unwanted.py"), "python", sched.every(15))
    db.add_run(job_id, "manual", "queued")
    db.engine_beat(4242, time.time(), time.time())  # a heartbeat, but the engine is gone
    window = MainWindow(data)
    window.jobs_table.selectRow(0)

    window.delete_job()

    assert db.get_job(job_id) is None
    assert dialogs == []
    window.close()


@pytest.fixture
def settings_dialog(qapp, data, monkeypatch):
    # Don't look through this computer's PATH and registry in the background.
    monkeypatch.setattr(app_module.SettingsDialog, "_look_for_programs", lambda self: None)
    return app_module.SettingsDialog(None, Database(data.db).settings(), data)


def test_settings_drop_quotes_around_program_paths(settings_dialog):
    settings_dialog.python_edit.setText(r' "C:\Python312\python.exe" ')

    assert settings_dialog.values()["python_path"] == r"C:\Python312\python.exe"


def test_settings_refuse_a_program_that_does_not_exist(settings_dialog, tmp_path):
    settings_dialog.lua_edit.setText(str(tmp_path / "lua.exe"))

    settings_dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    assert not settings_dialog.result()
    assert settings_dialog.error_label.text() == f"Lua: there's no program at {tmp_path / 'lua.exe'}."


def test_settings_show_the_programs_used_by_default(settings_dialog):
    settings_dialog.show_found({"tafl": r"C:\Tools\tafl.exe", "python": r"C:\Python312\python.exe", "lua": None})

    assert settings_dialog.tafl_edit.placeholderText() == r"Default: C:\Tools\tafl.exe"
    assert settings_dialog.python_edit.placeholderText() == r"Default: C:\Python312\python.exe"
    assert settings_dialog.lua_edit.placeholderText() == "lua.exe, needed for Lua jobs"


def test_a_file_of_unknown_type_asks_for_the_type(qapp, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("")
    dialog = JobDialog()
    dialog.name_edit.setText("Notes")

    dialog.path_edit.setText(str(notes))
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    assert dialog.kind_combo.currentData() is None
    assert not dialog.result()
    assert dialog.error_label.text() == "Choose the type of this job."
