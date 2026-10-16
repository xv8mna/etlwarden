"""Short, readable text for times and durations."""

from __future__ import annotations

import locale
import sys
import time
from datetime import datetime

from .schedule import DAY_NAMES

NOTHING = "—"


def when(timestamp: float | None, now: float | None = None, *, seconds: bool = False) -> str:
    """"today 06:00", "tomorrow 06:00", "Fri 06:00" or a full date."""
    if timestamp is None:
        return NOTHING
    moment = datetime.fromtimestamp(timestamp)
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    days = (moment.date() - today).days
    clock = moment.strftime("%H:%M:%S" if seconds else "%H:%M")
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"tomorrow {clock}"
    if days == -1:
        return f"yesterday {clock}"
    if 1 < days < 7:
        return f"{DAY_NAMES[moment.weekday()]} {clock}"
    return f"{moment:%Y-%m-%d} {clock}"


def duration(seconds: float | None) -> str:
    """"12 s", "3 min 5 s", "1 h" or "1 h 2 min"."""
    if seconds is None:
        return ""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def decode_output(raw: bytes) -> str:
    """Text a program printed. Each line is read as UTF-8 when that's valid.

    Otherwise there are two candidates on Windows: batch files and console
    tools print in the console code page (OEM, 850 on German Windows), other
    programs such as KNIME and Hop in the Windows one (ANSI, 1252). The line is
    read both ways and the more plausible reading wins (see _plausibility), so
    "Größe" beats "Gr”áe", "Straße" beats "Straáe", "Verträge" beats "Vertrõge"
    and "1.234,56 €" beats "1.234,56 Ç". A tie goes to ANSI.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "".join(_decode_line(line) for line in raw.splitlines(keepends=True))


def _decode_line(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    readings = [line.decode(encoding, errors="replace") for encoding in _FALLBACK_ENCODINGS]
    return max(readings, key=_plausibility)  # max() keeps the first on a tie


_COMMON_ACCENTED = set("äöüÄÖÜéèêëàáâçñóòôúùûíìîïÉÈÊÀÁÂÇÑÓÒÔÚÙÛÍøØåÅæÆœŒ")
# Windows typography (bytes 0x80-0x9F in code page 1252); in the console code page
# the same bytes are accented letters.
_TYPOGRAPHY = set("€…–—„“”‘’•‚")
_AFTER_A_WORD = set("…“”’")  # the ones that normally follow a letter directly


def _plausibility(text: str) -> int:
    """How much a line reads like real text: ß counts 3, common accented letters 2, other
    letters 1, Windows typography 2 (only where it's normally used), box drawing 3 (the
    lines of `tree` and similar), and the replacement character � takes 2 off."""
    score = 0
    for index, char in enumerate(text):
        if char.isascii():
            continue
        if char == "�":
            score -= 2
        elif char == "ß":
            score += 3
        elif char in _COMMON_ACCENTED:
            score += 2
        elif char in _TYPOGRAPHY:
            after_letter = index > 0 and text[index - 1].isalpha()
            score += 2 if not after_letter or char in _AFTER_A_WORD else 0
        elif "─" <= char <= "╿":
            score += 3
        elif char.isalpha():
            score += 1
    return score


def _ansi_encoding() -> str:
    if sys.platform == "win32":
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetACP()}"  # also right in Python's UTF-8 mode
    return locale.getpreferredencoding(False)


_FALLBACK_ENCODINGS = (_ansi_encoding(), "oem") if sys.platform == "win32" else (_ansi_encoding(),)
