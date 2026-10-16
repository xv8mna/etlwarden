"""Build the standalone Windows version: dist/etlwarden-<version>-windows-x64.zip.

    .venv\\Scripts\\python -m pip install -e ".[build]"
    .venv\\Scripts\\python packaging\\build.py

The zip holds one folder, etlwarden, with etlwarden.exe, etlwarden-console.exe,
the libraries they need (in _internal), the examples, and the license texts and
notices for the software included.
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
LICENSES = ROOT / "packaging" / "licenses"


def main() -> int:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build"),
         str(ROOT / "packaging" / "etlwarden.spec")],
        check=True,
    )
    folder = DIST / "etlwarden"

    shutil.copy(ROOT / "LICENSE", folder / "LICENSE.txt")
    shutil.copy(ROOT / "README.md", folder / "README.md")
    shutil.copytree(ROOT / "examples", folder / "examples")
    licenses = folder / "licenses"
    licenses.mkdir()
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "Apache-2.0.txt", "Qt-third-party.txt"):
        shutil.copy(LICENSES / name, licenses / name)
    shutil.copy(Path(sys.base_prefix) / "LICENSE.txt", licenses / "PYTHON-LICENSE.txt")
    (folder / "THIRD-PARTY-NOTICES.txt").write_text(notices(), encoding="utf-8")

    # The version is in the zip's name only: the folder inside is always "etlwarden", so
    # shortcuts and scheduled tasks keep working after unzipping a newer version over it.
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
    qt_minor = ".".join(qt.split(".")[:2])
    return f"""\
etlwarden {__version__} for Windows

etlwarden itself is released under the MIT license; see LICENSE.txt. Its source code,
including how this download is built (packaging/build.py), is at
https://github.com/xv8mna/etlwarden/tree/v{__version__}

This download also contains the following software, unchanged.

PySide6 and Shiboken6 {pyside} (Qt for Python), with the Qt {qt} libraries they include
  Licensed under the GNU Lesser General Public License version 3 (licenses/LGPL-3.0.txt),
  which adds permissions to the GNU General Public License version 3 (licenses/GPL-3.0.txt).
  Copyright (C) The Qt Company Ltd. and other contributors.
  All of it, the Python code included, consists of separate files in the PySide6 and
  shiboken6 folders inside _internal, so you can replace them with your own build of the
  same version.
  Source code of exactly these versions:
    PySide6 and Shiboken6: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{pyside}-src/
      (later moved to https://download.qt.io/archive/), and the tag v{pyside} of
      https://code.qt.io/cgit/pyside/pyside-setup.git
    Qt: https://download.qt.io/archive/qt/{qt_minor}/{qt}/
  Qt contains code from other projects under permissive licenses (PCRE2, zlib, HarfBuzz,
  FreeType, libpng, double-conversion, TinyCBOR, md4c and others). Their copyright notices
  and license texts are in licenses/Qt-third-party.txt.
  The Microsoft C++ runtime files that come with PySide6 (MSVCP140*.dll) are redistributed
  under the terms of Microsoft's Visual Studio license.

Python {platform.python_version()}
  Licensed under the Python Software Foundation License (licenses/PYTHON-LICENSE.txt), which
  also lists the licenses of the libraries Python includes, such as OpenSSL, SQLite and zlib,
  and covers the Microsoft C++ runtime files that come with Python (VCRUNTIME140*.dll).

PyInstaller {PyInstaller.__version__}
  The bootloader (the small program that starts etlwarden.exe) is licensed under the GNU
  General Public License version 2 with an exception that allows it to be distributed with
  programs under any license. The run-time hooks built into etlwarden.exe and
  etlwarden-console.exe are licensed under the Apache License 2.0 (licenses/Apache-2.0.txt).
  https://pyinstaller.org
"""


if __name__ == "__main__":
    raise SystemExit(main())
