"""Runtime hook: point Tkinter at Tcl/Tk bundled inside the one-file EXE."""
import os
import sys

if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    tcl_dir = os.path.join(base, "_tcl_data")
    tk_dir = os.path.join(base, "_tk_data")
    if os.path.isdir(tcl_dir):
        os.environ["TCL_LIBRARY"] = tcl_dir
    if os.path.isdir(tk_dir):
        os.environ["TK_LIBRARY"] = tk_dir
