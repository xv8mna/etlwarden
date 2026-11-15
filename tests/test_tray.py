import sys
import threading
import time
from pathlib import Path

import pytest

from etlwarden import engine as engine_module
from etlwarden import schedule as sched
from etlwarden.engine import Engine, tray_tooltip
from etlwarden.paths import DataDir, default_root

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific behaviour")


class FakeTray:
    """Stands in for tray.Tray, so these tests show nothing on the screen."""

    def __init__(self, on_open, on_stop):
        self.on_open, self.on_stop = on_open, on_stop
        self.shown = []
        self.closed = False
        self.answer = True
        self.questions = []

    def start(self):
        pass

    def show(self, tooltip, failed):
        self.shown.append((tooltip, failed))

    def close(self):
        self.closed = True

    def ask(self, question):
        self.questions.append(question)
        return self.answer


def tray_engine(data):
    made = []

    def make(on_open, on_stop):
        made.append(FakeTray(on_open, on_stop))
        return made[-1]

    return Engine(data, poll_seconds=0.05, tray=make), made


def run_now(engine, job_id):
    engine.db.request_run(job_id)
    engine.tick()
    assert engine.wait_idle(30)
    engine.tick()


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_the_tooltip_says_name_and_state():
    home = DataDir(default_root())
    assert tray_tooltip(0, home) == "etlwarden - Engine running"
    assert tray_tooltip(1, home) == "etlwarden - Engine running, 1 job failed"
    assert tray_tooltip(3, home) == "etlwarden - Engine running, 3 jobs failed"


def test_the_tooltip_names_a_data_folder_that_is_not_the_usual_one():
    assert tray_tooltip(0, DataDir(Path(r"C:\etlwarden-data"))) == "etlwarden - Engine running\nC:\\etlwarden-data"


def test_a_long_folder_keeps_its_end_in_the_tooltip(data):
    tooltip = tray_tooltip(2, DataDir(data.root / ("x" * 300) / "night jobs"))

    assert len(tooltip) == 127  # what Windows can show
    assert tooltip.startswith("etlwarden - Engine running, 2 jobs failed\n…")
    assert tooltip.endswith("xxx\\night jobs")


def test_the_icon_shows_the_engine_and_its_failures(data, script):
    engine, made = tray_engine(data)
    job_id = engine.db.add_job("Load", script("load.py", "import sys\nsys.exit(1)"), "python", sched.every(60))

    engine.tick()
    assert made[0].shown[-1] == (tray_tooltip(0, data), False)

    run_now(engine, job_id)
    assert made[0].shown[-1] == (tray_tooltip(1, data), True)
    assert ", 1 job failed" in made[0].shown[-1][0]

    script("load.py", "print('fine')")
    run_now(engine, job_id)
    assert made[0].shown[-1][1] is False
    assert len(made) == 1  # one icon all the time


def test_the_setting_removes_the_icon_and_brings_it_back(data):
    engine, made = tray_engine(data)
    engine.tick()

    engine.db.save_settings({"tray_icon": 0})
    engine.tick()
    assert made[0].closed

    engine.db.save_settings({"tray_icon": 1})
    engine.tick()
    assert len(made) == 2 and not made[1].closed


def test_an_icon_that_breaks_leaves_the_engine_alone(data, script):
    tries = []

    def broken(on_open, on_stop):
        tries.append(1)
        raise OSError("no taskbar here")

    engine = Engine(data, poll_seconds=0.05, tray=broken)
    job_id = engine.db.add_job("Hello", script("hello.py", "print('hi')"), "python", sched.every(60))

    run_now(engine, job_id)
    engine.tick()

    assert engine.db.runs(job_id)[0].status == "ok"
    assert tries == [1]  # tried once, not every few seconds


def test_stop_engine_in_the_menu_stops_it(data):
    engine, made = tray_engine(data)
    engine.tick()

    made[0].on_stop()

    assert engine._stop.is_set()
    assert made[0].questions == []  # nothing was running, so nothing to ask


def test_stop_engine_asks_first_while_a_job_runs(data, script):
    engine, made = tray_engine(data)
    job_id = engine.db.add_job("Slow", script("slow.py", "import time\ntime.sleep(60)"), "python", sched.every(60))
    engine.db.request_run(job_id)
    engine.tick()
    assert wait_for(lambda: engine.db.count_running() == 1)

    made[0].answer = False
    made[0].on_stop()
    assert not engine._stop.is_set()
    assert made[0].questions == ["1 job is running right now. Stopping the engine stops them too. Stop anyway?"]

    made[0].answer = True
    made[0].on_stop()
    assert engine._stop.is_set()
    engine.shutdown()


def test_a_click_opens_the_window_for_this_data_folder(data, monkeypatch):
    started = []
    monkeypatch.setattr(engine_module, "start_window", lambda data: started.append(data))
    engine, made = tray_engine(data)
    engine.tick()

    made[0].on_open()

    assert started == [data]


def test_the_icon_goes_when_the_engine_stops(data):
    engine, made = tray_engine(data)
    running = threading.Thread(target=engine.run_forever)
    running.start()
    try:
        assert wait_for(lambda: made and made[0].shown)
        made[0].on_stop()
        running.join(30)
    finally:
        engine.stop()
    assert not running.is_alive()
    assert made[0].closed


@pytest.mark.parametrize("title, class_name, ours", [
    ("etlwarden", "Qt6120QWindowIcon", True),
    ("etlwarden", "CabinetWClass", False),  # Explorer showing a folder called etlwarden
    ("Settings", "Qt6120QWindowIcon", False),
])
def test_only_etlwarden_s_own_window_is_brought_to_the_front(title, class_name, ours):
    from etlwarden.tray import is_etlwarden_window

    assert is_etlwarden_window(title, class_name) is ours


def has_taskbar() -> bool:
    import ctypes

    return bool(ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None))


@windows_only
def test_the_real_icon_answers_clicks_and_comes_back_after_explorer_restarts(monkeypatch):
    from etlwarden import tray as tray_module

    monkeypatch.setattr(tray_module, "bring_window_to_front", lambda: False)  # don't touch a real window
    opened, stopped = [], []
    icon = tray_module.Tray(lambda: opened.append(1), lambda: stopped.append(1), hidden=True)
    icon.start()
    send = tray_module._dll("user32").SendMessageW
    try:
        icon.show("etlwarden - test", True)
        if has_taskbar():  # GitHub's Windows has no taskbar to put an icon in
            assert wait_for(lambda: icon._shown == ("etlwarden - test", True))

        send(icon.hwnd, tray_module.CLICKED, 0, tray_module.WM_LBUTTONUP)
        send(icon.hwnd, tray_module.CLICKED, 0, tray_module.WM_LBUTTONUP)  # the second half of a double-click
        assert opened == [1]

        send(icon.hwnd, tray_module.WM_COMMAND, tray_module.STOP, 0)
        assert stopped == [1]

        if has_taskbar():
            send(icon.hwnd, icon._taskbar_created, 0, 0)  # as Explorer does after a restart
            assert icon.visible
    finally:
        icon.close()
    assert not icon._thread.is_alive()
    assert not icon.visible
