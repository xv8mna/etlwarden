"""Build the standalone Windows version: dist/etlwarden-<version>-windows-x64.zip.

    .venv\\Scripts\\python -m pip install -e ".[build]"
    .venv\\Scripts\\python packaging\\build.py

The zip holds one folder with etlwarden.exe, etlwarden-console.exe, the
libraries they need (in _internal), and the licenses of everything included.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PyInstaller  # noqa: E402
import PySide6  # noqa: E402
from PySide6.QtCore import qVersion  # noqa: E402

from etlwarden import __version__  # noqa: E402

DIST = ROOT / "dist"


def main() -> int:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build"),
         str(ROOT / "packaging" / "etlwarden.spec")],
        check=True,
    )
    folder = DIST / f"etlwarden-{__version__}"
    if folder.exists():
        shutil.rmtree(folder)
    (DIST / "etlwarden").rename(folder)

    shutil.copy(ROOT / "LICENSE", folder / "LICENSE.txt")
    shutil.copy(ROOT / "README.md", folder / "README.md")
    licenses = folder / "licenses"
    licenses.mkdir()
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        shutil.copy(ROOT / "packaging" / "licenses" / name, licenses / name)
    shutil.copy(Path(sys.base_prefix) / "LICENSE.txt", licenses / "PYTHON-LICENSE.txt")
    (folder / "THIRD-PARTY-NOTICES.txt").write_text(notices(), encoding="utf-8")

    archive = DIST / f"etlwarden-{__version__}-windows-x64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for path in sorted(folder.rglob("*")):
            zipped.write(path, path.relative_to(DIST))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"\n{archive}\n{archive.stat().st_size / 1e6:.1f} MB, SHA-256 {digest}")
    return 0


def notices() -> str:
    pyside, qt = PySide6.__version__, qVersion()
    return f"""\
etlwarden {__version__} for Windows

etlwarden itself is released under the MIT license; see LICENSE.txt.
This download also contains the following software, unchanged.

PySide6 and Shiboken6 {pyside} (Qt for Python), with the Qt {qt} libraries they include
  Licensed under the GNU Lesser General Public License version 3 (licenses/LGPL-3.0.txt),
  which adds permissions to the GNU General Public License version 3 (licenses/GPL-3.0.txt).
  Copyright (C) The Qt Company Ltd. and other contributors.
  They are separate files in the _internal folder (the PySide6 and shiboken6 folders),
  so you can replace them with your own build of the same version.
  Source code of exactly these versions:
    PySide6 and Shiboken6: https://download.qt.io/official_releases/QtForPython/pyside6/
      and https://code.qt.io/cgit/pyside/pyside-setup.git (tag v{pyside})
    Qt: https://download.qt.io/archive/qt/

Python {platform.python_version()}
  Licensed under the Python Software Foundation License (licenses/PYTHON-LICENSE.txt), which
  also lists the licenses of the libraries Python includes, such as OpenSSL, SQLite and zlib.

PyInstaller {PyInstaller.__version__} bootloader (the small program that starts etlwarden.exe)
  Licensed under the GNU General Public License version 2 with an exception that allows it
  to be distributed with programs under any license. https://pyinstaller.org
"""


if __name__ == "__main__":
    raise SystemExit(main())
