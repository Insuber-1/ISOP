# -*- mode: python ; coding: utf-8 -*-
"""One-file PyInstaller build for Intelligent Server Operations Platform."""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import os


def collect_app_data_without_node_modules(root):
    """Bundle app sources while leaving npm dependencies to the Docker image."""
    collected = []
    for current_dir, subdirs, filenames in os.walk(root):
        subdirs[:] = [name for name in subdirs if name != "node_modules"]
        for filename in filenames:
            source = os.path.join(current_dir, filename)
            relative_dir = os.path.relpath(current_dir, root)
            destination = "app" if relative_dir == "." else os.path.join("app", relative_dir)
            collected.append((source, destination))
    return collected

# On Python versions with regular Tcl/Tk folders, collect them into the explicit
# locations used by runtime_tk.py. Python 3.14+ may expose the libraries through
# zipfs instead; in that case PyInstaller's built-in _tkinter hook collects them.
try:
    import tkinter
    _tcl_library = tkinter.Tcl().eval("info library")
except Exception as exc:
    raise SystemExit(
        "ERROR: Python's Tcl/Tk installation could not be opened. "
        "Install Python with Tcl/Tk support and rebuild. "
        f"Details: {exc}"
    )

tcl_tk_datas = []
if _tcl_library.replace("\\", "/").startswith("//zipfs:/"):
    # PyInstaller's hook-_tkinter supports Tcl/Tk libraries stored in zipfs.
    pass
else:
    TCL_DIR = os.path.abspath(_tcl_library)
    if not os.path.isdir(TCL_DIR):
        raise SystemExit(f"ERROR: Tcl library directory not found: {TCL_DIR}")

    _TCL_ROOT = os.path.dirname(TCL_DIR)
    _TCL_VERSION = os.path.basename(TCL_DIR)
    _TK_VERSION = _TCL_VERSION[3:] if _TCL_VERSION.startswith("tcl") else "8.6"
    TK_DIR = os.path.join(_TCL_ROOT, "tk" + _TK_VERSION)
    if not os.path.isdir(TK_DIR):
        raise SystemExit(f"ERROR: Tk library directory not found: {TK_DIR}")

    tcl_tk_datas = [
        (TCL_DIR, "_tcl_data"),
        (TK_DIR, "_tk_data"),
    ]

# Everything needed by the application is embedded in the executable.
datas = [
    ("templates", "templates"),
    ("logo.png", "."),
    ("arch_dark_theme.json", "."),
    ("theme_gray.json", "."),
    ("docker-compose.yml", "."),
] + collect_app_data_without_node_modules("app") + tcl_tk_datas + collect_data_files("customtkinter")

a = Analysis(
    ["main.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=datas,
    hiddenimports=(
        collect_submodules("flask")
        + collect_submodules("werkzeug")
        + collect_submodules("customtkinter")
        + ["_tkinter"]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["runtime_tk.py"],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Intelligent-Server-Operations-Platform",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
