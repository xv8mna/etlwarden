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


def test_a_running_job_cannot_be_deleted(qapp, data, script, monkeypatch):
    db = Database(data.db)
    job_id = db.add_job("Busy", script("busy.py"), "python", sched.every(15))
    db.add_run(job_id, "manual", "queued")
    db.engine_beat(4242, time.time(), time.time())  # an engine is running
    window = MainWindow(data)
    window.jobs_table.selectRow(0)
    shown = []
    monkeypatch.setattr(app_module.QMessageBox, "information", lambda *args: shown.append(args[2]))

    window.delete_job()

    assert db.get_job(job_id) is not None
    assert "running or waiting" in shown[0]
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


def test_a_job_left_waiting_by_a_stopped_engine_can_be_deleted(qapp, data, script, monkeypatch):
    db = Database(data.db)
    job_id = db.add_job("Unwanted", script("unwanted.py"), "python", sched.every(15))
    db.add_run(job_id, "manual", "queued")  # no engine is running
    window = MainWindow(data)
    window.jobs_table.selectRow(0)
    monkeypatch.setattr(app_module.QMessageBox, "question", lambda *args: app_module.QMessageBox.StandardButton.Yes)

    window.delete_job()

    assert db.get_job(job_id) is None
    window.close()


def test_settings_drop_quotes_around_program_paths(qapp, data):
    db = Database(data.db)
    dialog = app_module.SettingsDialog(None, db.settings(), data)

    dialog.python_edit.setText(r' "C:\Python312\python.exe" ')

    assert dialog.values()["python_path"] == r"C:\Python312\python.exe"
