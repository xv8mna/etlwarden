import sys

import pytest

from etlwarden import textfmt
from tests.conftest import ts

NOW = ts("2026-10-14T12:00")  # a Wednesday


@pytest.mark.parametrize(
    "moment, text",
    [
        ("2026-10-14T06:00", "today 06:00"),
        ("2026-10-15T06:00", "tomorrow 06:00"),
        ("2026-10-13T06:00", "yesterday 06:00"),
        ("2026-10-16T06:00", "Fri 06:00"),
        ("2026-10-25T06:00", "2026-10-25 06:00"),
    ],
)
def test_when(moment, text):
    assert textfmt.when(ts(moment), NOW) == text


def test_when_with_seconds_and_without_a_time():
    assert textfmt.when(ts("2026-10-14T06:00:07"), NOW, seconds=True) == "today 06:00:07"
    assert textfmt.when(None) == textfmt.NOTHING


@pytest.mark.parametrize(
    "seconds, text",
    [(None, ""), (4.4, "4 s"), (60, "1 min"), (185, "3 min 5 s"), (3600, "1 h"), (3720, "1 h 2 min")],
)
def test_duration(seconds, text):
    assert textfmt.duration(seconds) == text


def test_decode_output_prefers_utf8_and_never_fails():
    assert textfmt.decode_output("Grüße".encode()) == "Grüße"
    assert len(textfmt.decode_output(b"\xe4")) == 1  # not UTF-8: falls back instead of raising


@pytest.mark.skipif(sys.platform != "win32", reason="the console (OEM) code page only exists on Windows")
def test_decode_output_reads_console_output_line_by_line():
    header = "Job:     Größe\n".encode()
    console = "Größe Übersicht\r\n".encode("oem")  # what cmd.exe and console tools print
    assert textfmt.decode_output(header + console) == "Job:     Größe\nGröße Übersicht\r\n"


def _german_code_pages() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    return ctypes.windll.kernel32.GetOEMCP() == 850 and ctypes.windll.kernel32.GetACP() == 1252


@pytest.mark.skipif(not _german_code_pages(), reason="needs the German Windows code pages")
def test_decode_output_tells_windows_and_console_text_apart():
    windows_text = "für Müller\r\n".encode("cp1252")
    console_text = "Größe\r\n".encode("cp850")
    assert textfmt.decode_output(windows_text + console_text) == "für Müller\r\nGröße\r\n"


@pytest.mark.skipif(not _german_code_pages(), reason="needs the German Windows code pages")
@pytest.mark.parametrize("text", ["Verträge geladen", "März 2026", "Café", "Ölpreis"])
def test_windows_text_that_is_also_valid_console_text_stays_windows_text(text):
    assert textfmt.decode_output(text.encode("cp1252")) == text


@pytest.mark.skipif(not _german_code_pages(), reason="needs the German Windows code pages")
@pytest.mark.parametrize(
    "text, code_page",
    [
        ("Straße 12", "cp1252"),
        ("Preis: 5 €", "cp1252"),
        ("Läuft…", "cp1252"),
        ("„Bericht“ fertig", "cp1252"),
        ("Müller – Verträge", "cp1252"),
        ("Straße 12", "cp850"),
        ("┌─────┐", "cp850"),
        ("└─ Größe ─┘", "cp850"),
        ("Übersicht: 3 Dateien", "cp850"),
    ],
)
def test_german_lines_come_out_right_from_either_code_page(text, code_page):
    assert textfmt.decode_output(text.encode(code_page)) == text
