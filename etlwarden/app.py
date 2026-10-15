"""The window: add jobs, choose when they run, and see what happened.

The window never runs jobs itself. It only edits the job list and the engine
does the running, so closing the window doesn't stop anything.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTime, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFontDatabase, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import engine, runners, textfmt
from . import schedule as sched
from .db import Database, Job, Run, int_setting
from .paths import DataDir

REFRESH_MS = 2000
# How long the window shows "Starting…" or "Stopping…" before it trusts the heartbeat again.
ENGINE_PENDING_SECONDS = 15

GREEN, RED, BLUE, AMBER, GRAY = "#1a7f37", "#cf222e", "#0969da", "#9a6700", "#6e7781"
RESULT_TEXT = {
    "queued": "Waiting",
    "running": "Running",
    "ok": "OK",
    "failed": "Failed",
    "timeout": "Time limit",
    "skipped": "Skipped",
    "interrupted": "Interrupted",
}
RESULT_COLOR = {
    "queued": BLUE,
    "running": BLUE,
    "ok": GREEN,
    "failed": RED,
    "timeout": RED,
    "skipped": GRAY,
    "interrupted": AMBER,
}
STARTED_BY_TEXT = {"schedule": "Scheduled", "manual": "Run now", "catch-up": "Catch-up"}
JOB_FILES = f"Job files ({' '.join('*' + suffix for suffix in runners.SUFFIXES)});;All files (*)"


class MainWindow(QMainWindow):
    def __init__(self, data: DataDir) -> None:
        super().__init__()
        self.data = data
        self.db = Database(data.db)
        self._job_rows: list[tuple] | None = None
        self._run_rows: list[tuple] | None = None
        self._engine_alive = False
        self._engine_pending: tuple[str, float] | None = None
        self._engine_process = None

        self.setWindowTitle("etlwarden")
        self.resize(1040, 660)
        self._build_toolbar()

        self.jobs_table = _table(["On", "Name", "Runs", "Next run", "Last result", "File"])
        self.jobs_table.itemChanged.connect(self._on_item_changed)
        self.jobs_table.itemSelectionChanged.connect(self.refresh)
        self.jobs_table.itemDoubleClicked.connect(lambda _item: self.edit_job())
        # A click on empty space or Escape goes back to the history of all jobs.
        self.jobs_table.viewport().installEventFilter(self)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.jobs_table, self.jobs_table.clearSelection)

        self.history_label = QLabel()
        self.show_all_button = QPushButton("Show all jobs")
        self.show_all_button.setFlat(True)
        self.show_all_button.setStyleSheet(f"color: {BLUE}")
        self.show_all_button.clicked.connect(self.jobs_table.clearSelection)
        self.runs_table = _table(["Job", "Started", "Took", "How", "Result", "Details"])
        self.runs_table.itemDoubleClicked.connect(lambda _item: self.show_log())
        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(0, 6, 0, 0)
        history_layout.addLayout(_row(self.history_label, self.show_all_button, stretch=True))
        history_layout.addWidget(self.runs_table)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.jobs_table)
        splitter.addWidget(history)
        splitter.setSizes([330, 290])
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        self.engine_label = QLabel()
        self.engine_button = QPushButton()
        self.engine_button.clicked.connect(self.toggle_engine)
        self.statusBar().addPermanentWidget(self.engine_label)
        self.statusBar().addPermanentWidget(self.engine_button)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Jobs")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)
        self._add_action(toolbar, "Add job…", self.add_job)
        self.edit_action = self._add_action(toolbar, "Edit…", self.edit_job)
        self.run_action = self._add_action(toolbar, "Run now", self.run_now)
        self.delete_action = self._add_action(toolbar, "Delete", self.delete_job)
        toolbar.addSeparator()
        self._add_action(toolbar, "Settings…", self.open_settings)

    def _add_action(self, toolbar: QToolBar, text: str, slot: Callable[[], None]) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(lambda: slot())
        toolbar.addAction(action)
        return action

    # Showing the job list -------------------------------------------------

    def refresh(self) -> None:
        now = time.time()
        try:
            jobs = self.db.list_jobs()
            latest = self.db.latest_runs()
            alive = self.db.engine_alive(now)
            self._show_jobs(jobs, latest, now)
            selected = self.selected_job_id()  # read after the table was rebuilt
            runs = self.db.runs(selected)
        except sqlite3.Error as exc:
            self.statusBar().showMessage(f"Couldn't read the job list: {exc}", REFRESH_MS * 2)
            return
        names = {job.id: job.name for job in jobs}
        self._show_runs(runs, names, now)
        selected_name = names.get(selected)
        self.history_label.setText(f"History of {selected_name}" if selected_name else "History of all jobs")
        self.show_all_button.setVisible(selected_name is not None)
        self._show_engine_state(alive, now)
        for action in (self.edit_action, self.run_action, self.delete_action):
            action.setEnabled(selected is not None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (watched is self.jobs_table.viewport() and event.type() == QEvent.Type.MouseButtonPress
                and not self.jobs_table.indexAt(event.position().toPoint()).isValid()):
            self.jobs_table.clearSelection()
        return super().eventFilter(watched, event)

    def _show_jobs(self, jobs: list[Job], latest: dict[int, Run], now: float) -> None:
        rows = []
        for job in jobs:
            result, color = _result(latest.get(job.id), now)
            next_run = textfmt.when(job.next_run, now) if job.enabled else "Off"
            rows.append((job.id, job.enabled, job.name, sched.summary(job.schedule), next_run,
                         result, color, job.path))
        if rows == self._job_rows:
            return
        self._job_rows = rows
        selected = self.selected_job_id()
        table = self.jobs_table
        table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for row, (job_id, enabled, name, runs, next_run, result, color, path) in enumerate(rows):
                switch = QTableWidgetItem()
                switch.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                                | Qt.ItemFlag.ItemIsSelectable)
                switch.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
                switch.setData(Qt.ItemDataRole.UserRole, job_id)
                switch.setToolTip("Untick to pause this job")
                table.setItem(row, 0, switch)
                table.setItem(row, 1, _cell(name))
                table.setItem(row, 2, _cell(runs))
                table.setItem(row, 3, _cell(next_run, None if enabled else GRAY))
                table.setItem(row, 4, _cell(result, color))
                table.setItem(row, 5, _cell(path))
            _reselect(table, selected)
        finally:
            table.blockSignals(False)

    def _show_runs(self, runs: list[Run], names: dict[int, str], now: float) -> None:
        rows = []
        for run in runs:
            rows.append((
                run.id,
                names.get(run.job_id, "?"),
                textfmt.when(run.started_at or run.queued_at, now, seconds=True),
                textfmt.duration(run.duration),
                STARTED_BY_TEXT.get(run.started_by, run.started_by),
                RESULT_TEXT.get(run.status, run.status),
                RESULT_COLOR.get(run.status),
                run.message or "",
            ))
        if rows == self._run_rows:
            return
        self._run_rows = rows
        selected = self.selected_run_id()
        table = self.runs_table
        table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for row, (run_id, job_name, started, took, how, result, color, details) in enumerate(rows):
                first = _cell(job_name)
                first.setData(Qt.ItemDataRole.UserRole, run_id)
                table.setItem(row, 0, first)
                table.setItem(row, 1, _cell(started))
                table.setItem(row, 2, _cell(took))
                table.setItem(row, 3, _cell(how))
                table.setItem(row, 4, _cell(result, color))
                table.setItem(row, 5, _cell(details))
            _reselect(table, selected)
        finally:
            table.blockSignals(False)

    def _show_engine_state(self, alive: bool, now: float) -> None:
        pending = self._engine_pending
        started = self._engine_process
        if pending and pending[0] == "starting" and not alive and started and started.poll() is not None:
            self.statusBar().showMessage(
                f"The engine stopped right away (exit code {started.returncode}). "
                f"The reason is in engine.log in the data folder: {self.data.root}"
            )
            pending = self._engine_pending = None
        # Starting is done once the engine is alive, stopping once it isn't.
        if pending and (now - pending[1] > ENGINE_PENDING_SECONDS or alive == (pending[0] == "starting")):
            pending = self._engine_pending = None
        self._engine_alive = alive
        if pending:
            text = "Starting the engine…" if pending[0] == "starting" else "Stopping the engine…"
            color = GRAY
        elif alive:
            text, color = "Engine running", GREEN
        else:
            text, color = "Engine not running: scheduled jobs won't start", RED
        self.engine_label.setText(text)
        self.engine_label.setStyleSheet(f"color: {color}; padding-right: 6px")
        self.engine_button.setText("Stop engine" if alive else "Start engine")
        self.engine_button.setEnabled(pending is None)

    def selected_job_id(self) -> int | None:
        return _selected_id(self.jobs_table)

    def selected_run_id(self) -> int | None:
        return _selected_id(self.runs_table)

    def selected_job(self) -> Job | None:
        job_id = self.selected_job_id()
        return None if job_id is None else self.db.get_job(job_id)

    # Actions --------------------------------------------------------------

    def add_job(self) -> None:
        dialog = JobDialog(self)
        if dialog.exec():
            self.db.add_job(**dialog.values())
            self.refresh()

    def edit_job(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        dialog = JobDialog(self, job)
        if dialog.exec():
            self.db.update_job(job.id, **dialog.values())
            self.refresh()

    def run_now(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        self.db.request_run(job.id)
        if self._engine_alive:
            self.statusBar().showMessage(f"{job.name} starts within a few seconds.", 6000)
        else:
            self.statusBar().showMessage(f"{job.name} starts once the engine is running.", 8000)
        self.refresh()

    def delete_job(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        busy = f"“{job.name}” is running or waiting to run. Delete it once it has finished."
        # Only a running engine can be busy with the job; without one, leftover queue
        # entries just go with it.
        if self.db.engine_alive() and self.db.has_active_run(job.id):
            QMessageBox.information(self, "Delete job", busy)
            return
        answer = QMessageBox.question(
            self, "Delete job", f"Delete “{job.name}” and its history? The file itself stays where it is."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Check again in the same step as deleting: the engine may have started it meanwhile.
        if not self.db.delete_job(job.id, only_if_idle=self.db.engine_alive()):
            QMessageBox.information(self, "Delete job", busy)
            return
        shutil.rmtree(self.data.job_logs(job.id), ignore_errors=True)
        self.refresh()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.db.settings(), self.data)
        if dialog.exec():
            self.db.save_settings(dialog.values())

    def show_log(self) -> None:
        run_id = self.selected_run_id()
        run = None if run_id is None else self.db.get_run(run_id)
        if run is None:
            return
        if not run.log_path:
            self.statusBar().showMessage(run.message or "This run has no log.", 6000)
            return
        job = self.db.get_job(run.job_id)
        LogDialog(self, Path(run.log_path), f"Log of {job.name if job else 'a deleted job'}").exec()

    def toggle_engine(self) -> None:
        if self._engine_alive:
            running = self.db.count_running()
            if running:
                answer = QMessageBox.question(
                    self,
                    "Stop the engine",
                    f"{running} job{'s are' if running > 1 else ' is'} running right now. "
                    "Stopping the engine stops them too. Stop anyway?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.db.request_engine_stop()
            self._engine_pending = ("stopping", time.time())
        else:
            try:
                self._engine_process = engine.start_in_background(self.data)
            except OSError as exc:
                QMessageBox.warning(self, "Start the engine", f"Couldn't start the engine:\n{exc}")
                return
            self._engine_pending = ("starting", time.time())
        self.refresh()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        self.db.set_enabled(item.data(Qt.ItemDataRole.UserRole), enabled)
        QTimer.singleShot(0, self.refresh)  # don't rebuild the table inside its own signal


class JobDialog(QDialog):
    """Add a job, or edit one."""

    def __init__(self, parent: QWidget | None = None, job: Job | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit job" if job else "Add job")
        self.setMinimumWidth(600)
        self._auto_name = ""

        self.name_edit = QLineEdit()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("The file this job runs")
        self.path_edit.textChanged.connect(lambda _text: self._path_changed())
        self.path_edit.editingFinished.connect(self._check_on_disk)
        # Looking at the disk can be slow for network paths, so wait until typing pauses.
        self._disk_check = QTimer(self, singleShot=True, interval=400)
        self._disk_check.timeout.connect(self._check_on_disk)
        file_button = QPushButton("File…")
        file_button.clicked.connect(self._pick_file)
        folder_button = QPushButton("Folder…")
        folder_button.setToolTip("For KNIME workflows saved as a folder")
        folder_button.clicked.connect(self._pick_folder)
        self.hint_label = QLabel()
        self.hint_label.setStyleSheet(f"color: {AMBER}")
        self.hint_label.setWordWrap(True)
        self.hint_label.hide()

        self.kind_combo = QComboBox()
        for kind, (label, _command) in runners.KINDS.items():
            self.kind_combo.addItem(label, kind)

        self.daily_radio = QRadioButton("At")
        self.time_edit = QTimeEdit(QTime(6, 0))
        self.time_edit.setDisplayFormat("HH:mm")
        self.day_boxes = [QCheckBox(name) for name in sched.DAY_NAMES]
        self.interval_radio = QRadioButton("Every")
        self.interval_combo = QComboBox()
        for minutes in sched.INTERVAL_CHOICES:
            self.interval_combo.addItem(sched.describe(sched.every(minutes)).removeprefix("Every "), minutes)
        self.interval_combo.setCurrentIndex(self.interval_combo.findData(15))
        when = QGridLayout()
        when.setContentsMargins(0, 0, 0, 0)
        when.addWidget(self.daily_radio, 0, 0)
        when.addLayout(_row(self.time_edit, QLabel("on"), *self.day_boxes, stretch=True), 0, 1)
        when.addWidget(self.interval_radio, 1, 0)
        when.addLayout(_row(self.interval_combo, stretch=True), 1, 1)
        self.next_label = QLabel()
        self.next_label.setStyleSheet(f"color: {GRAY}")

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 24 * 60)
        self.timeout_spin.setSuffix(" min")
        self.timeout_spin.setSpecialValueText("No limit")
        self.timeout_spin.setToolTip("Stop the job if it runs longer than this")
        self.catch_up_box = QCheckBox("If the computer was off at the run time, run it once when it's back")
        self.enabled_box = QCheckBox("Enabled")
        self.error_label = QLabel()
        self.error_label.setStyleSheet(f"color: {RED}")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout(self)
        form.addRow("Name", self.name_edit)
        form.addRow("File", _row(self.path_edit, file_button, folder_button))
        form.addRow("", self.hint_label)
        form.addRow("Type", self.kind_combo)
        form.addRow("Runs", when)
        form.addRow("", self.next_label)
        form.addRow("Time limit", _row(self.timeout_spin, stretch=True))
        form.addRow("", self.catch_up_box)
        form.addRow("", self.enabled_box)
        form.addRow(self.error_label)
        form.addRow(buttons)

        for changed in (self.daily_radio.toggled, self.enabled_box.toggled,
                        *(box.toggled for box in self.day_boxes)):
            changed.connect(self._update_preview)
        self.time_edit.timeChanged.connect(self._update_preview)
        self.interval_combo.currentIndexChanged.connect(self._update_preview)

        self._load(job)

    def _load(self, job: Job | None) -> None:
        if job is None:
            self.daily_radio.setChecked(True)
            for day, box in enumerate(self.day_boxes):
                box.setChecked(day in sched.WEEKDAYS)
            self.timeout_spin.setValue(60)
            self.catch_up_box.setChecked(True)
            self.enabled_box.setChecked(True)
            self._update_preview()
            return
        self.path_edit.setText(job.path)
        self.name_edit.setText(job.name)
        index = self.kind_combo.findData(job.kind)
        if index < 0:  # a kind this version doesn't know; keep it as it is
            self.kind_combo.addItem(job.kind, job.kind)
            index = self.kind_combo.count() - 1
        self.kind_combo.setCurrentIndex(index)
        schedule = job.schedule
        if schedule.get("type") == "interval":
            self.interval_radio.setChecked(True)
            minutes = int(schedule["minutes"])
            if self.interval_combo.findData(minutes) < 0:  # saved before the list existed
                self.interval_combo.addItem(f"{minutes} minutes", minutes)
            self.interval_combo.setCurrentIndex(self.interval_combo.findData(minutes))
            days = sched.WEEKDAYS
        else:
            self.daily_radio.setChecked(True)
            self.time_edit.setTime(QTime(*sched.parse_time(schedule["time"])))
            days = schedule["days"]
        for day, box in enumerate(self.day_boxes):
            box.setChecked(day in days)
        self.timeout_spin.setValue(round(job.timeout_minutes))
        self.catch_up_box.setChecked(job.catch_up)
        self.enabled_box.setChecked(job.enabled)
        self._update_preview()

    def schedule(self) -> dict:
        if self.interval_radio.isChecked():
            return {"type": "interval", "minutes": self.interval_combo.currentData()}
        return {
            "type": "daily",
            "time": self.time_edit.time().toString("HH:mm"),
            "days": [day for day, box in enumerate(self.day_boxes) if box.isChecked()],
        }

    def path(self) -> Path | None:
        """The typed path as a full path. Quotes from Explorer's "Copy as path" are dropped,
        and a relative path is taken from the current folder, so the engine finds it too."""
        text = _unquoted(self.path_edit.text())
        if not text:
            return None
        return Path(os.path.abspath(os.path.expanduser(os.path.expandvars(text))))

    def values(self) -> dict:
        """The job as keyword arguments for Database.add_job and update_job."""
        return {
            "name": self.name_edit.text().strip(),
            "path": str(self.path()),
            "kind": self.kind_combo.currentData(),
            "schedule": self.schedule(),
            "enabled": self.enabled_box.isChecked(),
            "timeout_minutes": self.timeout_spin.value(),
            "catch_up": self.catch_up_box.isChecked(),
        }

    def _path_changed(self) -> None:
        """While typing, judge by the file ending only; the disk check follows in a moment."""
        self.hint_label.hide()
        path = self.path()
        if path is None:
            return
        kind = runners.kind_from_name(path)
        if kind:
            self.kind_combo.setCurrentIndex(self.kind_combo.findData(kind))
        self._suggest_name(path.stem if kind else path.name)
        self._disk_check.start()

    def _check_on_disk(self) -> None:
        self._disk_check.stop()
        path = self.path()
        if path is None:
            return
        if path.is_dir():
            if runners.is_knime_workflow(path):
                self.kind_combo.setCurrentIndex(self.kind_combo.findData("knime"))
                self._show_hint(None)
            else:
                self._show_hint("This folder isn't a KNIME workflow: there's no workflow.knime inside.")
        elif path.is_file() and runners.kind_from_name(path) is None:
            self._show_hint("etlwarden doesn't know this kind of file. Check that the type below is right.")
        else:
            self._show_hint(None)

    def _show_hint(self, text: str | None) -> None:
        self.hint_label.setText(text or "")
        self.hint_label.setVisible(bool(text))

    def _suggest_name(self, name: str) -> None:
        current = self.name_edit.text().strip()
        if not current or current == self._auto_name:
            self._auto_name = name
            self.name_edit.setText(name)

    def _pick_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose the file to run", self.path_edit.text().strip(), JOB_FILES
        )
        if path:
            self.path_edit.setText(str(Path(path)))
            self._check_on_disk()

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose the KNIME workflow folder", self.path_edit.text().strip()
        )
        if path:
            self.path_edit.setText(str(Path(path)))
            self._check_on_disk()

    def _update_preview(self) -> None:
        daily = self.daily_radio.isChecked()
        self.time_edit.setEnabled(daily)
        for box in self.day_boxes:
            box.setEnabled(daily)
        self.interval_combo.setEnabled(not daily)
        try:
            schedule = sched.validate(self.schedule())
        except sched.ScheduleError as exc:
            self.next_label.setText(str(exc))
            return
        if self.enabled_box.isChecked():
            self.next_label.setText(f"Next run: {textfmt.when(sched.next_run_after(schedule, time.time()))}")
        else:
            self.next_label.setText("Disabled: it only runs when you click Run now.")

    def _accept(self) -> None:
        problem = self._problem()
        if problem:
            self.error_label.setText(problem)
            self.error_label.show()
            return
        self.accept()

    def _problem(self) -> str | None:
        if not self.name_edit.text().strip():
            return "Give the job a name."
        path = self.path()
        if path is None:
            return "Choose the file this job runs."
        if not path.exists():
            return "That file doesn't exist."
        if path.is_dir() and not (self.kind_combo.currentData() == "knime" and runners.is_knime_workflow(path)):
            return "A folder only works as a KNIME workflow folder, with workflow.knime inside."
        try:
            sched.validate(self.schedule())
        except sched.ScheduleError as exc:
            return str(exc)
        return None


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None, settings: dict[str, str], data: DataDir) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(int_setting(settings, "max_parallel"))
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(10, 100_000)
        self.keep_spin.setValue(int_setting(settings, "keep_runs"))
        self.keep_spin.setSuffix(" runs per job")
        self.tafl_edit = _program_edit(settings["tafl_path"], "tafl", "tafl.exe, needed for TAFL jobs")
        self.python_edit = QLineEdit(settings["python_path"])
        python = runners.default_python()
        self.python_edit.setPlaceholderText(f"Default: {python}" if python else "python.exe, needed for Python jobs")
        self.lua_edit = _program_edit(settings["lua_path"], "lua", "lua.exe, needed for Lua jobs")
        self.knime_edit = QLineEdit(settings["knime_path"])
        self.knime_edit.setPlaceholderText("knime.exe, needed for KNIME jobs")
        self.hop_edit = QLineEdit(settings["hop_run_path"])
        self.hop_edit.setPlaceholderText("hop-run.bat, needed for Apache Hop jobs")

        folder = QLabel(str(data.root))
        folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        open_folder = QPushButton("Open")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data.root))))
        python_note = QLabel(
            "Python jobs run with the Python you get by typing python in a new terminal. "
            "Choose another one if a job needs packages from a particular environment."
        )
        python_note.setWordWrap(True)
        python_note.setStyleSheet(f"color: {GRAY}")
        note = QLabel("The engine picks up changes within a few seconds.")
        note.setStyleSheet(f"color: {GRAY}")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        programs = "Programs (*.exe);;All files (*)"
        form = QFormLayout(self)
        form.addRow("Jobs at the same time", self.parallel_spin)
        form.addRow("Keep history", self.keep_spin)
        form.addRow("TAFL", _row(self.tafl_edit, self._browse(self.tafl_edit, "TAFL", programs)))
        form.addRow("Python", _row(self.python_edit, self._browse(self.python_edit, "Python", programs)))
        form.addRow("", python_note)
        form.addRow("Lua", _row(self.lua_edit, self._browse(self.lua_edit, "Lua", programs)))
        form.addRow("KNIME", _row(self.knime_edit, self._browse(self.knime_edit, "KNIME", programs)))
        form.addRow("Apache Hop", _row(self.hop_edit, self._browse(
            self.hop_edit, "Apache Hop's hop-run", "hop-run (hop-run.bat hop-run.sh);;All files (*)")))
        form.addRow("Data folder", _row(folder, open_folder, stretch=True))
        form.addRow(note)
        form.addRow(buttons)

    def _browse(self, edit: QLineEdit, title: str, file_filter: str) -> QPushButton:
        button = QPushButton("Browse…")

        def browse() -> None:
            path, _filter = QFileDialog.getOpenFileName(self, f"Choose {title}", edit.text(), file_filter)
            if path:
                edit.setText(str(Path(path)))

        button.clicked.connect(browse)
        return button

    def values(self) -> dict[str, object]:
        return {
            "max_parallel": self.parallel_spin.value(),
            "keep_runs": self.keep_spin.value(),
            "tafl_path": _unquoted(self.tafl_edit.text()),
            "python_path": _unquoted(self.python_edit.text()),
            "lua_path": _unquoted(self.lua_edit.text()),
            "knime_path": _unquoted(self.knime_edit.text()),
            "hop_run_path": _unquoted(self.hop_edit.text()),
        }


class LogDialog(QDialog):
    def __init__(self, parent: QWidget | None, path: Path, title: str) -> None:
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(title)
        self.resize(860, 540)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        reload_button = QPushButton("Reload")
        reload_button.clicked.connect(self.load)
        open_button = QPushButton("Open in editor")
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        buttons = _row(reload_button, open_button, stretch=True)
        buttons.addWidget(close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addLayout(buttons)
        self.load()

    def load(self) -> None:
        try:
            text = textfmt.decode_output(self.path.read_bytes())
        except FileNotFoundError:
            text = "The log file is gone. Old logs are cleaned up after a while."
        except OSError as exc:
            text = f"Couldn't read the log: {exc}"
        self.text.setPlainText(text)
        scrollbar = self.text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    return table


def _cell(text: str, color: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
    if color:
        item.setForeground(QColor(color))
    return item


def _row(*widgets: QWidget, stretch: bool = False) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch()
    return layout


def _program_edit(value: str, program: str, needed_for: str) -> QLineEdit:
    """A path field for a program that may already be on PATH."""
    edit = QLineEdit(value)
    names = [f"{program}.exe"] if sys.platform == "win32" else [program]
    found = runners.find_program(names)
    edit.setPlaceholderText(f"Default: {found}" if found else needed_for)
    return edit


def _unquoted(text: str) -> str:
    """A pasted path without spaces around it or the quotes Explorer's "Copy as path" adds."""
    return text.strip().strip('"').strip()


def _selected_id(table: QTableWidget) -> int | None:
    """The id stored in the first cell of the selected row."""
    rows = table.selectionModel().selectedRows()
    if not rows:
        return None
    item = table.item(rows[0].row(), 0)
    return item.data(Qt.ItemDataRole.UserRole) if item else None


def _reselect(table: QTableWidget, wanted: int | None) -> None:
    table.clearSelection()
    if wanted is None:
        return
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item and item.data(Qt.ItemDataRole.UserRole) == wanted:
            table.selectRow(row)
            return


def _result(run: Run | None, now: float) -> tuple[str, str]:
    if run is None:
        return "Never ran", GRAY
    text = RESULT_TEXT.get(run.status, run.status)
    color = RESULT_COLOR.get(run.status, GRAY)
    if run.status in ("queued", "running"):
        return f"{text}…", color
    return f"{text} · {textfmt.when(run.finished_at or run.queued_at, now)}", color


ICON = Path(__file__).with_name("icon.png")


def main(data: DataDir) -> int:
    if sys.platform == "win32":
        # Its own taskbar button and icon, instead of being grouped with python.exe.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("etlwarden")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("etlwarden")
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    window = MainWindow(data)
    window.show()
    return app.exec()
