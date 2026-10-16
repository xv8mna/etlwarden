# PyInstaller recipe for the standalone Windows version. Build with packaging/build.py.
#
# It makes two programs that share one folder:
#   etlwarden.exe          the window and the background engine, without a console
#   etlwarden-console.exe  the same for the command line, e.g. "etlwarden-console status"

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from etlwarden import __version__  # noqa: E402

ICON = str(ROOT / "packaging" / "etlwarden.ico")

# Parts of Qt that etlwarden doesn't use: software OpenGL, image formats other than PNG
# (which is built in), SVG, touch input, and everything for networking.
UNUSED = (
    "opengl32sw.dll", "qt6svg.dll", "qt6network.dll",
    "plugins/imageformats/", "plugins/iconengines/", "plugins/generic/",
    "plugins/networkinformation/", "plugins/tls/",
    "plugins/platforms/qdirect2d.dll", "plugins/platforms/qminimal.dll",
)


def version_file(program: str) -> str:
    """Name, version and copyright shown in the .exe's Properties > Details."""
    # Windows wants four numbers; "0.2" or "0.2.0rc1" become (0, 2, 0, 0).
    parts = [int(n) for n in re.match(r"\d+(?:\.\d+)*", __version__).group().split(".")][:4]
    numbers = tuple(parts + [0] * (4 - len(parts)))
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
    excludes=["tkinter", "unittest", "pydoc", "pytest", "_pytest", "PyInstaller", "PySide6.QtNetwork"],
    # Qt for Python's own Python code as plain files, not inside the .exe, so it can be
    # replaced like the Qt DLLs (its license, the LGPL, asks for that).
    module_collection_mode={"PySide6": "py", "shiboken6": "py"},
)
# Qt's own translations aren't used: the window is in English and the file dialogs are Windows'.
a.datas = [entry for entry in a.datas if "translations" not in Path(entry[0]).parts]
a.binaries = [
    entry for entry in a.binaries
    if not any(part in entry[0].replace("\\", "/").lower() for part in UNUSED)
]
# Everything bundled must come from this Python and its packages, never from whatever
# else happens to be on the build machine's PATH (such as Git's own OpenSSL).
allowed = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
strays = [src for _name, src, _kind in a.binaries
          if src and not any(Path(src).resolve().is_relative_to(root) for root in allowed)]
if strays:
    raise SystemExit("These files come from outside Python and its packages:\n  " + "\n  ".join(strays))
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
