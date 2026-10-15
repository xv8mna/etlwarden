# PyInstaller recipe for the standalone Windows version. Build with packaging/build.py.
#
# It makes two programs that share one folder:
#   etlwarden.exe          the window and the background engine, without a console
#   etlwarden-console.exe  the same for the command line, e.g. "etlwarden-console status"

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from etlwarden import __version__  # noqa: E402

ICON = str(ROOT / "packaging" / "etlwarden.ico")


def version_file(program: str) -> str:
    """Name, version and copyright shown in the .exe's Properties > Details."""
    numbers = tuple(int(part) for part in __version__.split(".")) + (0,)
    path = Path(workpath) / f"{program}-version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'etlwarden contributors'),
      StringStruct('FileDescription', 'etlwarden: a scheduler for ETL jobs'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{program}'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 the etlwarden contributors. MIT license.'),
      StringStruct('OriginalFilename', '{program}.exe'),
      StringStruct('ProductName', 'etlwarden'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return str(path)


a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "etlwarden" / "icon.png"), "etlwarden")],
    excludes=["tkinter", "unittest", "pydoc", "pytest", "_pytest", "PyInstaller"],
)
# Qt's own translations aren't used: the window is in English and the file dialogs are Windows'.
a.datas = [entry for entry in a.datas if "translations" not in Path(entry[0]).parts]
pyz = PYZ(a.pure)

window = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="etlwarden",
    console=False, icon=ICON, version=version_file("etlwarden"),
)
console = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="etlwarden-console",
    console=True, icon=ICON, version=version_file("etlwarden-console"),
)
COLLECT(window, console, a.binaries, a.datas, name="etlwarden")
