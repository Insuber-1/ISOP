"""
main.py — Intelligent Server Operations Platform
ติดตามทรัพยากรเครื่องและ Docker, ตรวจจับความผิดปกติ, จัดการบริการ,
แสดงแดชบอร์ด, บันทึกประวัติ และสำรองไฟล์ตั้งค่าที่เลือก
"""
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog
import tkinter.messagebox as msgbox
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import math
import yaml
import os
import sys
import json
import subprocess
import platform
import re
import shlex
import shutil
import csv
import hashlib
import queue
import psutil
import tempfile
from datetime import datetime, timedelta
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from PIL import Image, ImageTk

from collector import DataCollector
from brain import AIServerBrain
from remediator import Remediator
from notifier import Notifier
import server as web_server
import docker_ops
from backup_manager import BackupScheduler
from ram_ai_manager import RamAIManager, parse_mem_to_mb
from ops_kpi import calculate_kpis, read_events
from paths import APP_DIR, app_path, resource_path, atomic_write_json
# ครอบ subprocess.run()/Popen() ทุกจุดด้วย proc_utils แทนเรียกตรงๆ กันหน้าต่าง CMD ผุดขึ้นมา
# บน Windows ทุกครั้งที่เรียก docker/docker-compose/ping (ดูเหตุผลเต็มๆ ใน proc_utils.py)
import proc_utils


# Standard-library-only probe run on Linux hosts over key-based SSH. The short
# sampling interval gives CPU and network rates without installing an agent.
SSH_METRICS_SCRIPT = r'''import json, os, shutil, time
def read_cpu():
    with open('/proc/stat', 'r', encoding='ascii') as f:
        fields = list(map(int, f.readline().split()[1:9]))
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle
def read_net():
    rx = tx = 0
    with open('/proc/net/dev', 'r', encoding='ascii') as f:
        for line in f.readlines()[2:]:
            name, data = line.split(':', 1)
            if name.strip() == 'lo':
                continue
            values = list(map(int, data.split()))
            rx += values[0]
            tx += values[8]
    return rx, tx
def read_disk_io():
    try:
        physical = set(os.listdir('/sys/block'))
    except OSError:
        physical = set()
    read = write = 0
    with open('/proc/diskstats', 'r', encoding='ascii') as f:
        for line in f:
            fields = line.split()
            if len(fields) >= 10 and fields[2] in physical:
                read += int(fields[5]); write += int(fields[9])
    return read, write
c1, i1 = read_cpu(); n1 = read_net(); d1 = read_disk_io()
time.sleep(0.25)
c2, i2 = read_cpu(); n2 = read_net(); d2 = read_disk_io()
cpu = 100.0 * (c2-c1-(i2-i1)) / max(1, c2-c1)
mem = {}
with open('/proc/meminfo', 'r', encoding='ascii') as f:
    for line in f:
        key, value = line.split(':', 1)
        mem[key] = int(value.strip().split()[0])
total = mem.get('MemTotal', 0); available = mem.get('MemAvailable', mem.get('MemFree', 0))
disk = shutil.disk_usage('/')
try:
    load = os.getloadavg()[0]
except (AttributeError, OSError):
    load = 0.0
try:
    processes = sum(name.isdigit() for name in os.listdir('/proc'))
except OSError:
    processes = 0
with open('/proc/uptime', 'r', encoding='ascii') as f:
    uptime = float(f.read().split()[0])
seconds = 0.25
print(json.dumps({'cpu_pct': round(max(0, min(100, cpu)), 1),
 'ram_pct': round(100*(total-available)/max(1,total), 1),
 'ram_used_mb': round((total-available)/1024, 1), 'ram_total_mb': round(total/1024, 1),
 'disk_pct': round(100*(disk.total-disk.free)/max(1,disk.total), 1),
 'disk_used_gb': round((disk.total-disk.free)/1073741824, 2),
 'disk_total_gb': round(disk.total/1073741824, 2),
 'disk_read_mb_s': round(max(0,d2[0]-d1[0])*512/seconds/1048576, 3),
 'disk_write_mb_s': round(max(0,d2[1]-d1[1])*512/seconds/1048576, 3),
 'net_rx_mb_s': round(max(0,n2[0]-n1[0])/seconds/1048576, 3),
 'net_tx_mb_s': round(max(0,n2[1]-n1[1])/seconds/1048576, 3),
 'load_1m': round(load, 2), 'process_count': processes,
 'uptime_sec': round(uptime, 0)}))'''


def _repair_frozen_tcl_paths():
    """Use a local Python Tcl/Tk install if one-file extraction lost its scripts.

    PyInstaller normally extracts these under ``_MEIPASS/_tcl_data`` and
    ``_tk_data``. Some Windows launches have the directories but not init.tcl;
    Tk then fails before the main window is created. Prefer the bundled copy,
    and only fall back when its marker scripts are absent.
    """
    if not getattr(sys, "frozen", False):
        return

    def tcl_starts():
        try:
            # A file-existence check is insufficient: Tcl may reject an
            # incomplete or incompatible script tree. Probe the interpreter
            # without opening a GUI window before committing to the path.
            tk.Tcl().eval("info library")
            return True
        except Exception:
            return False

    tcl_dir = os.environ.get("TCL_LIBRARY", "")
    tk_dir = os.environ.get("TK_LIBRARY", "")
    if (os.path.isfile(os.path.join(tcl_dir, "init.tcl"))
            and os.path.isfile(os.path.join(tk_dir, "tk.tcl")) and tcl_starts()):
        return

    import glob

    roots = [sys.prefix, sys.base_prefix, os.path.dirname(sys.executable)]
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    if local_app_data:
        # Prefer the newest installed runtime; it is most likely to match the
        # Tcl/Tk DLL version bundled by PyInstaller.
        roots.extend(sorted(
            glob.glob(os.path.join(local_app_data, "Programs", "Python", "Python*")),
            reverse=True,
        ))
    if program_files:
        roots.extend(sorted(glob.glob(os.path.join(program_files, "Python*")), reverse=True))

    seen = set()
    for root in roots:
        for candidate_tcl in glob.glob(os.path.join(root, "tcl", "tcl*")):
            candidate_tcl = os.path.abspath(candidate_tcl)
            if candidate_tcl in seen or not os.path.isfile(os.path.join(candidate_tcl, "init.tcl")):
                continue
            seen.add(candidate_tcl)
            candidate_tk = os.path.join(os.path.dirname(candidate_tcl),
                                        os.path.basename(candidate_tcl).replace("tcl", "tk", 1))
            if os.path.isfile(os.path.join(candidate_tk, "tk.tcl")):
                os.environ["TCL_LIBRARY"] = candidate_tcl
                os.environ["TK_LIBRARY"] = candidate_tk
                if tcl_starts():
                    return


_repair_frozen_tcl_paths()

ctk.set_appearance_mode("Dark")

# เวอร์ชันโปรแกรม — แสดงในหน้า ⚙️ ตั้งค่า > เกี่ยวกับ
APP_VERSION = "0.7.3"

# โฟลเดอร์ที่ตัวโปรแกรมอยู่จริง — ใช้เป็นฐานอ้างอิงไฟล์อื่นๆ ข้างๆ กัน (ธีม, docker-compose.yml,
# ไฟล์ตั้งค่าต่างๆ) คำนวณจาก paths.py จุดเดียว เพื่อให้ตรงกับ docker_ops.py/server.py เป๊ะๆ
# แม้ตอนแพ็คเป็น .exe แล้ว (ดูรายละเอียดเหตุผลได้ในไฟล์ paths.py)

# ── docker-compose.yml สำหรับ "เว็บโปรแกรม" (เช่น word-editor + cloudflare tunnel) ──
# ตัว compose/app ถูกฝังใน EXE แต่ Docker bind-mount ต้องการ path ที่คงที่ตลอดอายุ
# container จึงคัดลอก resource ไปยัง LocalAppData/runtime ตอนเปิดโปรแกรม แทนการใช้
# _MEIPASS ซึ่งเปลี่ยนชื่อทุกครั้งที่ one-file EXE เปิดใหม่
RUNTIME_DIR = app_path("runtime")
RUNTIME_APP_DIR = os.path.join(RUNTIME_DIR, "app")
_BUNDLED_COMPOSE_FILE = resource_path("docker-compose.yml")
# Version the runtime file by content. Docker Desktop or another open app version
# may still be reading an older compose file; a content-addressed path avoids
# overwriting a file in use and lets relaunch/self-test safely reuse the same one.
with open(_BUNDLED_COMPOSE_FILE, "rb") as _compose_stream:
    _COMPOSE_DIGEST = hashlib.sha256(_compose_stream.read()).hexdigest()[:12]
COMPOSE_FILE = os.path.join(RUNTIME_DIR, f"docker-compose-{_COMPOSE_DIGEST}.yml")

def _prepare_runtime_compose():
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    import shutil
    if not os.path.isfile(COMPOSE_FILE):
        temp_compose = f"{COMPOSE_FILE}.{os.getpid()}.tmp"
        try:
            shutil.copy2(_BUNDLED_COMPOSE_FILE, temp_compose)
            os.replace(temp_compose, COMPOSE_FILE)
        finally:
            if os.path.exists(temp_compose):
                try:
                    os.remove(temp_compose)
                except OSError:
                    pass
    bundled_app_dir = resource_path("app")
    if os.path.isdir(bundled_app_dir):
        # Dependencies are installed by the word-editor container at startup.
        # They can be numerous and PyInstaller's one-file extraction may not
        # preserve every node_modules entry on Windows; copying that tree here
        # made the desktop app crash before its window appeared.
        shutil.copytree(
            bundled_app_dir,
            RUNTIME_APP_DIR,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("node_modules"),
        )

_prepare_runtime_compose()

# ══════════════════════════════════════════════════════════
# UI preferences (ธีมสี / ขนาด UI) — โหลดก่อนสร้างหน้าต่างเพื่อให้ widget แรกใช้ค่าที่บันทึกไว้
# ══════════════════════════════════════════════════════════
def _load_ui_prefs() -> dict:
    """อ่าน app_preferences.json แบบเบาๆ ตรงนี้ (ไม่ผ่าน instance ของ AIServerApp เพราะยังไม่มี)
       เพื่อเอาค่า ui_theme/ui_scale มาใช้ตั้งค่าธีม/ขนาด UI ตั้งแต่ก่อนสร้างหน้าต่างโปรแกรม"""
    try:
        with open(app_path("app_preferences.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_UI_PREFS = _load_ui_prefs()

# ธีมที่รองรับ: "dark" (ดำ, ค่าเริ่มต้น) / "gray" (เทา #2C2F33)
UI_THEME_NAME = _UI_PREFS.get("ui_theme", "dark")
if UI_THEME_NAME not in ("dark", "gray"):
    UI_THEME_NAME = "dark"

# ขนาด UI (widget/window scaling ของ CustomTkinter) — ปรับได้จาก ⚙️ ตั้งค่า > 🎨 ธีมและขนาด UI
# มีประโยชน์ตอนเอาไปนำเสนอ/ต่อจอโปรเจกเตอร์ที่ตัวอักษรปกติดูเล็กเกินไป
try:
    UI_SCALE = float(_UI_PREFS.get("ui_scale", 1.0))
except (TypeError, ValueError):
    UI_SCALE = 1.0
UI_SCALE = min(max(UI_SCALE, 0.75), 2.5)   # กันค่ามั่ว/ค่าเก่าตกค้างที่จะทำให้ UI พังเล็ก/ใหญ่เกินไป
ctk.set_widget_scaling(UI_SCALE)
ctk.set_window_scaling(UI_SCALE)

# ── ธีมสี Arch Linux–inspired (ดำ/เข้มเป็นหลัก + accent ฟ้า-cyan) เป็นค่าเริ่มต้น ──
# ไฟล์ CTk color-theme (คุมสี default ของ widget ที่ไม่ได้ระบุ fg_color/text_color เองตรงๆ)
# ต่อ 1 ไฟล์ต่อ 1 ธีม อยู่ข้างๆ ตัวโปรแกรมเหมือนกันหมด — ถ้าหาไฟล์ที่เลือกไว้ไม่เจอ (เช่นลืมก็อปไปด้วย)
# จะ fallback ไปธีม dark ก่อน แล้วค่อย fallback ไปธีม "blue" เริ่มต้นของ CTk เป็นด่านสุดท้าย ไม่ให้แอปพัง
_THEME_JSON_FILES = {
    "dark":  "arch_dark_theme.json",
    "gray":  "theme_gray.json",
}
_THEME_PATH = resource_path(_THEME_JSON_FILES.get(UI_THEME_NAME, "arch_dark_theme.json"))
_FALLBACK_THEME_PATH = resource_path("arch_dark_theme.json")
try:
    if os.path.exists(_THEME_PATH):
        ctk.set_default_color_theme(_THEME_PATH)
    elif os.path.exists(_FALLBACK_THEME_PATH):
        ctk.set_default_color_theme(_FALLBACK_THEME_PATH)
    else:
        ctk.set_default_color_theme("blue")
except Exception:
    ctk.set_default_color_theme("blue")


def _run_self_test(relaunch: bool = False) -> int:
    """Run a non-destructive runtime smoke test used by build_onefile.bat.

    This is intentionally exercised from the frozen EXE on Windows after building.
    It checks the exact failure points that are otherwise only discovered after
    distribution: Tcl/Tk, bundled resources, all UI themes, Flask templates,
    public web dashboard, writable app-data, docker-compose YAML, and one-file
    self-relaunch.
    """
    checks = []

    def check(name, fn):
        try:
            fn()
            checks.append((name, True, "OK"))
        except Exception as exc:
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    check("Tcl/Tk", lambda: _self_test_tk())
    check("Bundled resources", lambda: _self_test_resources())
    check("All UI themes", lambda: _self_test_themes())
    check("Docker Compose YAML", lambda: _self_test_compose())
    check("Web dashboard without login", lambda: _self_test_web())
    check("Writable app-data", lambda: _self_test_data_dir())

    if relaunch and getattr(sys, "frozen", False):
        def _relaunch():
            result = subprocess.run(
                [sys.executable, "--self-test-child"],
                cwd=APP_DIR,
                capture_output=True,
                text=True,
                timeout=90,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "child EXE failed\n" + (result.stdout or "") + "\n" + (result.stderr or "")
                )
        check("One-file relaunch", _relaunch)

    lines = ["=== Intelligent Server Operations Platform self-test ==="]
    all_ok = True
    for name, ok, detail in checks:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        all_ok = all_ok and ok
    lines.append("=== SELF-TEST PASS ===" if all_ok else "=== SELF-TEST FAIL ===")
    report = "\n".join(lines) + "\n"
    print(report, end="")
    try:
        report_path = os.environ.get("ISOP_SELF_TEST_LOG") or app_path("self_test.log")
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        pass
    return 0 if all_ok else 1


def _self_test_tk():
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    root.destroy()


def _self_test_resources():
    required = [
        "templates/dashboard.html",
        "logo.png", "arch_dark_theme.json", "theme_gray.json",
        "docker-compose.yml",
    ]
    missing = [x for x in required if not os.path.exists(resource_path(x))]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    with Image.open(resource_path("logo.png")) as image:
        image.verify()


def _self_test_themes():
    original = UI_THEME_NAME
    for name in ("dark", "gray"):
        path = resource_path(_THEME_JSON_FILES[name])
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        ctk.set_default_color_theme(path)
    ctk.set_default_color_theme(resource_path(_THEME_JSON_FILES[original]))


def _self_test_compose():
    for compose_path in (resource_path("docker-compose.yml"), COMPOSE_FILE):
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid compose YAML: {compose_path}")
    if os.path.isdir(resource_path("app")) and not os.path.isdir(RUNTIME_APP_DIR):
        raise FileNotFoundError(RUNTIME_APP_DIR)


def _self_test_web():
    app = web_server.create_app()
    app.testing = True
    client = app.test_client()
    for path in ("/", "/dashboard"):
        response = client.get(path, follow_redirects=True)
        if response.status_code != 200:
            raise AssertionError(f"{path} did not open dashboard: {response.status_code}")
    for path in ("/login", "/setup", "/logout"):
        response = client.get(path, follow_redirects=False)
        if response.status_code != 302 or not response.headers.get("Location", "").endswith("/dashboard"):
            raise AssertionError(f"{path} did not redirect to dashboard: {response.status_code}")
    response = client.get("/api/status")
    if response.status_code != 200:
        raise AssertionError(f"dashboard API requires login: {response.status_code}")


def _self_test_data_dir():
    test_file = app_path(".__selftest__")
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("ok")
    with open(test_file, "r", encoding="utf-8") as f:
        if f.read() != "ok":
            raise AssertionError("app-data write/read mismatch")
    os.remove(test_file)


if "--self-test-child" in sys.argv:
    raise SystemExit(_run_self_test(relaunch=False))
elif "--self-test-relaunch" in sys.argv:
    raise SystemExit(_run_self_test(relaunch=True))


class Theme:
    """พาเลตสีกลาง ใช้ให้สีทั่วทั้งแอปสม่ำเสมอ — โทนดำ/เข้มแบบ Arch Linux + accent ฟ้า-cyan ทันสมัย"""
    # พื้นหลัง (มืดสุด → อ่อนขึ้นเรื่อยๆ)
    BG_ROOT      = "#08090b"
    BG_PANEL     = "#0e1014"
    BG_CARD      = "#13161b"
    BG_CARD_ALT  = "#1a1e25"
    BG_TERMINAL  = "#08090b"
    BORDER       = "#272d35"

    # accent หลัก (โทน Arch Linux cyan-blue)
    ACCENT       = "#e8edf2"
    ACCENT_HOVER = "#ffffff"
    ACCENT_BG    = "#1b2027"   # แถบพื้นหลังฟ้าเข้ม เช่น banner AI status ตอนปกติ

    # สี semantic
    SUCCESS       = "#22c55e"
    SUCCESS_HOVER = "#16a34a"
    WARNING       = "#facc15"
    WARNING_HOVER = "#ca8a04"
    DANGER        = "#ef4444"
    DANGER_HOVER  = "#b91c1c"
    DANGER_BG     = "#3f0d10"   # แถบพื้นหลังแดงเข้ม เช่น banner ตอน anomaly / toast แจ้งเตือน
    NEUTRAL            = "#3f3f46"
    NEUTRAL_HOVER      = "#2a2a30"
    NEUTRAL_DARK       = "#232328"
    NEUTRAL_DARK_HOVER = "#34343c"
    PURPLE       = "#a855f7"
    PURPLE_HOVER = "#7e22ce"

    # ข้อความ
    TEXT_PRIMARY   = "#f3f5f7"
    TEXT_SECONDARY = "#a4abb5"
    TEXT_MUTED     = "#858d99"   # ปรับให้อ่อนขึ้นจากเดิม #6b7280 — เดิมมองแทบไม่เห็นบนพื้นเข้ม
    TEXT_TERMINAL  = "#4ade80"
    TEXT_ACCENT    = "#e8edf2"

    # สีตัวหนังสือในแท็บ Action Log ตามระดับความสำคัญ (คำขอ: แจ้งเตือน = ตัวหนังสือสีแดง)
    LOG_DANGER  = "#ff5f5f"
    LOG_WARNING = "#facc15"
    LOG_SUCCESS = "#4ade80"
    LOG_PROMPT  = "#38bdf8"
    LOG_INFO    = "#9ca3af"


# ══════════════════════════════════════════════════════════
# ธีมสีที่รองรับนอกจากดำ: เทา #2C2F33
#
# ตั้งใจ "ไม่" เปลี่ยนสี ACCENT/SUCCESS/WARNING/DANGER/PURPLE/ACCENT_BG/DANGER_BG/สีในเทอร์มินัล/
# สีใน Action Log — สีกลุ่มนี้เป็นสีของ "ปุ่ม/badge/เทอร์มินัล" ที่มีพื้นหลังเป็นสีของตัวเองอยู่แล้ว
# (ไม่ใช่พื้นหลังของธีมหลัก) จึงคงคอนทราสต์ดีอยู่แล้วไม่ว่าจะสลับธีมไหนก็ตาม — เปลี่ยนเฉพาะกลุ่ม
# "พื้นหลัง/เส้นขอบ/ตัวหนังสือทั่วไป" ที่วางอยู่บนพื้นหลังหลักของโปรแกรมโดยตรงเท่านั้น
# ══════════════════════════════════════════════════════════
THEME_PALETTES = {
    "dark": {
        "BG_ROOT": "#08090b", "BG_PANEL": "#0e1014", "BG_CARD": "#13161b",
        "BG_CARD_ALT": "#1a1e25", "BORDER": "#272d35",
        "TEXT_PRIMARY": "#f3f5f7", "TEXT_SECONDARY": "#a4abb5", "TEXT_MUTED": "#858d99",
    },
    "gray": {
        "BG_ROOT": "#2C2F33", "BG_PANEL": "#33363c", "BG_CARD": "#393c42",
        "BG_CARD_ALT": "#40444b", "BORDER": "#4b4f57",
        "TEXT_PRIMARY": "#f2f3f5", "TEXT_SECONDARY": "#c3c7cf", "TEXT_MUTED": "#9aa0a8",
    },
}


def _apply_theme_palette(name: str):
    """เขียนทับค่าสีใน class Theme ให้ตรงกับธีมที่เลือกไว้"""
    palette = THEME_PALETTES.get(name, THEME_PALETTES["dark"])
    for key, value in palette.items():
        setattr(Theme, key, value)


def _active_theme_color(value):
    """Return the dark-mode color from a CustomTkinter theme value."""
    if isinstance(value, (list, tuple)):
        return value[-1] if value else None
    return value


def _live_theme_color_maps(old_name: str, new_name: str):
    """Build old-to-new color maps for app palette and CTk defaults."""
    old_palette = THEME_PALETTES[old_name]
    new_palette = THEME_PALETTES[new_name]
    palette_map = {
        old_palette[key].lower(): new_palette[key]
        for key in old_palette
        if key != "BG_ROOT" and old_palette[key].lower() != new_palette[key].lower()
    }

    old_path = resource_path(_THEME_JSON_FILES[old_name])
    new_path = resource_path(_THEME_JSON_FILES[new_name])
    with open(old_path, "r", encoding="utf-8") as stream:
        old_json = json.load(stream)
    with open(new_path, "r", encoding="utf-8") as stream:
        new_json = json.load(stream)

    widget_maps = {}
    for widget_class, old_options in old_json.items():
        new_options = new_json.get(widget_class, {})
        for option, old_value in old_options.items():
            old_color = _active_theme_color(old_value)
            new_color = _active_theme_color(new_options.get(option))
            if (isinstance(old_color, str) and isinstance(new_color, str)
                    and old_color.startswith("#") and new_color.startswith("#")
                    and old_color.lower() != new_color.lower()):
                widget_maps.setdefault(widget_class, {})[option] = (old_color.lower(), new_color)
    return palette_map, widget_maps


def _apply_theme_to_widget_tree(root, old_name: str, new_name: str, on_complete=None):
    """Recolor existing widgets in small batches to keep Tk responsive."""
    palette_map, widget_maps = _live_theme_color_maps(old_name, new_name)
    ctk.set_default_color_theme(resource_path(_THEME_JSON_FILES[new_name]))
    _apply_theme_palette(new_name)

    generic_options = (
        "fg_color", "bg_color", "background", "bg", "border_color",
        "text_color", "foreground", "fg", "placeholder_text_color",
        "insertbackground", "selectbackground", "selectforeground",
        "scrollbar_button_color", "scrollbar_button_hover_color",
    )
    options_by_class = {
        "CTk": ("fg_color",), "CTkToplevel": ("fg_color",),
        "CTkFrame": ("fg_color", "bg_color", "border_color", "top_fg_color"),
        "CTkLabel": ("fg_color", "bg_color", "border_color", "text_color"),
        "CTkButton": ("fg_color", "bg_color", "border_color", "text_color", "hover_color"),
        "CTkEntry": ("fg_color", "bg_color", "border_color", "text_color", "placeholder_text_color"),
        "CTkTextbox": ("fg_color", "bg_color", "border_color", "text_color",
                       "scrollbar_button_color", "scrollbar_button_hover_color"),
        "CTkProgressBar": ("fg_color", "bg_color", "border_color", "progress_color"),
        "CTkSlider": ("fg_color", "progress_color", "button_color", "button_hover_color"),
        "CTkSwitch": ("fg_color", "progress_color", "button_color", "button_hover_color", "text_color"),
        "CTkCheckBox": ("fg_color", "border_color", "hover_color", "checkmark_color", "text_color"),
        "CTkRadioButton": ("fg_color", "border_color", "hover_color", "text_color"),
        "CTkOptionMenu": ("fg_color", "button_color", "button_hover_color", "text_color"),
        "CTkComboBox": ("fg_color", "border_color", "button_color", "button_hover_color", "text_color"),
        "CTkScrollbar": ("fg_color", "button_color", "button_hover_color"),
        "CTkSegmentedButton": ("fg_color", "selected_color", "selected_hover_color",
                               "unselected_color", "unselected_hover_color", "text_color"),
        "CTkScrollableFrame": ("label_fg_color",),
        "Canvas": ("background",), "Text": ("background", "foreground", "insertbackground",
                                             "selectbackground", "selectforeground"),
        "Entry": ("background", "foreground", "insertbackground", "selectbackground", "selectforeground"),
        "Scrollbar": ("background", "activebackground", "troughcolor"),
    }

    def recolor(widget):
        widget_class = type(widget).__name__
        if widget_class == "Treeview":
            try:
                tree_style = ttk.Style(widget)
                tree_style.configure(
                    "Resource.Treeview", background=Theme.BG_PANEL,
                    foreground=Theme.TEXT_PRIMARY, fieldbackground=Theme.BG_PANEL,
                    rowheight=25, font=("Segoe UI", 10))
                tree_style.configure(
                    "Resource.Treeview.Heading", background=Theme.BG_CARD_ALT,
                    foreground=Theme.TEXT_PRIMARY, font=("Segoe UI", 10, "bold"),
                    bordercolor=Theme.BORDER, relief="flat")
                tree_style.map("Resource.Treeview.Heading",
                               background=[("active", Theme.ACCENT_BG)],
                               foreground=[("active", Theme.TEXT_PRIMARY)])
            except tk.TclError:
                pass
        class_map = widget_maps.get(widget_class, {})
        options = set(options_by_class.get(widget_class, generic_options)).union(class_map)
        for option in options:
            try:
                current = widget.cget(option)
            except Exception:
                continue
            active = _active_theme_color(current)
            replacement = None
            if widget_class in ("CTk", "CTkToplevel") and option == "fg_color":
                old_root = THEME_PALETTES[old_name]["BG_ROOT"]
                if isinstance(active, str) and active.lower() == old_root.lower():
                    replacement = THEME_PALETTES[new_name]["BG_ROOT"]
            if replacement is None and option in class_map:
                old_color, new_color = class_map[option]
                if isinstance(active, str) and active.lower() == old_color:
                    replacement = new_color
            if replacement is None and isinstance(active, str):
                replacement = palette_map.get(active.lower())
            if replacement is not None:
                try:
                    widget.configure(**{option: replacement})
                except Exception:
                    pass

    pending = [root]
    cursor = 0

    def recolor_batch():
        nonlocal cursor
        end = min(cursor + 30, len(pending))
        while cursor < end:
            widget = pending[cursor]
            cursor += 1
            recolor(widget)
            try:
                pending.extend(widget.winfo_children())
            except Exception:
                pass
        if cursor < len(pending):
            try:
                root.after(1, recolor_batch)
            except tk.TclError:
                return
            return
        try:
            root.configure(fg_color=THEME_PALETTES[new_name]["BG_ROOT"])
        except tk.TclError:
            return
        if on_complete:
            try:
                on_complete()
            except (tk.TclError, RuntimeError):
                pass

    root.after_idle(recolor_batch)


_apply_theme_palette(UI_THEME_NAME)

# ─────────────────────────────────────────────
# ค่าเริ่มต้น resource limits (เซฟลง JSON)
# ─────────────────────────────────────────────
DEFAULT_LIMITS = {"cpu_limit": 100, "ram_limit_mb": 2048, "ai_ram_enabled": False,
                 "auto_restart_unhealthy": False}

# ค่าเริ่มต้นของ "แจ้งเตือน" (ต่างจาก limit — ไม่ได้บังคับ container แค่แจ้งเมื่อใช้งานเกิน)
DEFAULT_ALERT = {"cpu_pct": 90.0, "ram_pct": 90.0}

LOGO_FILE = resource_path("logo.png")

# ── รายการคำสั่งพื้นฐานสำหรับกด Tab หาในแท็บ 💻 Terminal (รันบนเครื่อง/Windows โดยตรง) ──
# เผื่อคนลืมคำสั่งหรือไม่อยากพิมพ์เอง — กด Tab ตอนช่องว่างก็ขึ้นให้เลือกได้เลย
TERMINAL_COMMAND_SUGGESTIONS = [
    "dir", "cls", "cd..", "cd \\", "echo %cd%",
    "ipconfig", "ipconfig /all", "ping 127.0.0.1", "ping 8.8.8.8",
    "tasklist", "netstat -ano", "whoami", "systeminfo",
    "docker ps", "docker ps -a", "docker images", "docker stats",
    "docker-compose up -d", "docker-compose down", "docker-compose ps",
    "docker-compose logs -f", "docker logs", "docker restart",
    "python --version", "pip list", "pip --version",
    "git status", "git pull", "git log --oneline -10",
    "exit",
]

# ── รายการคำสั่ง shell พื้นฐานสำหรับกด Tab หาในช่อง Terminal ของแต่ละ container (docker exec) ──
CONTAINER_SHELL_SUGGESTIONS = [
    "ls -la", "pwd", "whoami", "ps aux", "top -b -n 1",
    "df -h", "free -m", "cat /etc/os-release", "env",
    "which node", "node -v", "npm -v", "python3 --version",
    "curl -I http://localhost", "ping -c 3 8.8.8.8",
    "exit",
]

# ── หา URL แบบ https://xxxx.trycloudflare.com จาก output ของ cloudflared (⚡ Quick Tunnel
#    ต่อ server แต่ละตัวในแท็บ 🖥️ Server Manager) ──
QUICK_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def show_splash_screen(duration_ms: int = 3000):
    """โชว์หน้าจอโลโก้ (splash screen) สักครู่ตอนเปิดโปรแกรม แล้วปิดตัวเองอัตโนมัติก่อนเข้าโปรแกรมจริง
       ใช้ tk.Tk() ธรรมดา (ไม่ใช่ ctk) เพราะแค่โชว์รูป+ข้อความเฉยๆ ไม่ต้องพึ่งธีมอะไรพิเศษ
       และเลี่ยงปัญหาสร้าง CTk root ซ้อนกับ AIServerApp ที่จะตามมาทีหลัง

       พื้นหลังหน้าต่างทำเป็น "โปร่งใส" (เฉพาะ Windows ผ่าน -transparentcolor) เลยไม่มีกล่อง
       สีทึบให้เห็นเลย จะเห็นแค่ตัวโลโก้ (+ ข้อความ) ลอยอยู่กลางจอเท่านั้น — ถ้าไฟล์ logo.png
       เองมีพื้นหลังทึบฝังอยู่ในรูปอยู่แล้ว (ไม่ใช่ PNG แบบโปร่งใส) จะยังเห็นพื้นหลังของรูปนั้น
       ตามปกติ อันนี้แก้ที่ตัวไฟล์รูปเองแทน โค้ดนี้คุมได้แค่พื้นหลังของหน้าต่างเท่านั้น"""
    splash = tk.Tk()
    splash.overrideredirect(True)          # ไม่มีแถบหัวหน้าต่าง/กรอบ ดูเป็น splash จริงๆ

    # สีเฉพาะที่แทบไม่มีใครใช้จริง ใช้เป็น "คีย์" ทำให้พื้นหลังส่วนนี้ใสไปเลย (Windows เท่านั้น)
    transparent_key = "#0a0a0b"
    splash.configure(bg=transparent_key)
    try:
        splash.attributes("-transparentcolor", transparent_key)
    except tk.TclError:
        pass  # OS ที่ไม่รองรับ (เช่น Linux/Mac) จะเห็นพื้นหลังสีทึบแทน ไม่ error/ไม่พัง
    splash.attributes("-topmost", True)

    logo_img = None
    if os.path.exists(LOGO_FILE):
        try:
            pil_img = Image.open(LOGO_FILE)
            pil_img.thumbnail((860, 860), Image.LANCZOS)   # ขนาดโลโก้สูงสุด ~860x860
            logo_img = ImageTk.PhotoImage(pil_img)
        except Exception:
            logo_img = None

    if logo_img is not None:
        logo_label = tk.Label(splash, image=logo_img, bg=transparent_key,
                              borderwidth=0, highlightthickness=0)
        logo_label.image = logo_img  # กัน garbage collector เก็บรูปไปทั้งที่ยังใช้อยู่
        logo_label.pack(padx=50, pady=(46, 14))
    else:
        # หาไฟล์ logo.png ไม่เจอ — โชว์ไอคอนตัวหนังสือแทนไปก่อน ไม่ให้ splash ว่างเปล่า/error
        tk.Label(splash, text="🖥️", font=("Segoe UI", 60),
                 bg=transparent_key, fg=Theme.TEXT_ACCENT).pack(padx=60, pady=(46, 10))

    tk.Label(splash, text="Intelligent Server Operations Platform", font=("Segoe UI", 16, "bold"),
             bg=transparent_key, fg=Theme.TEXT_PRIMARY).pack(pady=(0, 4))
    tk.Label(splash, text="กำลังเปิดโปรแกรม...", font=("Segoe UI", 11),
             bg=transparent_key, fg=Theme.TEXT_MUTED).pack(pady=(0, 36))

    splash.update_idletasks()
    win_w = max(320, splash.winfo_reqwidth())
    win_h = splash.winfo_reqheight()
    x = (splash.winfo_screenwidth() - win_w) // 2
    y = (splash.winfo_screenheight() - win_h) // 2
    splash.geometry(f"{win_w}x{win_h}+{x}+{y}")

    splash.after(duration_ms, splash.destroy)
    splash.mainloop()


class AIServerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self._ui_callback_queue = queue.SimpleQueue()
        self._shutdown_started = False
        self._background_stop = threading.Event()
        self._compose_task_lock = threading.Lock()
        self.title("Intelligent Server Operations Platform — ระบบปฏิบัติการและบริหารจัดการเครื่องแม่ข่าย")
        self.geometry("1150x750")
        self.minsize(900, 600)
        self.configure(fg_color=Theme.BG_ROOT)

        # เดิม DataCollector()/Notifier() ไม่รับ path มาเลย เลยใช้ค่า default เป็นชื่อไฟล์
        # แบบ relative ("metrics_db.csv"/"notifier_config.json") ซึ่งขึ้นกับ current working
        # directory ตอนรัน — ส่ง path เต็มจาก app_path() ไปตรงๆ กันปัญหานี้
        self.collector   = DataCollector(db_file=app_path("metrics_db.csv"))
        self.brain       = AIServerBrain()
        # Train the unsupervised baseline from the most recent 30 days when
        # prior samples exist. A fresh installation remains in warm-up mode.
        training_rows = self.collector.get_training_history(days=30, max_rows=1500)
        self._model_status = self.brain.train_from_history(training_rows, days=30)
        print(f"[AI] Isolation Forest: {self._model_status}")
        self._lstm_setup_in_progress = False
        self._lstm_samples_since_fit = 0
        self.remediator  = Remediator()
        self.notifier    = Notifier(config_file=app_path("notifier_config.json"),
                                    on_alert=self._show_anomaly_alert)

        self.servers_data    = []

        self.settings_file   = app_path("app_settings.json")
        self.limits_file     = app_path("resource_limits.json")
        self.alerts_file     = app_path("alert_thresholds.json")
        self.email_config_file = app_path("email_config.json")
        self.preferences_file  = app_path("app_preferences.json")   # RAM สูงสุด / ตำแหน่ง backup / เปิด-ปิดระบบ backup
        self.yaml_paths      = {}
        self.yaml_labels     = {}   # filepath -> ชื่อที่ผู้ใช้ตั้งเอง (กันชื่อซ้ำ)

        # ── ตั้งค่าอีเมลผู้ส่ง (สำหรับปุ่ม "ส่งลิงก์ dashboard" ในแท็บ Dashboard) ──
        self._email_config: dict = self._load_email_config()

        # ── การตั้งค่าโปรแกรม (เปิดจากปุ่ม ☰ มุมขวาบน): RAM สูงสุด / backup ──
        self.prefs: dict = self._load_preferences()

        # ── ระบบ backup อัตโนมัติ: backup ทันทีตอนเซิร์ฟเวอร์เริ่ม แล้วซ้ำทุก 1 ชั่วโมง
        #    ยังไม่ .start() ตรงนี้ — จะเปิดตอนท้าย __init__ พร้อมกับตอนเริ่ม web server
        #    ถ้าผู้ใช้เปิดใช้งานและเลือกโฟลเดอร์ปลายทางไว้แล้วเท่านั้น ──
        self.backup_scheduler = BackupScheduler(
            get_sources=self._gather_backup_sources,
            get_dest_dir=lambda: self.prefs.get("backup_path", ""),
            on_log=self._backup_log,
            interval_sec=3600,
        )

        # ── ระบบ 🤖 AI RAM (ต่อ server): ปรับ RAM limit อัตโนมัติเฉพาะ container ที่เปิดสวิตช์ไว้
        #    เท่านั้น — container ไหนไม่เปิดสวิตช์ (ค่าเริ่มต้น) ยังคงเป็น Manual ตามเดิมทุกประการ
        #    เริ่มลูปพื้นหลังได้เลยตั้งแต่ตอนนี้ เพราะเช็คทีละ container ว่าเปิดสวิตช์หรือยังเองอยู่แล้ว ──
        self.ram_ai_manager = RamAIManager(
            get_candidates=lambda: [n for n, info in self.tracked_containers.items()
                                     if info.get("stype") == "docker"],
            is_enabled=lambda n: (self.get_limit(n).get("ai_ram_enabled", False)
                                   and self._docker_stats_cache.get(n, {}).get("online", False)),
            get_usage_mb=self._get_container_usage_mb,
            get_ceiling_mb=lambda: self.prefs.get("max_ram_mb", 4096),
            get_current_limit_mb=lambda n: self.get_limit(n).get("ram_limit_mb", 2048),
            apply_limit=self._ai_apply_ram_limit,
            on_log=self._ram_ai_log,
            interval_sec=20,
        )
        # ปิด docker-compose (ถ้ามี) ให้เรียบร้อยก่อนโปรแกรมจะปิดจริง — กด X ที่มุมขวาบน
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)

        # history สำหรับ mini-graph (60 จุด)
        self._cpu_hist:  list[float] = []
        self._ram_hist:  list[float] = []
        self._disk_hist: list[float] = []
        self._last_status_error_log = 0.0

        # resource limits per container/server: {name: {cpu_limit, ram_limit_mb}}
        self._resource_limits: dict[str, dict] = self._load_limits()

        # ── /ral: threshold "แจ้งเตือน" ต่อ server: {name: {cpu_pct, ram_pct}} ──
        self._alert_thresholds: dict[str, dict] = self._load_alerts()
        self._last_alert_ts: dict[str, float] = {}   # กันแจ้งเตือนถี่เกินไปต่อ server
        self._alert_cooldown: float = 30.0            # วินาที

        # กันการสั่ง remediation ซ้ำถี่เกินไปตอนยังผิดปกติต่อเนื่อง (ทำให้ระบบเสถียร/บาลานซ์ดีขึ้น)
        self._last_remediation_ts: float = 0.0
        self._remediation_cooldown: float = 300.0  # วินาที — ลดการทำซ้ำ/ล้างไฟล์ซ้ำขณะยังผิดปกติ
        self._last_anomaly_state: bool = False
        # Predictive alerts are evaluated only when a new persisted sample arrives,
        # and re-arm after the forecast has stayed below the clear threshold.
        self._predictive_alert_state: dict[str, dict] = {}
        self._cached_lstm_capacity_forecast: dict | None = None
        self._action_event_ids: set[str] = set()
        self._recent_action_log_texts: dict[str, float] = {}

        # ── Server Dashboard: ดู CPU/RAM/Disk + Terminal ของแต่ละ server แยกกัน ──
        self.tracked_containers:   dict = {}   # name -> {"yaml_key","service_name","stype","ip"}
        self.yaml_container_names: dict = {}   # yaml_key -> [container names...] (ลบพร้อมกันตอน Delete YAML)

        # ── รายชื่อ container ที่ถูก Import เข้า Server Manager แล้ว — เซฟลงไฟล์นี้ทุกครั้งที่
        #    เพิ่ม/ลบ เพื่อให้ server.py (Flask, คนละ process ข้าม thread แต่คนละ "แหล่งข้อมูล")
        #    อ่านไปกรอง /api/containers ได้ ไม่ให้เว็บโชว์ container ที่ยังไม่ได้ import ──
        # ไฟล์เดียวกับที่ server.py (Flask) อ่าน — ต้องเป็น absolute path เดียวกันเป๊ะ
        # (ใช้ app_path() จาก paths.py ทั้งสองฝั่ง ดูรายละเอียดได้ในไฟล์ paths.py)
        self.tracked_containers_file = app_path("tracked_containers.json")
        self._server_dash_widgets: dict = {}   # name -> widget ต่างๆ ในการ์ดของ Server Dashboard
        self._server_manager_status_labels: dict = {}  # name -> ป้ายสถานะใน Server Manager
        self._docker_stats_cache:  dict = {}   # name -> {"online","cpu_pct","mem_usage","mem_pct","disk_str"}
        self._server_ping_cache: dict = {}     # name -> {"online", "latency_ms", "method", "checked_at"}
        self._remote_metrics_file = app_path("remote_metrics.csv")
        self._remote_metrics_lock = threading.Lock()
        self._ssh_profiles_file = app_path("ssh_profiles.json")
        self._ssh_profiles = self._load_ssh_profiles()
        self._last_health_event_ts: dict[str, float] = {}
        # In-memory incident timer; duration is observed at Docker poll resolution.
        self._unhealthy_since: dict[str, float] = {}
        self._unhealthy_poll_streak: dict[str, int] = {}
        self._last_auto_restart_ts: dict[str, float] = {}
        self._auto_restart_inflight: set[str] = set()
        self._auto_restart_waiting_recovery: dict[str, float] = {}
        self._auto_restart_cooldown = 300.0
        self._log_procs:   dict = {}           # name -> subprocess.Popen ของ docker logs -f ที่กำลังสตรีม
        self._log_streams: dict = {}           # name -> True ถ้ากำลังสตรีม log อยู่

        # ── ⚡ Quick Tunnel ต่อ server แต่ละตัว (เปิดคนละตัวพร้อมกันได้) — key คือชื่อ container/server
        #    value: {"proc", "url", "port", "row_btn", "dialog", "status_label", "toggle_btn", "port_entry"} ──
        self._server_quick_tunnels: dict = {}

        # ── history คำสั่งของแท็บ 💻 Terminal (กด ↑/↓ ทวนคำสั่งเก่า, Tab หาใน terminal ได้) ──
        self.terminal_history: list[str] = []
        self.log_command_history: list[str] = []   # history ของช่องคำสั่ง /ping /limit /ral ในแท็บ Action Log

        # ── หน้าต่างแจ้งเตือน Anomaly แบบ custom (topmost จริง) — เก็บ reference ไว้กันเปิดซ้อนกัน
        #    หลายบานตอนเกิด anomaly ถี่ๆ (ถึงจะมี cooldown ใน notifier.py กันอยู่แล้วก็ตาม) ──
        self._anomaly_alert_win = None
        self._anomaly_pulse_after_id = None
        self._redraw_after_ids: dict[str, str] = {}

        self.setup_ui()
        self.after(50, self._drain_ui_callbacks)
        self.after(250, self._prepare_lstm_model)
        self.update_realtime_status()
        self.load_saved_yamls()
        self._save_tracked_containers()  # sync ไฟล์ตอนเริ่ม เผื่อไม่มี server ที่ import ไว้เลย
        # เริ่มหลังจากโหลดรายการ container และ cache ที่ callback ของ AI RAM อ่านครบแล้ว
        self.ram_ai_manager.start()

        # ── เก็บ CPU/RAM/Disk ของแต่ละ container ใน background thread แยก
        #    (ไม่ให้ subprocess "docker stats" ไปบล็อก UI thread หลัก)
        #
        #    ใช้ self.after(...) เพื่อ "เลื่อน" การ start thread ออกไปจนกว่า mainloop()
        #    จะเริ่มทำงานจริงก่อน — ถ้า start thread ตรงนี้เลย แล้ว thread ทำงานเสร็จเร็วมาก
        #    (เช่น docker-compose up คืนค่าเกือบทันทีเพราะ container รันอยู่แล้ว) thread นั้น
        #    อาจเรียก self.after(...) ข้ามไปจาก thread อื่นก่อนที่ mainloop() จะเริ่ม
        #    ทำให้เกิด RuntimeError: main thread is not in main loop ──
        self.after(100, lambda: threading.Thread(target=self._docker_stats_poll_loop, daemon=True).start())
        self.after(150, lambda: threading.Thread(target=self._server_ping_poll_loop, daemon=True).start())

        # ── เปิด docker-compose.yml (เว็บโปรแกรม เช่น word-editor + cloudflare tunnel)
        #    อัตโนมัติตอนโปรแกรมนี้เริ่มทำงาน (ทำใน background กัน UI ค้างตอนดึง image ครั้งแรก) ──
        self.after(100, lambda: threading.Thread(target=self._compose_up_on_start, daemon=True).start())

        # ── เปิดเว็บเซิร์ฟเวอร์สำหรับดูจากมือถือ/เว็บ (background thread) ──
        self.web_port = 5000
        self._start_web_server()

        # ── เริ่มระบบ backup อัตโนมัติพร้อมกับตอนเปิดเซิร์ฟเวอร์ (ถ้าผู้ใช้เปิดใช้งานไว้ใน ⚙️ ตั้งค่า
        #    และเลือกตำแหน่งจัดเก็บไว้แล้ว) — backup ทันที 1 ครั้ง แล้วซ้ำทุก 1 ชั่วโมงหลังจากนั้น ──
        if self.prefs.get("backup_enabled") and self.prefs.get("backup_path"):
            self.backup_scheduler.start()

    # ══════════════════════════════════════════════════════════
    # Thread safety helper
    # ══════════════════════════════════════════════════════════
    def _schedule_canvas_redraw(self, key: str, callback, delay_ms: int = 100):
        """Coalesce rapid resize/refresh events into one canvas redraw."""
        previous = self._redraw_after_ids.get(key)
        if previous:
            try:
                self.after_cancel(previous)
            except tk.TclError:
                pass

        def redraw():
            self._redraw_after_ids.pop(key, None)
            try:
                if self.winfo_exists():
                    callback()
            except (tk.TclError, RuntimeError):
                pass

        try:
            self._redraw_after_ids[key] = self.after(max(0, int(delay_ms)), redraw)
        except tk.TclError:
            self._redraw_after_ids.pop(key, None)

    def _safe_after(self, delay, func, *args):
        """Queue UI work from background threads; only the Tk thread calls after/widgets."""
        self._ui_callback_queue.put((max(0, int(delay)), func, args))

    def _invoke_ui_callback(self, func, args):
        try:
            if self.winfo_exists():
                func(*args)
        except (RuntimeError, tk.TclError):
            pass  # Window/mainloop has closed; discard late worker updates.
        except Exception as exc:
            print(f"[UI callback] {exc}")

    def _drain_ui_callbacks(self):
        """Run queued callbacks on the Tk thread and keep the queue alive until shutdown."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        for _ in range(100):
            try:
                delay, func, args = self._ui_callback_queue.get_nowait()
            except queue.Empty:
                break
            if delay:
                try:
                    self.after(delay, self._invoke_ui_callback, func, args)
                except tk.TclError:
                    return
            else:
                self._invoke_ui_callback(func, args)
        try:
            if self.winfo_exists():
                self.after(50, self._drain_ui_callbacks)
        except tk.TclError:
            pass

    # ══════════════════════════════════════════════════════════
    # Web server (Flask) สำหรับดูสถานะจากมือถือ/เว็บ
    # ══════════════════════════════════════════════════════════
    def _get_local_ip(self) -> str:
        """หา IP ของเครื่องในเครือข่ายบ้าน (LAN) เพื่อแสดงให้ผู้ใช้เปิดจากมือถือ"""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _start_web_server(self):
        self._public_dashboard_url = None   # จะถูกเติมทีหลังโดย _poll_tunnel_url ถ้ามี dashboard-tunnel
        self._tunnel_status = "waiting"     # waiting | ready | unavailable
        try:
            web_server.start_server_background(host="0.0.0.0", port=self.web_port)
            ip = self._get_local_ip()
            self._lan_dashboard_url = f"http://{ip}:{self.web_port}"
            self.after(500, self._update_web_url_label)
        except Exception as e:
            self._lan_dashboard_url = None
            self.after(500, lambda: self.web_url_label.configure(
                text=f"❌ เปิดเว็บเซิร์ฟเวอร์ไม่สำเร็จ: {e}", text_color=Theme.DANGER))

    def _update_web_url_label(self):
        """อัปเดตข้อความแสดง URL ของ dashboard — โชว์ทั้งลิงก์วงแลน (ใช้ในบ้าน/WiFi เดียวกัน)
           และลิงก์สาธารณะผ่าน cloudflared tunnel (ใช้ได้จากทุกที่ ถ้ามี container
           'dashboard-tunnel' ทำงานอยู่) พร้อมกัน"""
        lines = []
        if getattr(self, "_lan_dashboard_url", None):
            lines.append(f"🏠 ในเครือข่ายเดียวกัน (WiFi/แลน):\n{self._lan_dashboard_url}")

        if self._tunnel_status == "ready" and getattr(self, "_public_dashboard_url", None):
            lines.append(f"🌍 จากที่ไหนก็ได้ (ลิงก์เปลี่ยนทุกครั้งที่เปิดโปรแกรม):\n{self._public_dashboard_url}")
        elif self._tunnel_status == "unavailable":
            lines.append("🌍 ลิงก์สาธารณะ: ❌ ยังหาลิงก์ tunnel ไม่เจอ (ตรวจ service dashboard-tunnel ใน Docker Compose)")
        else:
            lines.append("🌍 ลิงก์สาธารณะ: ⏳ กำลังรอ tunnel เชื่อมต่อ...")

        self.web_url_label.configure(text="\n\n".join(lines), text_color=Theme.TEXT_ACCENT)

    # ══════════════════════════════════════════════════════════
    # docker-compose.yml (เว็บโปรแกรม) — เปิดตอนโปรแกรมเริ่ม / ปิดตอนโปรแกรมปิด
    # ══════════════════════════════════════════════════════════
    def _is_docker_available(self) -> bool:
        """เช็คว่า Docker Desktop/daemon พร้อมใช้งานจริงไหม (ไม่ใช่แค่มีคำสั่ง docker ติดตั้งอยู่)
           กันปัญหา 'Docker Desktop ยังไม่ได้เปิด' หรือเพิ่งเปิดแต่ยังโหลดไม่เสร็จตอนโปรแกรมนี้เริ่ม"""
        try:
            r = proc_utils.run_hidden(["docker", "info"], capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_compose(self, args: list) -> tuple[bool, str]:
        """รัน docker compose บนไฟล์ docker-compose.yml ที่อยู่โฟลเดอร์เดียวกับ main.py
           ลองทั้ง 'docker-compose' (ตัวเก่า) และ 'docker compose' (plugin ใหม่)
           เผื่อเครื่องมีติดตั้งแค่ตัวใดตัวหนึ่ง"""
        if not os.path.exists(COMPOSE_FILE):
            return False, f"ไม่พบไฟล์ {COMPOSE_FILE}"
        candidates = [
            ["docker-compose", "-p", "ai-server-management", "-f", COMPOSE_FILE] + args,
            ["docker", "compose", "-p", "ai-server-management", "-f", COMPOSE_FILE] + args,
        ]
        last_err = "ไม่ทราบสาเหตุ"
        for cmd in candidates:
            try:
                r = proc_utils.run_hidden(cmd, capture_output=True, text=True, timeout=180)
                if r.returncode == 0:
                    return True, (r.stdout or "").strip()
                last_err = (r.stderr or r.stdout or "ไม่ทราบสาเหตุ").strip()
            except FileNotFoundError:
                last_err = f"ไม่พบคำสั่ง {cmd[0]}"
                continue
            except subprocess.TimeoutExpired:
                last_err = "หมดเวลาการทำงาน (timeout)"
                continue
        return False, last_err

    def _compose_up_on_start(self):
        """Serialize startup/recheck compose tasks and let shutdown cancel queued retries."""
        if self._background_stop.is_set():
            return
        if not self._compose_task_lock.acquire(blocking=False):
            self._safe_after(0, self._append_action_log,
                             "⏳ Docker Compose กำลังทำงานอยู่ — ข้ามคำสั่งซ้ำ", "warning")
            return
        try:
            self._compose_up_on_start_locked()
        finally:
            self._compose_task_lock.release()

    def _compose_up_on_start_locked(self):
        """สั่ง docker-compose up -d อัตโนมัติตอนโปรแกรมเริ่ม (background — ครั้งแรกอาจ
           ดึง image นานหลายนาที ไม่ให้ไปบล็อกหน้าต่างโปรแกรมหลัก)

           ก่อนสั่งจริง จะรอ Docker Desktop ให้พร้อมก่อน (นานสุด 2 นาที) เพราะเป็นเรื่องปกติมาก
           ที่คนจะเปิดโปรแกรมนี้พร้อมๆ กับเพิ่งเปิด Docker Desktop — ถ้าไม่รอแล้วสั่ง docker-compose
           ไปเลยจะเจอ error 'failed to connect to the docker API ... npipe' เพราะ daemon ยังไม่ตื่น"""
        if not os.path.exists(COMPOSE_FILE):
            return  # ไม่มีไฟล์ก็ข้ามเงียบๆ ไม่ต้อง log กวนใจ (ไม่ใช่ทุกเครื่องจะมี)

        waited = 0.0
        max_wait = 120.0
        warned = False
        while not self._is_docker_available():
            if self._background_stop.is_set():
                return
            if not warned:
                self._safe_after(0, self._append_action_log,
                          "⏳ รอ Docker Desktop เปิดให้พร้อมก่อน... (ถ้ายังไม่ได้เปิด Docker Desktop "
                          "กรุณาเปิดแอป Docker Desktop ก่อน แล้วรอสักครู่)", "warning")
                warned = True
            if waited >= max_wait:
                self._safe_after(0, self._append_action_log,
                          "❌ Docker Desktop ยังไม่พร้อมใช้งานหลังรอ 2 นาที — เปิด Docker Desktop "
                          "แล้วรอจนไอคอนขึ้นว่า 'Docker Desktop is running' จากนั้นกด "
                          "🔄 เปิด docker-compose ใหม่ ที่แท็บ Dashboard", "danger")
                self._tunnel_status = "unavailable"
                self._safe_after(0, self._update_web_url_label)
                return
            time.sleep(5)
            waited += 5

        if self._background_stop.is_set():
            return
        ok, msg = self._run_compose(["up", "-d"])
        if ok:
            self._safe_after(0, self._append_action_log,
                      f"✅ เปิด docker-compose ({os.path.basename(COMPOSE_FILE)}) เรียบร้อยตอนเริ่มโปรแกรม",
                      "success")
            # container "dashboard-tunnel" (ถ้ามีอยู่ใน docker-compose.yml) เปิด tunnel
            # สาธารณะให้ Flask dashboard (พอร์ต 5000) — รอ cloudflared เชื่อมต่อเสร็จ
            # แล้วดึงลิงก์ trycloudflare.com ออกมาแสดง/ใช้ส่งอีเมลต่อ
            self._poll_tunnel_url()
        else:
            self._safe_after(0, self._append_action_log,
                      f"❌ เปิด docker-compose ไม่สำเร็จตอนเริ่มโปรแกรม: {msg}", "danger")
            self._tunnel_status = "unavailable"
            self._safe_after(0, self._update_web_url_label)

    def _poll_tunnel_url(self, tries: int = 90, delay_sec: float = 2.0):
        """รอ cloudflared (container 'dashboard-tunnel') สร้างลิงก์ public แบบ trycloudflare.com
           สำหรับเข้าเว็บ dashboard จากนอกวงแลน/router อื่นได้ — เป็น quick tunnel เลยไม่ต้องมี
           Cloudflare account แต่ลิงก์จะเปลี่ยนใหม่ทุกครั้งที่เปิดโปรแกรม (ผู้ใช้แจ้งว่ารับได้)

           tries=90 * delay_sec=2 = รอสูงสุด ~3 นาที เพราะครั้งแรกที่รัน docker-compose up
           ต้อง pull image cloudflare/cloudflared ก่อน (อาจช้าถ้าเน็ตไม่แรง) ถ้ารอนานขนาดนี้
           แล้วยังไม่เจอ ค่อยฟันธงว่ามีปัญหาจริง พร้อมบอกสาเหตุที่เจาะจง (ไม่ใช่แค่ 'หาไม่เจอ')

           เรียกจาก background thread เดิมของ _compose_up_on_start อยู่แล้ว หรือจาก thread ใหม่
           ที่สร้างจากปุ่ม '🔄 เช็คลิงก์อีกครั้ง' ก็ได้ — ไม่ว่ากรณีไหน sleep ตรงๆ ได้โดยไม่ทำให้
           หน้าต่างหลักค้าง เพราะไม่ได้รันบน main thread"""
        self._tunnel_status = "waiting"
        self._safe_after(0, self._update_web_url_label)

        last_diag = {}
        for _ in range(tries):
            if self._background_stop.is_set():
                return
            try:
                last_diag = docker_ops.diagnose_tunnel("dashboard-tunnel")
            except Exception as e:
                # กันเหนียว: ถ้า diagnose_tunnel โยน exception ที่ไม่คาดคิดออกมา (เช่น encoding
                # ปัญหาเดิมที่เคยเจอ หรือเหตุอื่นในอนาคต) ห้ามปล่อยให้ thread นี้ตายเงียบๆ เด็ดขาด —
                # ไม่งั้นสถานะจะค้างที่ "waiting" ตลอดไปโดยไม่มี error โผล่ให้เห็นเลย (เพราะ .exe ไม่มี
                # console ให้ print stderr ออกมา) ให้ log ไว้ใน Action Log แล้วค่อยลองใหม่รอบถัดไปแทน
                self._safe_after(0, self._append_action_log,
                          f"⚠️ เช็คสถานะ tunnel รอบนี้ล้มเหลว (จะลองใหม่ต่อ): {e}", "warning")
                time.sleep(delay_sec)
                continue
            if last_diag.get("url"):
                self._public_dashboard_url = last_diag["url"]
                self._tunnel_status = "ready"
                self._safe_after(0, self._update_web_url_label)
                self._safe_after(0, self._append_action_log,
                          f"✅ เปิดลิงก์สาธารณะสำหรับ dashboard แล้ว: {last_diag['url']}", "success")
                return
            time.sleep(delay_sec)

        # หมดเวลารอแล้วยังไม่เจอ — บอกสาเหตุที่ชัดเจนที่สุดเท่าที่เช็คได้ ไม่ใช่แค่ข้อความกลางๆ
        if not last_diag.get("exists"):
            reason = ("ยังไม่มี container ของ service 'dashboard-tunnel' เลย — เช็คว่า docker-compose.yml ที่ "
                      f"{COMPOSE_FILE} มี service นี้อยู่ไหม แล้วลองรัน 'docker compose up -d' "
                      "เองในเทอร์มินัลดูว่ามี error อะไรไหม")
        elif not last_diag.get("running"):
            reason = ("container ของ service 'dashboard-tunnel' มีอยู่แต่ไม่ได้ทำงาน (อาจ crash/exit ซ้ำๆ) — "
                      "ตรวจ log ของ container dashboard-tunnel ใน Docker Desktop เพื่อดูสาเหตุ")
        else:
            reason = ("container ทำงานอยู่แต่ยังไม่เจอลิงก์ trycloudflare.com ใน log เลย — "
                      "อาจเชื่อมต่อ Cloudflare edge ไม่ได้ (เน็ตช้า/ไฟร์วอลล์บล็อก) "
                      "ตรวจ log ของ container dashboard-tunnel ใน Docker Desktop เพื่อดู log จริง")
        if last_diag.get("error"):
            reason += f" (error: {last_diag['error']})"

        self._safe_after(0, self._append_action_log, f"⚠️ หาลิงก์สาธารณะของ dashboard ไม่เจอ: {reason}", "warning")
        self._public_dashboard_url = None
        self._tunnel_status = "unavailable"
        self._safe_after(0, self._update_web_url_label)

    def _recheck_tunnel_url(self):
        """ปุ่ม '🔄 เปิด docker-compose ใหม่' — สั่ง docker-compose up -d + หาลิงก์สาธารณะใหม่ทั้งชุด
           โดยไม่ต้องปิดโปรแกรมแล้วเปิดใหม่ ใช้ได้ทั้งกรณี Docker Desktop เพิ่งพร้อม (ตอนแรกยังไม่ได้เปิด)
           หรือแค่อยากเช็คลิงก์อีกรอบเฉยๆ ก็ได้เหมือนกัน (ถ้า container รันอยู่แล้ว docker-compose up -d
           จะไม่ทำอะไรซ้ำ แค่ยืนยันว่าสถานะโอเค แล้วไปหาลิงก์ต่อทันที)"""
        self._append_action_log("⏳ กำลังลองเปิด docker-compose และหาลิงก์สาธารณะใหม่อีกครั้ง...")
        threading.Thread(target=self._compose_up_on_start, daemon=True).start()

    def _on_app_close(self):
        """เรียกตอนกด X ปิดหน้าต่าง — สั่ง docker-compose down ให้เรียบร้อยก่อน แล้วค่อยปิดแอปจริง
           ทำใน background thread กันหน้าต่างค้างระหว่างรอ docker ปิด container"""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._background_stop.set()
        self.backup_scheduler.stop()  # หยุดรอบ backup ถัดไป (backup ที่เขียนอยู่ตอนนี้ยังปล่อยให้เสร็จตามปกติ)
        self.ram_ai_manager.stop()    # หยุดรอบปรับ RAM อัตโนมัติถัดไป
        self.title("Intelligent Server Operations Platform — กำลังปิด Docker Compose รอสักครู่...")

        def _shutdown():
            try:
                # Do not let process exit interrupt an archive that is already being written.
                self.backup_scheduler.wait()
                if not self.ram_ai_manager.wait():
                    print("[shutdown] AI RAM worker is still stopping")
                for tstate in list(self._server_quick_tunnels.values()):
                    proc = tstate.get("proc")
                    if proc is not None:
                        try:
                            proc.terminate()
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        except Exception:
                            pass
                with self._compose_task_lock:
                    if os.path.exists(COMPOSE_FILE):
                        ok, msg = self._run_compose(["down"])
                        print(f"[docker-compose down] ok={ok} msg={msg}")
            except Exception as exc:
                print(f"[shutdown] {exc}")
            finally:
                self._safe_after(0, self.destroy)

        threading.Thread(target=_shutdown, daemon=True).start()

    # ══════════════════════════════════════════════════════════
    # ส่งลิงก์ Dashboard ทางอีเมล (ปุ่ม "✅ ตกลง" ในแท็บ Dashboard)
    # ══════════════════════════════════════════════════════════
    def _load_email_config(self) -> dict:
        default = {"sender_email": "", "sender_app_password": "",
                  "smtp_host": "smtp.gmail.com", "smtp_port": 465}
        if os.path.exists(self.email_config_file):
            try:
                with open(self.email_config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(default)
                merged.update(data)
                return merged
            except Exception:
                pass
        return default

    def _save_email_config(self):
        atomic_write_json(self.email_config_file, self._email_config)

    def _open_email_sender_settings(self):
        """หน้าต่างตั้งค่าอีเมลผู้ส่ง (ครั้งเดียวพอ) — ค่าเริ่มต้นรองรับ Gmail ผ่าน App Password"""
        win = ctk.CTkToplevel(self)
        win.title("⚙️ ตั้งค่าอีเมลผู้ส่ง")
        win.geometry("420x420")
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="⚙️ ตั้งค่าอีเมลผู้ส่ง",
                     font=("Segoe UI", 15, "bold")).pack(pady=(18, 4))
        ctk.CTkLabel(
            win,
            text="ค่าเริ่มต้นตั้งไว้สำหรับ Gmail — ต้องสร้าง 'App Password'\n"
                 "จาก Google Account ก่อน (ไม่ใช่รหัสผ่าน Gmail ปกติ เพราะ\n"
                 "Gmail ปิดการ login ด้วยรหัสผ่านตรงๆ ไปแล้ว)",
            font=("Segoe UI", 11), text_color=Theme.TEXT_MUTED, justify="left"
        ).pack(padx=20, pady=(0, 14))

        cfg = self._email_config

        ctk.CTkLabel(win, text="อีเมลผู้ส่ง (Gmail)", anchor="w").pack(fill="x", padx=20)
        sender_entry = ctk.CTkEntry(win, placeholder_text="you@gmail.com")
        sender_entry.insert(0, cfg.get("sender_email", ""))
        sender_entry.pack(fill="x", padx=20, pady=(2, 10))

        ctk.CTkLabel(win, text="App Password (16 หลัก)", anchor="w").pack(fill="x", padx=20)
        pass_entry = ctk.CTkEntry(win, placeholder_text="xxxx xxxx xxxx xxxx", show="•")
        pass_entry.insert(0, cfg.get("sender_app_password", ""))
        pass_entry.pack(fill="x", padx=20, pady=(2, 10))

        host_row = ctk.CTkFrame(win, fg_color="transparent")
        host_row.pack(fill="x", padx=20, pady=(2, 10))
        host_col = ctk.CTkFrame(host_row, fg_color="transparent")
        host_col.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkLabel(host_col, text="SMTP Host", anchor="w").pack(fill="x")
        host_entry = ctk.CTkEntry(host_col)
        host_entry.insert(0, cfg.get("smtp_host", "smtp.gmail.com"))
        host_entry.pack(fill="x", pady=(2, 0))

        port_col = ctk.CTkFrame(host_row, fg_color="transparent")
        port_col.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ctk.CTkLabel(port_col, text="SMTP Port (SSL)", anchor="w").pack(fill="x")
        port_entry = ctk.CTkEntry(port_col)
        port_entry.insert(0, str(cfg.get("smtp_port", 465)))
        port_entry.pack(fill="x", pady=(2, 0))

        def save():
            # ช่องว่างหมายถึงให้คงค่า sender ที่ตั้งไว้ ไม่ลบ credential ออกจาก config
            sender_value = sender_entry.get().strip()
            password_value = pass_entry.get().strip()
            self._email_config["sender_email"] = sender_value or cfg.get("sender_email", "")
            self._email_config["sender_app_password"] = password_value or cfg.get("sender_app_password", "")
            self._email_config["smtp_host"] = host_entry.get().strip() or "smtp.gmail.com"
            try:
                self._email_config["smtp_port"] = int(port_entry.get().strip())
            except ValueError:
                self._email_config["smtp_port"] = 465
            self._save_email_config()
            msgbox.showinfo("บันทึกแล้ว", "บันทึกการตั้งค่าอีเมลผู้ส่งเรียบร้อย")
            win.destroy()

        ctk.CTkButton(win, text="💾 บันทึก", fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER,
                      command=save).pack(pady=18)

    def _set_email_status(self, text: str, color: str):
        self.email_status_label.configure(text=text, text_color=color)

    def _send_dashboard_link(self, event=None):
        to_email = self.dashboard_email_entry.get().strip()
        if not to_email or "@" not in to_email or "." not in to_email.split("@")[-1]:
            self._set_email_status("❌ กรอกอีเมลให้ถูกต้องก่อน", Theme.DANGER)
            return
        if not self._email_config.get("sender_email") or not self._email_config.get("sender_app_password"):
            self._set_email_status("⚠️ ยังไม่ได้ตั้งค่าอีเมลผู้ส่ง — กด ⚙️ ก่อน", Theme.WARNING)
            return

        # ใช้ลิงก์สาธารณะ (เข้าได้จากทุกเครือข่าย) ก่อนเสมอถ้ามี — เข้าเหตุผลที่ผู้ใช้ขอเพิ่มมา
        # (router/เครื่องอื่นที่ไม่ได้อยู่ WiFi เดียวกันเข้าลิงก์วงแลนไม่ได้) ถ้า tunnel ยังไม่พร้อม
        # ค่อย fallback ไปใช้ลิงก์วงแลนแทนชั่วคราว
        url_text = getattr(self, "_public_dashboard_url", None) or getattr(self, "_lan_dashboard_url", None)
        if not url_text:
            self._set_email_status("⚠️ เว็บเซิร์ฟเวอร์ยังไม่พร้อม ลองใหม่อีกครั้ง", Theme.WARNING)
            return

        self._set_email_status("⏳ กำลังส่งอีเมล...", Theme.TEXT_MUTED)
        threading.Thread(target=self._send_dashboard_link_worker, args=(to_email, url_text), daemon=True).start()

    def _send_dashboard_link_worker(self, to_email: str, url: str):
        ok, err = self._smtp_send_link(to_email, url)
        if ok:
            self._safe_after(0, self._set_email_status, f"✅ ส่งลิงก์ไปที่ {to_email} แล้ว", Theme.SUCCESS)
            self._safe_after(0, self._append_action_log,
                      f"✅ ส่งลิงก์ dashboard ({url}) ไปที่ {to_email} แล้ว", "success")
        else:
            self._safe_after(0, self._set_email_status, f"❌ ส่งไม่สำเร็จ: {err}", Theme.DANGER)
            self._safe_after(0, self._append_action_log,
                      f"❌ ส่งลิงก์ dashboard ไปที่ {to_email} ไม่สำเร็จ: {err}", "danger")

    def _smtp_send_link(self, to_email: str, url: str) -> tuple[bool, str]:
        """ส่งอีเมลลิงก์ dashboard ผ่าน SMTP — ค่าเริ่มต้นตั้งไว้สำหรับ Gmail (ต้องใช้ App Password)"""
        cfg = self._email_config
        sender = cfg.get("sender_email", "")
        password = cfg.get("sender_app_password", "")
        host = cfg.get("smtp_host", "smtp.gmail.com")
        try:
            port = int(cfg.get("smtp_port", 465))
        except (TypeError, ValueError):
            port = 465

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🖥️ ลิงก์เข้าดู/คุมระบบ — Intelligent Server Operations Platform"
        msg["From"] = sender
        msg["To"] = to_email

        html = f"""\
<div style="background:#0a0a0d;color:#e5e7eb;padding:32px 20px;font-family:'Segoe UI',Tahoma,sans-serif;max-width:520px;margin:0 auto;border-radius:12px;">
  <h2 style="color:#38bdf8;margin:0 0 6px;">🖥️ Intelligent Server Operations Platform</h2>
  <p style="color:#9ca3af;font-size:13px;margin:0 0 24px;">ลิงก์เข้าดู/คุมระบบจากมือถือหรือเบราว์เซอร์</p>
  <div style="background:#151519;border:1px solid #26262f;border-radius:8px;padding:20px;">
    <p style="margin:0 0 16px;">กดปุ่มด้านล่างเพื่อเปิดหน้าควบคุม (ต้องอยู่ใน WiFi/เครือข่ายเดียวกับเครื่องนี้):</p>
    <p style="text-align:center;margin:24px 0;">
      <a href="{url}" style="background:#38bdf8;color:#0a0a0d;padding:12px 28px;border-radius:8px;
         text-decoration:none;font-weight:bold;display:inline-block;">เปิด Dashboard</a>
    </p>
    <p style="font-size:12px;color:#6b7280;word-break:break-all;">ลิงก์โดยตรง: <a href="{url}" style="color:#38bdf8;">{url}</a></p>
  </div>
  <p style="font-size:11px;color:#6b7280;margin-top:20px;text-align:center;">
    ส่งอัตโนมัติจาก Intelligent Server Operations Platform • {time.strftime('%Y-%m-%d %H:%M:%S')}
  </p>
</div>
"""
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                smtp.login(sender, password)
                smtp.sendmail(sender, [to_email], msg.as_string())
            return True, ""
        except smtplib.SMTPAuthenticationError:
            return False, "เข้าสู่ระบบอีเมลผู้ส่งไม่สำเร็จ (เช็ค App Password ใน ⚙️ ตั้งค่าอีเมลผู้ส่ง)"
        except Exception as e:
            return False, str(e)

    # ══════════════════════════════════════════════════════════
    # Resource Limits persistence
    # ══════════════════════════════════════════════════════════
    def _load_limits(self) -> dict:
        if os.path.exists(self.limits_file):
            try:
                with open(self.limits_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_limits(self):
        atomic_write_json(self.limits_file, self._resource_limits)

    def get_limit(self, name: str) -> dict:
        return self._resource_limits.get(name, dict(DEFAULT_LIMITS))

    # ══════════════════════════════════════════════════════════
    # 🤖 AI RAM ต่อ server (Server Manager) — เปิด = ปรับ RAM อัตโนมัติ, ปิด = Manual (ค่าเริ่มต้น)
    # ══════════════════════════════════════════════════════════
    def _set_ai_ram_enabled(self, name: str, enabled: bool):
        """สลับสวิตช์ 🤖 AI RAM ของ container นี้ — ไม่แตะค่า cpu_limit/ram_limit_mb เดิมที่ตั้งไว้
           (ถ้าเพิ่งปิด ค่า RAM ล่าสุดที่ AI ตั้งไว้จะกลายเป็นค่า Manual ต่อจากนี้จนกว่าจะเปิดใหม่)"""
        limits = dict(self.get_limit(name))
        limits["ai_ram_enabled"] = enabled
        self._resource_limits[name] = limits
        self._save_limits()

    def _set_auto_restart_unhealthy(self, name: str, enabled: bool):
        """Store an explicit per-container opt-in for health-check-based recovery."""
        limits = dict(self.get_limit(name))
        limits["auto_restart_unhealthy"] = bool(enabled)
        self._resource_limits[name] = limits
        self._save_limits()
        if not enabled:
            self._unhealthy_poll_streak[name] = 0

    def _get_container_usage_mb(self, name: str):
        """อ่าน RAM ที่ container ใช้จริงตอนนี้ (MB) จาก cache ของ docker stats ที่ poll อยู่แล้ว
           ทุก 3 วิ (ตัวเลขก่อนเครื่องหมาย '/' ใน MemUsage เช่น '31.09MiB / 1.953GiB' -> 31.09)
           คืน None ถ้า container offline หรืออ่านค่าไม่ได้ตอนนี้"""
        stats = self._docker_stats_cache.get(name)
        if not stats or not stats.get("online"):
            return None
        mem_usage = stats.get("mem_usage", "") or ""
        used_part = mem_usage.split("/")[0].strip() if "/" in mem_usage else mem_usage.strip()
        return parse_mem_to_mb(used_part)

    def _ai_apply_ram_limit(self, name: str, new_mb: int):
        """เรียกจาก RamAIManager เท่านั้น (background thread ของมันเอง ไม่ใช่ UI thread) —
           ปรับ ram_limit_mb ใหม่ + เซฟลง resource_limits.json + สั่ง docker update จริง
           ใช้ apply_resource_limits ตัวเดียวกับตอนบันทึกด้วยมือ กันสองที่ตรรกะไม่ตรงกัน"""
        limits = dict(self.get_limit(name))
        limits["ram_limit_mb"] = new_mb
        limits["ai_ram_enabled"] = True
        self._resource_limits[name] = limits
        self._save_limits()
        ok, msg = self.apply_resource_limits(name)
        self._safe_after(0, self._update_dash_limit_label, name)
        return ok, msg

    def _ram_ai_log(self, text: str, level: str = "info"):
        """callback ที่ RamAIManager เรียกจาก background thread — ต้องสลับกลับมา UI thread ก่อน"""
        self._safe_after(0, self._append_action_log, text, level)

    # ══════════════════════════════════════════════════════════
    # การตั้งค่าโปรแกรม (⚙️ ปุ่ม ☰ มุมขวาบน): RAM สูงสุด / ตำแหน่ง backup / เปิด-ปิดระบบ backup
    # ══════════════════════════════════════════════════════════
    _DEFAULT_PREFS = {
        "max_ram_mb": 4096,
        "backup_enabled": False,
        "backup_path": "",
        "ui_theme": "dark",   # "dark" | "gray" — ดู THEME_PALETTES ด้านบน
        "ui_scale": 1.0,      # ตัวคูณขนาด UI ทั้งโปรแกรม (100% = 1.0)
    }

    def _load_preferences(self) -> dict:
        merged = dict(self._DEFAULT_PREFS)
        if os.path.exists(self.preferences_file):
            try:
                with open(self.preferences_file, "r", encoding="utf-8") as f:
                    merged.update(json.load(f))
            except Exception:
                pass
        # Migrate a saved preference for the removed white theme to the default dark theme.
        if merged.get("ui_theme") not in ("dark", "gray"):
            merged["ui_theme"] = "dark"
        return merged

    def _save_preferences(self):
        atomic_write_json(self.preferences_file, self.prefs)

    # ══════════════════════════════════════════════════════════
    # ระบบ backup อัตโนมัติ
    # ══════════════════════════════════════════════════════════
    def _gather_backup_sources(self) -> list:
        """รวบรวม path โฟลเดอร์ของ "server" ที่ถูก import เข้าแท็บ Server Manager แล้วเท่านั้น
           (โฟลเดอร์ที่มี docker-compose.yml ของแต่ละ server วางอยู่ข้างใน จะได้ backup ทั้ง
           โฟลเดอร์ ไม่ใช่แค่ตัวไฟล์ .yml — เพราะข้อมูลจริงของ server เช่น app/, server.js,
           ไฟล์อื่นๆ อยู่ในโฟลเดอร์เดียวกันนี้ทั้งหมด)

           ไม่รวมไฟล์/โค้ดของตัวโปรแกรม Intelligent Server Operations Platform เอง (docker-compose.yml ของ
           ตัวแอป, ธีม, โลโก้, ไฟล์ตั้งค่าต่างๆ เช่น resource_limits.json/alert_thresholds.json)
           เพราะสิ่งเหล่านั้นไม่ใช่ "server" ที่ผู้ใช้ import เข้ามา เป็นแค่ไฟล์ของตัวโปรแกรมเอง
           (เรียกใหม่ทุกครั้งที่ backup เพราะรายการเปลี่ยนได้ระหว่างที่โปรแกรมรันอยู่)"""
        seen = set()
        result = []
        for fdir in self.yaml_paths.values():
            norm = os.path.normpath(fdir)
            if norm not in seen and os.path.isdir(norm):
                seen.add(norm)
                result.append(norm)
        return result

    def _backup_log(self, text: str, level: str = "info"):
        """callback ที่ BackupScheduler เรียกจาก background thread — ต้องสลับกลับมา UI thread ก่อน"""
        self._safe_after(0, self._append_action_log, text, level)

    def _save_tracked_containers(self):
        """เซฟรายชื่อ container ที่ถูก Import เข้าแท็บ Server Manager แล้วลง JSON —
           server.py (Flask) จะอ่านไฟล์นี้เพื่อกรอง /api/containers ให้เว็บ dashboard
           โชว์เฉพาะ server ที่ import เข้าระบบแล้วเท่านั้น ไม่ใช่ container ทุกตัวที่รันอยู่บนเครื่อง"""
        try:
            atomic_write_json(self.tracked_containers_file, sorted(self.tracked_containers.keys()))
        except Exception as e:
            print(f"[tracked_containers] เซฟไฟล์ไม่สำเร็จ: {e}")

    # ══════════════════════════════════════════════════════════
    # Alert Thresholds persistence (ใช้กับคำสั่ง /ral ในแท็บ Action Log)
    # ══════════════════════════════════════════════════════════
    def _load_alerts(self) -> dict:
        if os.path.exists(self.alerts_file):
            try:
                with open(self.alerts_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_alerts(self):
        atomic_write_json(self.alerts_file, self._alert_thresholds)

    def get_alert(self, name: str) -> dict:
        merged = dict(DEFAULT_ALERT)
        merged.update(self._alert_thresholds.get(name, {}))
        return merged

    # ══════════════════════════════════════════════════════════
    # Setup UI
    # ══════════════════════════════════════════════════════════
    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)

        # ── แถบบนสุด: ปุ่มเมนู ☰ มุมขวาบน — เปิดหน้า ⚙️ ตั้งค่า (RAM สูงสุด / Backup / About) ──
        topbar = ctk.CTkFrame(self, fg_color="transparent")
        topbar.grid(row=0, column=0, padx=20, pady=(10, 4), sticky="ew")
        topbar.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            topbar, text="☰", width=42, height=38,
            font=("Segoe UI", 16, "bold"),
            fg_color=Theme.BG_CARD, hover_color=Theme.NEUTRAL_DARK_HOVER,
            text_color=Theme.TEXT_PRIMARY, corner_radius=9,
            command=self._open_settings_window,
        ).grid(row=0, column=1, sticky="e")

        self.tabview = ctk.CTkTabview(
            self, width=1100, height=700,
            fg_color=Theme.BG_PANEL,
        )
        self.tabview.grid(row=1, column=0, padx=20, pady=(0, 18), sticky="nsew")

        # ปรับฟอนต์ของแถบแท็บให้หนาขึ้นแบบ optional — บาง version ของ customtkinter
        # ไม่รองรับ segmented_button_font ตอนสร้าง CTkTabview เลยตั้งทีหลังแทน กัน error ไว้เผื่อ
        # internal attribute เปลี่ยนชื่อไปในบาง version
        try:
            self.tabview._segmented_button.configure(font=("Segoe UI", 13, "bold"))
        except Exception:
            pass

        self.tab_dashboard        = self.tabview.add("📊 Dashboard")
        self.tab_history          = self.tabview.add("📈 Historical graph")
        self.tab_vertical_chart  = self.tabview.add("📊 Vertical chart")
        self.tab_server_dashboard = self.tabview.add("📡 Server Dashboard")
        self.tab_servers          = self.tabview.add("🖥️ Server Manager")
        self.tab_terminal         = self.tabview.add("💻 Terminal")
        self.tab_log              = self.tabview.add("📋 Action Log")

        self.build_dashboard()
        self.build_history_graph_tab()
        self.build_vertical_chart_tab()
        self.build_server_dashboard()
        self.build_server_manager()
        self.build_terminal()
        self.build_log_tab()

    # ══════════════════════════════════════════════════════════
    # หน้า ⚙️ ตั้งค่า (เปิดจากปุ่ม ☰ มุมขวาบน)
    # ══════════════════════════════════════════════════════════
    def _open_settings_window(self):
        win = ctk.CTkToplevel(self)
        win.title("⚙️ ตั้งค่า")
        # Keep the window within the usable screen area. At 200% UI scaling the
        # old fixed 900px height could place its lower controls off-screen.
        scale = max(float(self.prefs.get("ui_scale", 1.0)), 0.75)
        screen_w = max(self.winfo_screenwidth() - 80, 360)
        screen_h = max(self.winfo_screenheight() - 100, 420)
        width = min(460, int(screen_w / scale))
        height = min(900, int(screen_h / scale))
        win.geometry(f"{width}x{height}")
        win.minsize(min(360, width), min(420, height))
        win.configure(fg_color=Theme.BG_ROOT)
        win.transient(self)
        win.grab_set()

        # Settings have more content than fits on short screens or at large UI
        # scales, so keep the controls in a scrollable viewport.
        content = ctk.CTkScrollableFrame(win, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=8, pady=8)
        win = content

        ctk.CTkLabel(win, text="⚙️ ตั้งค่าโปรแกรม",
                     font=("Segoe UI", 16, "bold")).pack(pady=(20, 16))

        # ── RAM สูงสุด ──
        ctk.CTkLabel(win, text="RAM สูงสุดที่อนุญาตให้ใช้ (MB)", anchor="w",
                     font=("Segoe UI", 12, "bold")).pack(fill="x", padx=24)
        ram_entry = ctk.CTkEntry(win, placeholder_text="เช่น 4096")
        ram_entry.insert(0, str(self.prefs.get("max_ram_mb", 4096)))
        ram_entry.pack(fill="x", padx=24, pady=(4, 18))

        # ── ตำแหน่ง Backup ──
        ctk.CTkLabel(win, text="ตำแหน่งจัดเก็บไฟล์ Backup", anchor="w",
                     font=("Segoe UI", 12, "bold")).pack(fill="x", padx=24)

        current_path = self.prefs.get("backup_path") or ""
        path_row = ctk.CTkFrame(win, fg_color="transparent")
        path_row.pack(fill="x", padx=24, pady=(4, 2))
        path_row.grid_columnconfigure(0, weight=1)

        path_label = ctk.CTkLabel(
            path_row, text=current_path or "ยังไม่ได้เลือก...",
            text_color=(Theme.TEXT_PRIMARY if current_path else Theme.TEXT_MUTED),
            anchor="w", wraplength=280, justify="left",
        )
        path_label.grid(row=0, column=0, sticky="w")

        def choose_path():
            folder = filedialog.askdirectory(title="เลือกโฟลเดอร์/ไดรฟ์สำหรับเก็บ Backup")
            if folder:
                path_label.configure(text=folder, text_color=Theme.TEXT_PRIMARY)

        ctk.CTkButton(path_row, text="📁 เลือกโฟลเดอร์", width=120,
                      command=choose_path).grid(row=0, column=1, padx=(8, 0))

        ctk.CTkLabel(
            win,
            text="เมื่อเปิดใช้งาน โปรแกรมจะสำรองข้อมูลของ Server Manager ทั้งหมด\n"
                 "(YAML ที่ import ไว้ + การตั้งค่าต่างๆ) ไปที่โฟลเดอร์นี้ทันทีตอนเปิด\n"
                 "เซิร์ฟเวอร์ และลบของเก่า/สำรองใหม่ซ้ำทุก 1 ชั่วโมงโดยอัตโนมัติ",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, justify="left",
        ).pack(fill="x", padx=24, pady=(4, 16))

        # ── เปิด/ปิดระบบ backup ──
        backup_switch_var = ctk.BooleanVar(value=bool(self.prefs.get("backup_enabled", False)))
        ctk.CTkSwitch(
            win, text="เปิดใช้งานระบบ Backup อัตโนมัติ",
            variable=backup_switch_var, onvalue=True, offvalue=False,
        ).pack(anchor="w", padx=24, pady=(0, 18))

        status_label = ctk.CTkLabel(win, text="", font=("Segoe UI", 11))
        status_label.pack(pady=(0, 4))

        def save():
            raw_ram = ram_entry.get().strip()
            try:
                ram_val = int(raw_ram)
                if ram_val <= 0:
                    raise ValueError
            except ValueError:
                status_label.configure(text="❌ RAM สูงสุดต้องเป็นตัวเลขจำนวนเต็มมากกว่า 0",
                                        text_color=Theme.DANGER)
                return

            new_path = path_label.cget("text")
            if new_path == "ยังไม่ได้เลือก...":
                new_path = ""
            new_enabled = backup_switch_var.get()

            if new_enabled and not new_path:
                status_label.configure(text="⚠️ กรุณาเลือกตำแหน่ง Backup ก่อนเปิดใช้งาน",
                                        text_color=Theme.WARNING)
                return

            self.prefs["max_ram_mb"] = ram_val
            self.prefs["backup_path"] = new_path
            self.prefs["backup_enabled"] = new_enabled
            self._save_preferences()

            if new_enabled:
                if not self.backup_scheduler.is_running():
                    self.backup_scheduler.start()
                    self._append_action_log("✅ เปิดใช้งานระบบ Backup อัตโนมัติแล้ว — กำลังสำรองข้อมูลรอบแรก...", "success")
                else:
                    self._append_action_log("🔄 อัปเดตการตั้งค่า Backup แล้ว", "info")
            else:
                if self.backup_scheduler.is_running():
                    self.backup_scheduler.stop()
                    self._append_action_log("⏹️ ปิดใช้งานระบบ Backup อัตโนมัติแล้ว", "warning")

            status_label.configure(text="✅ บันทึกการตั้งค่าแล้ว", text_color=Theme.SUCCESS)

        ctk.CTkButton(win, text="💾 บันทึกการตั้งค่า", fg_color=Theme.SUCCESS,
                      hover_color=Theme.SUCCESS_HOVER, command=save).pack(pady=(2, 22))

        # ══════════════════════════════════════════════════════
        # 🎨 ธีมสี และขนาด UI
        # ══════════════════════════════════════════════════════
        ctk.CTkLabel(win, text="🎨 ธีมสี", anchor="w",
                     font=("Segoe UI", 12, "bold")).pack(fill="x", padx=24)

        theme_row = ctk.CTkFrame(win, fg_color="transparent")
        theme_row.pack(fill="x", padx=24, pady=(6, 4))

        _THEME_CHOICES = [("dark", "⚫ ดำ"), ("gray", "◾ เทา")]
        _theme_buttons: dict = {}
        theme_status_label = ctk.CTkLabel(win, text="", font=("Segoe UI", 11), justify="left")

        def _refresh_theme_buttons(active_name):
            for key, btn in _theme_buttons.items():
                if key == active_name:
                    btn.configure(fg_color=Theme.ACCENT, hover_color=Theme.ACCENT_HOVER,
                                  text_color="#0a0a0d")
                else:
                    btn.configure(fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                                  text_color=Theme.TEXT_PRIMARY)

        def _pick_theme(theme_name):
            old_name = self.prefs.get("ui_theme", "dark")
            if theme_name == old_name:
                return

            theme_status_label.configure(text="⏳ กำลังเปลี่ยนธีม...", text_color=Theme.TEXT_MUTED)

            def _finish_theme_change():
                _refresh_theme_buttons(theme_name)
                theme_status_label.configure(
                    text=f"✅ เปลี่ยนเป็นธีม{'เทา' if theme_name == 'gray' else 'ดำ'}แล้ว",
                    text_color=Theme.SUCCESS)

            try:
                _apply_theme_to_widget_tree(self, old_name, theme_name,
                                            on_complete=_finish_theme_change)
            except Exception as exc:
                theme_status_label.configure(
                    text=f"❌ เปลี่ยนธีมไม่สำเร็จ: {exc}", text_color=Theme.DANGER)
                return

            self.prefs["ui_theme"] = theme_name
            try:
                self._save_preferences()
            except Exception as exc:
                theme_status_label.configure(
                    text=f"⚠️ เปลี่ยนธีมแล้ว แต่บันทึกค่าไม่สำเร็จ: {exc}",
                    text_color=Theme.WARNING)
                _refresh_theme_buttons(theme_name)
                return
            _refresh_theme_buttons(theme_name)

        for key, label in _THEME_CHOICES:
            b = ctk.CTkButton(theme_row, text=label, width=110,
                              command=lambda k=key: _pick_theme(k))
            b.pack(side="left", padx=(0, 8))
            _theme_buttons[key] = b
        _refresh_theme_buttons(self.prefs.get("ui_theme", "dark"))

        theme_status_label.pack(fill="x", padx=24, pady=(2, 2))

        ctk.CTkLabel(
            win,
            text="เลือกธีมใหม่แล้วสีจะเปลี่ยนทันที (เลือกได้ระหว่างธีมดำและธีมเทา)",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, justify="left"
        ).pack(fill="x", padx=24, pady=(0, 16))

        # ── ขนาด UI (สเกลตัวอักษร/widget ทั้งโปรแกรม) ──
        ctk.CTkLabel(win, text="🔍 ขนาด UI (เผื่อนำเสนอ/ต่อจอโปรเจกเตอร์)", anchor="w",
                     font=("Segoe UI", 12, "bold")).pack(fill="x", padx=24)

        scale_row = ctk.CTkFrame(win, fg_color="transparent")
        scale_row.pack(fill="x", padx=24, pady=(6, 2))

        _SCALE_CHOICES = {"100%": 1.0, "110%": 1.1, "125%": 1.25,
                          "150%": 1.5, "175%": 1.75, "200%": 2.0}
        _current_scale = float(self.prefs.get("ui_scale", 1.0))
        _current_scale_label = min(
            _SCALE_CHOICES, key=lambda k: abs(_SCALE_CHOICES[k] - _current_scale))

        def _pick_scale(choice_str):
            value = _SCALE_CHOICES.get(choice_str, 1.0)
            ctk.set_widget_scaling(value)   # ใช้ผลทันที ไม่ต้องรีสตาร์ท
            ctk.set_window_scaling(value)
            self.prefs["ui_scale"] = value
            self._save_preferences()

        scale_menu = ctk.CTkOptionMenu(scale_row, values=list(_SCALE_CHOICES.keys()),
                                       command=_pick_scale, width=120)
        scale_menu.set(_current_scale_label)
        scale_menu.pack(side="left")

        ctk.CTkLabel(
            win, text="ปรับได้ทันทีโดยไม่ต้องรีสตาร์ทโปรแกรม — มีผลกับทุกหน้าต่างของโปรแกรมนี้",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, justify="left"
        ).pack(fill="x", padx=24, pady=(4, 18))

        # ── เกี่ยวกับ ──
        about = ctk.CTkFrame(win, fg_color=Theme.BG_CARD)
        about.pack(fill="x", padx=24, pady=(0, 20))
        ctk.CTkLabel(about, text="ℹ️ เกี่ยวกับโปรแกรม", font=("Segoe UI", 12, "bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(about, text="Intelligent Server Operations Platform", font=("Segoe UI", 11),
                     text_color=Theme.TEXT_SECONDARY).pack(anchor="w", padx=14)
        ctk.CTkLabel(about, text="ระบบปฏิบัติการและบริหารจัดการเครื่องแม่ข่าย", font=("Segoe UI", 11),
                     text_color=Theme.TEXT_SECONDARY).pack(anchor="w", padx=14)
        ctk.CTkLabel(about, text="ระบบปฏิบัติการและบริหารจัดการเครื่องแม่ข่าย", font=("Segoe UI", 11),
                     text_color=Theme.TEXT_SECONDARY).pack(anchor="w", padx=14)
        ctk.CTkLabel(about, text=f"เวอร์ชัน {APP_VERSION}", font=("Segoe UI", 11),
                     text_color=Theme.TEXT_MUTED).pack(anchor="w", padx=14, pady=(0, 12))

    # ══════════════════════════════════════════════════════════
    # แท็บ 1: Dashboard
    # ══════════════════════════════════════════════════════════
    def build_dashboard(self):
        tab = self.tab_dashboard
        tab.grid_columnconfigure((0, 1, 2), weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # ─── Metric Cards (row 0) ────────────────────────────
        def metric_card(col, label_text, bar_color, value_attr, bar_attr):
            frame = ctk.CTkFrame(tab, border_width=1, border_color=Theme.BORDER)
            frame.grid(row=0, column=col, padx=8, pady=8, sticky="nsew")
            lbl = ctk.CTkLabel(frame, text=label_text, font=("Segoe UI", 15, "bold"))
            lbl.pack(pady=(14, 4))
            val = ctk.CTkLabel(frame, text="0%", font=("Segoe UI", 26, "bold"), text_color=bar_color)
            val.pack()
            bar = ctk.CTkProgressBar(frame, progress_color=bar_color, height=14)
            bar.set(0)
            bar.pack(padx=20, pady=(6, 14), fill="x")
            hint = ctk.CTkLabel(frame, text="คลิกเพื่อดูรายละเอียดและ process",
                                font=("Segoe UI", 9), text_color=Theme.TEXT_MUTED)
            hint.pack(pady=(0, 8))
            setattr(self, value_attr, val)
            setattr(self, bar_attr, bar)
            setattr(self, f"{value_attr}_frame", frame)
            setattr(self, f"{value_attr}_hint", hint)

        metric_card(0, "🖥️  CPU",  Theme.ACCENT, "cpu_label",  "cpu_bar")
        metric_card(1, "🧠  RAM",  Theme.SUCCESS, "ram_label",  "ram_bar")
        metric_card(2, "💾  Disk", Theme.WARNING, "disk_label", "disk_bar")

        # เปิดรายละเอียดการใช้งานและ process ทั้งหมดเมื่อคลิกการ์ด
        for metric, widget_names in (
            ("cpu", ("cpu_label_frame", "cpu_label", "cpu_bar", "cpu_label_hint")),
            ("ram", ("ram_label_frame", "ram_label", "ram_bar", "ram_label_hint")),
            ("disk", ("disk_label_frame", "disk_label", "disk_bar", "disk_label_hint")),
        ):
            for widget_name in widget_names:
                widget = getattr(self, widget_name)
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _event, key=metric: self._open_resource_details(key))

        # ─── Risk Score (row 1) ─────────────────────────────
        risk_frame = ctk.CTkFrame(tab, fg_color=Theme.BG_CARD_ALT, border_width=1, border_color=Theme.BORDER)
        risk_frame.grid(row=1, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
        self.risk_label = ctk.CTkLabel(risk_frame, text="⚠️ Risk Score: —", font=("Segoe UI", 13))
        self.risk_label.pack(side="left", padx=16, pady=6)
        self.risk_bar = ctk.CTkProgressBar(risk_frame, progress_color=Theme.DANGER, height=10, width=400)
        self.risk_bar.set(0)
        self.risk_bar.pack(side="left", padx=10, pady=6)

        # ─── AI Status Frame (row 2) ─────────────────────────
        self.ai_status_frame = ctk.CTkFrame(tab, fg_color=Theme.ACCENT_BG)
        self.ai_status_frame.grid(row=2, column=0, columnspan=3, padx=8, pady=8, sticky="nsew")

        trend_header = ctk.CTkFrame(self.ai_status_frame, fg_color="transparent")
        trend_header.pack(fill="x", padx=14, pady=(10, 0))
        self.trend_label = ctk.CTkLabel(
            trend_header, text="📈 แนวโน้ม CPU: กำลังรวบรวมข้อมูล",
            font=("Segoe UI", 13, "bold"), text_color=Theme.ACCENT, anchor="w"
        )
        self.trend_label.pack(side="left", anchor="w")
        self.stability_label = ctk.CTkLabel(
            trend_header, text="เสถียรภาพ —/100",
            font=("Segoe UI", 13, "bold"), text_color=Theme.TEXT_PRIMARY, anchor="e"
        )
        self.stability_label.pack(side="right", anchor="e")

        self.ai_status_label = ctk.CTkLabel(
            self.ai_status_frame, text="✅ ระบบเฝ้าระวังพร้อมใช้งาน",
            font=("Segoe UI", 13), text_color=Theme.TEXT_ACCENT
        )
        self.ai_status_label.pack(pady=(4, 0))
        self.forecast_label = ctk.CTkLabel(
            self.ai_status_frame, text="เส้นทึบ: ข้อมูลจริง   เส้นประ: คาดการณ์ประมาณ 25 วินาที",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED
        )
        self.forecast_label.pack(pady=(0, 2))
        self.lstm_status_label = ctk.CTkLabel(
            self.ai_status_frame, text="🧠 LSTM: กำลังเตรียมข้อมูลตามลำดับเวลา",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, anchor="w")
        self.lstm_status_label.pack(fill="x", padx=14, pady=(2, 0))
        self.lstm_train_button = ctk.CTkButton(
            self.ai_status_frame, text="ฝึก LSTM ใหม่จากข้อมูลย้อนหลัง 30 วัน", width=240,
            height=26, fg_color=Theme.NEUTRAL,
            command=lambda: self._prepare_lstm_model(force=True))
        self.lstm_train_button.pack(anchor="e", padx=12, pady=(0, 2))
        self.healing_label = ctk.CTkLabel(
            self.ai_status_frame, text="", font=("Segoe UI", 12, "bold"), text_color=Theme.TEXT_ACCENT
        )
        self.healing_label.pack(pady=(0, 2))

        self.dashboard_trend_canvas = tk.Canvas(
            self.ai_status_frame, bg=Theme.BG_PANEL, height=150, highlightthickness=0
        )
        self.dashboard_trend_canvas.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self.dashboard_trend_canvas.bind(
            "<Configure>", lambda _event: self._schedule_canvas_redraw(
                "dashboard_trend", self._draw_dashboard_trend_graph))
        # ─── Mini graph canvas (row 3) ───────────────────────
        graph_outer = ctk.CTkFrame(tab, fg_color=Theme.BG_PANEL, border_width=1, border_color=Theme.BORDER)
        graph_outer.grid(row=3, column=0, columnspan=3, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(graph_outer, text="📉 Usage History (60 samples)",
                     font=("Segoe UI", 12), text_color=Theme.TEXT_MUTED).pack(anchor="w", padx=10, pady=(6, 0))
        self.graph_canvas = tk.Canvas(graph_outer, bg=Theme.BG_PANEL, height=90, highlightthickness=0)
        self.graph_canvas.pack(fill="x", padx=10, pady=(0, 8))
        tab.grid_rowconfigure(3, weight=0)

        # ─── การแจ้งเตือน + ดูจากมือถือ (row 4) ──────────────
        settings_row = ctk.CTkFrame(tab, fg_color="transparent")
        settings_row.grid(row=4, column=0, columnspan=3, padx=8, pady=(0, 8), sticky="ew")
        settings_row.grid_columnconfigure((0, 1), weight=1)

        notif_frame = ctk.CTkFrame(settings_row, border_width=1, border_color=Theme.BORDER)
        notif_frame.grid(row=0, column=0, padx=(0, 4), sticky="nsew")

        ctk.CTkLabel(notif_frame, text="🔔 การแจ้งเตือนเมื่อพบ Anomaly",
                     font=("Segoe UI", 14, "bold")).pack(pady=(10, 6))

        cfg = self.notifier.config

        row1 = ctk.CTkFrame(notif_frame, fg_color="transparent")
        row1.pack(fill="x", padx=14, pady=(4, 12))
        self.popup_switch = ctk.CTkSwitch(row1, text="แจ้งเตือนแบบ Popup บนเครื่องนี้",
                                          command=self.save_notifier_settings)
        if cfg.get("popup_enabled", True):
            self.popup_switch.select()
        self.popup_switch.pack(side="left")

        web_frame = ctk.CTkFrame(settings_row, border_width=1, border_color=Theme.BORDER)
        web_frame.grid(row=0, column=1, padx=(4, 0), sticky="nsew")

        ctk.CTkLabel(web_frame, text="📱 ดูสถานะ/คุมระบบจากมือถือ / เว็บเบราว์เซอร์",
                     font=("Segoe UI", 14, "bold")).pack(pady=(10, 6))

        url_row = ctk.CTkFrame(web_frame, fg_color="transparent")
        url_row.pack(fill="x", padx=14, pady=(0, 4))
        self.web_url_label = ctk.CTkLabel(
            url_row, text="⏳ กำลังเปิดเว็บเซิร์ฟเวอร์...",
            font=("Consolas", 13), text_color=Theme.TEXT_ACCENT, justify="left", anchor="w")
        self.web_url_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(url_row, text="🔄 เปิด docker-compose ใหม่", width=170, fg_color=Theme.NEUTRAL,
                      hover_color=Theme.NEUTRAL_HOVER, command=self._recheck_tunnel_url
                      ).pack(side="right", anchor="n")

        ctk.CTkLabel(
            web_frame,
            text="ลิงก์ 🏠 ใช้ได้เฉพาะในเครือข่าย WiFi/แลนเดียวกัน\n"
                 "ลิงก์ 🌍 เข้าได้จากทุกที่/ทุก router (ผ่าน Cloudflare tunnel) — จะเปลี่ยนใหม่\n"
                 "ทุกครั้งที่เปิดโปรแกรม ต้องรอสักครู่หลังเปิดโปรแกรมให้ tunnel เชื่อมต่อเสร็จก่อน "
                 "(ครั้งแรกอาจช้าเพราะต้องโหลด image) ถ้ารอนานแล้วไม่ขึ้น กด 🔄 เปิด docker-compose ใหม่",
            font=("Segoe UI", 11), text_color=Theme.TEXT_MUTED, justify="left").pack(pady=(0, 10), padx=14, anchor="w")

        # ── กรอกอีเมล + ปุ่มตกลง เพื่อส่งลิงก์ข้างบนไปให้ผู้ใช้ทางอีเมล ──
        email_row = ctk.CTkFrame(web_frame, fg_color="transparent")
        email_row.pack(fill="x", padx=14, pady=(0, 6))
        self.dashboard_email_entry = ctk.CTkEntry(email_row, placeholder_text="กรอกอีเมลผู้รับ เช่น you@gmail.com")
        self.dashboard_email_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.dashboard_email_entry.bind("<Return>", self._send_dashboard_link)
        self._setup_context_menu(self.dashboard_email_entry._entry, is_entry=True)
        ctk.CTkButton(email_row, text="✅ ตกลง", width=70, fg_color=Theme.SUCCESS,
                      hover_color=Theme.SUCCESS_HOVER,
                      command=self._send_dashboard_link).pack(side="left")

        bottom_row = ctk.CTkFrame(web_frame, fg_color="transparent")
        bottom_row.pack(fill="x", padx=14, pady=(0, 12))
        self.email_status_label = ctk.CTkLabel(bottom_row, text="", font=("Segoe UI", 11),
                                               text_color=Theme.TEXT_MUTED, justify="left", anchor="w")
        self.email_status_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(bottom_row, text="⚙️ ตั้งค่าอีเมลผู้ส่ง", width=150,
                      fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                      command=self._open_email_sender_settings).pack(side="right")

    def _open_resource_details(self, metric):
        """Show live host details and a searchable list of every accessible process."""
        titles = {"cpu": "CPU Performance", "ram": "RAM Performance", "disk": "Disk Performance"}
        win = ctk.CTkToplevel(self)
        win.title(titles.get(metric, "System Performance"))
        win.geometry("1050x690")
        win.minsize(820, 520)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text=titles.get(metric, "System Performance"),
                     font=("Segoe UI", 19, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
        summary = ctk.CTkLabel(win, text="กำลังอ่านสถานะระบบ...", anchor="w", justify="left",
                               font=("Segoe UI", 12), text_color=Theme.TEXT_SECONDARY)
        summary.pack(fill="x", padx=18, pady=(0, 12))

        tools = ctk.CTkFrame(win, fg_color="transparent")
        tools.pack(fill="x", padx=18, pady=(0, 8))
        search = ctk.CTkEntry(tools, placeholder_text="ค้นหาชื่อ process หรือ PID", width=300)
        search.pack(side="left")
        count_label = ctk.CTkLabel(tools, text="", text_color=Theme.TEXT_MUTED)
        count_label.pack(side="left", padx=12)
        ctk.CTkButton(tools, text="รีเฟรช", width=90,
                      command=lambda: refresh()).pack(side="right")

        table_frame = ctk.CTkFrame(win, fg_color=Theme.BG_PANEL)
        table_frame.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        columns = ("pid", "name", "cpu", "ram", "status", "threads", "read", "write")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=20)
        headings = {"pid": "PID", "name": "Process", "cpu": "CPU %", "ram": "RAM MB",
                    "status": "Status", "threads": "Threads", "read": "Read MB",
                    "write": "Write MB"}
        widths = {"pid": 75, "name": 250, "cpu": 90, "ram": 100,
                  "status": 120, "threads": 80, "read": 110, "write": 110}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=60,
                        anchor="w" if column in ("name", "status") else "e",
                        stretch=(column == "name"))
        style = ttk.Style(win)
        # Windows' native ttk theme paints heading cells white and ignores the
        # configured dark palette.  Clam honors explicit colors so the labels
        # stay readable in both supported app themes.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Resource.Treeview", background=Theme.BG_PANEL, foreground=Theme.TEXT_PRIMARY,
                        fieldbackground=Theme.BG_PANEL, rowheight=25, font=("Segoe UI", 10))
        style.configure("Resource.Treeview.Heading", background=Theme.BG_CARD_ALT,
                        foreground=Theme.TEXT_PRIMARY, font=("Segoe UI", 10, "bold"),
                        bordercolor=Theme.BORDER, relief="flat")
        style.map("Resource.Treeview.Heading",
                  background=[("active", Theme.ACCENT_BG)],
                  foreground=[("active", Theme.TEXT_PRIMARY)])
        style.map("Resource.Treeview", background=[("selected", "#225c67")],
                  foreground=[("selected", Theme.TEXT_PRIMARY)])
        tree.configure(style="Resource.Treeview")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        yscroll.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=(8, 0))
        xscroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        process_rows = []
        closed = [False]

        def format_bytes(value):
            return f"{value / (1024 ** 2):.1f}" if value is not None else "—"

        def refresh():
            if closed[0] or not win.winfo_exists():
                return
            try:
                cpu_total = psutil.cpu_percent(interval=None)
                per_cpu = psutil.cpu_percent(interval=None, percpu=True)
                memory = psutil.virtual_memory()
                swap = psutil.swap_memory()
                if metric == "cpu":
                    freq = psutil.cpu_freq()
                    freq_text = f" · ความถี่ {freq.current:.0f} MHz" if freq else ""
                    details = (f"ใช้งานรวม {cpu_total:.1f}% · {psutil.cpu_count(logical=False) or '—'} physical / "
                               f"{psutil.cpu_count(logical=True) or '—'} logical cores{freq_text}\n"
                               "การใช้งานราย core: " + "  |  ".join(
                                   f"Core {index + 1} {value:.0f}%" for index, value in enumerate(per_cpu)))
                elif metric == "ram":
                    details = (f"RAM รวม {memory.total / 1024**3:.2f} GiB · ใช้ {memory.used / 1024**3:.2f} GiB "
                               f"({memory.percent:.1f}%) · ว่าง {memory.available / 1024**3:.2f} GiB\n"
                               f"Swap รวม {swap.total / 1024**3:.2f} GiB · ใช้ {swap.used / 1024**3:.2f} GiB "
                               f"({swap.percent:.1f}%)")
                else:
                    partitions = []
                    for part in psutil.disk_partitions(all=False):
                        try:
                            usage = psutil.disk_usage(part.mountpoint)
                            partitions.append(
                                f"{part.device or part.mountpoint}: {usage.used / 1024**3:.1f}/"
                                f"{usage.total / 1024**3:.1f} GiB ({usage.percent:.1f}%)")
                        except (OSError, PermissionError):
                            continue
                    io = psutil.disk_io_counters()
                    io_text = (f"\nDisk I/O สะสม: อ่าน {io.read_bytes / 1024**3:.2f} GiB · "
                               f"เขียน {io.write_bytes / 1024**3:.2f} GiB" if io else "")
                    details = "พื้นที่รายไดรฟ์: " + (" · ".join(partitions) or "อ่านข้อมูลไม่ได้") + io_text
                summary.configure(text=details)
            except Exception as exc:
                summary.configure(text=f"อ่านข้อมูลระบบไม่สำเร็จ: {exc}")

            updated = []
            for proc in psutil.process_iter():
                try:
                    with proc.oneshot():
                        pid = proc.pid
                        name = proc.name() or "(unknown)"
                        cpu_pct = proc.cpu_percent(interval=None)
                        rss = proc.memory_info().rss
                        status = proc.status()
                        threads = proc.num_threads()
                        try:
                            io = proc.io_counters()
                            read_bytes, write_bytes = io.read_bytes, io.write_bytes
                        except (psutil.AccessDenied, AttributeError, OSError):
                            read_bytes = write_bytes = None
                    updated.append({"pid": pid, "name": name, "cpu": cpu_pct, "rss": rss,
                                    "status": status, "threads": threads,
                                    "read": read_bytes, "write": write_bytes})
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    continue
            process_rows[:] = updated
            render_rows()
            if not closed[0] and win.winfo_exists():
                win.after(2500, refresh)

        def render_rows():
            query = search.get().strip().casefold()
            if metric == "cpu":
                ordered = sorted(process_rows, key=lambda row: row["cpu"], reverse=True)
            elif metric == "ram":
                ordered = sorted(process_rows, key=lambda row: row["rss"], reverse=True)
            else:
                ordered = sorted(process_rows,
                                 key=lambda row: (row["read"] or 0) + (row["write"] or 0), reverse=True)
            matches = [row for row in ordered if not query or query in row["name"].casefold()
                       or query in str(row["pid"])]
            tree.delete(*tree.get_children())
            for row in matches:
                tree.insert("", "end", values=(row["pid"], row["name"], f"{row['cpu']:.1f}",
                           f"{row['rss'] / 1024**2:.1f}", row["status"], row["threads"],
                           format_bytes(row["read"]), format_bytes(row["write"])))
            count_label.configure(text=f"แสดง {len(matches):,}/{len(process_rows):,} processes · เรียงตาม {metric.upper()} ")

        search.bind("<KeyRelease>", lambda _event: render_rows())

        def close_window():
            closed[0] = True
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_window)
        refresh()

    def save_notifier_settings(self):
        self.notifier.update_config(popup_enabled=bool(self.popup_switch.get()))

    def _draw_graph(self):
        """วาด mini-graph สามเส้น (CPU/RAM/Disk) บน canvas"""
        c = self.graph_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return

        def draw_line(hist, color):
            if len(hist) < 2:
                return
            pts = []
            for i, v in enumerate(hist):
                x = int(i / (len(hist) - 1) * (w - 4)) + 2
                y = int(h - 4 - (v / 100.0) * (h - 8))
                pts.extend([x, y])
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=1.5, smooth=True)

        # grid lines at 50% and 90%
        for pct in (50, 90):
            y = int(h - 4 - (pct / 100.0) * (h - 8))
            c.create_line(0, y, w, y, fill=Theme.BORDER, dash=(2, 4))

        draw_line(self._cpu_hist,  Theme.ACCENT)
        draw_line(self._ram_hist,  Theme.SUCCESS)
        draw_line(self._disk_hist, Theme.WARNING)

        # legend
        for i, (label, color) in enumerate([("CPU", Theme.ACCENT), ("RAM", Theme.SUCCESS), ("Disk", Theme.WARNING)]):
            c.create_rectangle(w - 100 + i * 32, 4, w - 88 + i * 32, 14, fill=color, outline="")
            c.create_text(w - 86 + i * 32, 9, text=label, fill=color, font=("Segoe UI", 9), anchor="w")

    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _stability_index(metrics: dict, volatility: float = 0.0) -> float:
        """คะแนนเสถียรภาพ 0–100 จากโหลดสูงสุดและความผันผวนระยะสั้น"""
        peak = max((max(0.0, min(100.0, float(metrics.get(k, 0.0)))) for k in ("cpu", "ram", "disk")), default=0.0)
        load_penalty = max(0.0, peak - 60.0) * 0.65
        volatility_penalty = min(20.0, max(0.0, volatility) * 2.0)
        return round(max(0.0, min(100.0, 100.0 - load_penalty - volatility_penalty)), 1)

    def _prepare_lstm_model(self, force: bool = False):
        """Load chronological data/train off the UI thread and publish status safely."""
        if self._lstm_setup_in_progress:
            return
        self._lstm_setup_in_progress = True
        self._lstm_samples_since_fit = 0
        try:
            self.lstm_train_button.configure(state="disabled", text="กำลังฝึก LSTM…")
            self.lstm_status_label.configure(text="🧠 LSTM: กำลังอ่านข้อมูลย้อนหลังและเตรียมชุด validation")
        except (AttributeError, tk.TclError):
            pass
        threading.Thread(target=self._lstm_training_worker, args=(force,), daemon=True).start()

    def _lstm_training_worker(self, force: bool):
        rows = []
        error = ""
        try:
            rows = self.collector.get_lstm_training_history(days=30, max_rows=12000)
            self.brain.seed_lstm_history(rows)
            status = self.brain.get_lstm_status()
            if force or not status.get("trained"):
                status = self.brain.train_lstm_from_history(
                    rows, progress_callback=lambda progress: self._safe_after(
                        0, self._update_lstm_progress, progress))
            else:
                status["loaded_from_disk"] = True
        except Exception as exc:
            status = self.brain.get_lstm_status()
            error = str(exc)
        self._safe_after(0, self._finish_lstm_setup, status, len(rows), error)

    def _update_lstm_progress(self, progress: dict):
        try:
            self.lstm_status_label.configure(
                text=(f"🧠 LSTM: Epoch {progress['epoch']}/{progress['epochs']} · "
                      f"Validation loss {progress['validation_loss']} · "
                      f"MAE {progress['validation_mae']} จุดเปอร์เซ็นต์ · "
                      f"ทิศทางถูก {progress['directional_accuracy']}%"))
        except (AttributeError, KeyError, tk.TclError):
            pass

    def _finish_lstm_setup(self, status: dict, row_count: int, error: str = ""):
        self._lstm_setup_in_progress = False
        try:
            self.lstm_train_button.configure(state="normal", text="ฝึก LSTM ใหม่จากข้อมูลย้อนหลัง 30 วัน")
            if error:
                label = f"🧠 LSTM: เตรียมโมเดลไม่สำเร็จ — {error[:100]}"
            elif status.get("trained"):
                source = ("โหลดโมเดลที่บันทึกไว้" if status.get("loaded_from_disk") else
                          "ใช้โมเดลเดิม · ฝึกใหม่ยังไม่ผ่าน" if status.get("last_training_attempt") else
                          "ฝึกเสร็จ")
                label = (f"🧠 LSTM: {source} · ข้อมูล {status.get('training_days', 0)} วัน · "
                         f"Epoch {status.get('epochs', 0)} · "
                         f"Validation loss {status.get('validation_loss', '—')} · "
                         f"MAE {status.get('validation_mae', '—')} จุดเปอร์เซ็นต์ · "
                         f"ทิศทางถูก {status.get('directional_accuracy', '—')}% · "
                         f"ช่วงข้อมูล {status.get('sample_interval_seconds', 15)} วิ "
                         f"· {'บันทึกแล้ว' if status.get('saved', True) else 'บันทึกไฟล์ไม่สำเร็จ'} "
                         f"({row_count:,} แถวตามเวลา)")
            else:
                reason = status.get("reason", "ยังไม่มีข้อมูลเพียงพอ")
                label = f"🧠 LSTM: รอข้อมูลเพิ่ม — {reason} ({row_count:,} แถว)"
            self.lstm_status_label.configure(text=label)
        except (AttributeError, tk.TclError):
            pass

    def _draw_dashboard_trend_graph(self):
        """วาดโหลดจริงและแนวโน้มคาดการณ์ระยะสั้น โดยแสดงเส้นประเป็นค่าประมาณ"""
        canvas = getattr(self, "dashboard_trend_canvas", None)
        if canvas is None:
            return
        try:
            width, height = canvas.winfo_width(), canvas.winfo_height()
            canvas.delete("all")
            if width <= 1 or height <= 1:
                return

            histories = {
                "cpu": self._cpu_hist[-60:],
                "ram": self._ram_hist[-60:],
                "disk": self._disk_hist[-60:],
            }
            count = min((len(values) for values in histories.values()), default=0)
            if count == 0:
                canvas.create_text(width / 2, height / 2, text="กำลังเก็บข้อมูลเพื่อแสดงกราฟ...",
                                   fill=Theme.TEXT_MUTED, font=("Segoe UI", 11))
                return
            histories = {key: values[-count:] for key, values in histories.items()}
            lstm_forecast = self.brain.forecast_lstm(steps=3)
            forecast = lstm_forecast or self.brain.forecast_metrics(histories, steps=10)
            steps = (len(forecast["cpu"]) - 1) if forecast else 0

            pad_l, pad_r, pad_t, pad_b = 42, 16, 14, 25
            left, right = pad_l, max(pad_l + 1, width - pad_r)
            top, bottom = pad_t, max(pad_t + 1, height - pad_b)
            plot_w, plot_h = right - left, bottom - top
            units = max(1, count - 1 + steps)
            x_at = lambda index: left + (index / units) * plot_w
            y_at = lambda value: bottom - (max(0.0, min(100.0, float(value))) / 100.0) * plot_h

            if forecast and steps:
                split_x = x_at(count - 1)
                canvas.create_rectangle(split_x, top, right, bottom, fill="#0b0e12", outline="")
            for level in (0, 25, 50, 75, 100):
                y = y_at(level)
                canvas.create_line(left, y, right, y, fill=Theme.BORDER, dash=(2, 4))
                canvas.create_text(left - 7, y, text=str(level), fill=Theme.TEXT_MUTED,
                                   font=("Segoe UI", 8), anchor="e")
            if forecast and steps:
                split_x = x_at(count - 1)
                canvas.create_line(split_x, top, split_x, bottom, fill=Theme.BORDER, dash=(3, 4))
                canvas.create_text(left + 3, height - 8, text="ล่าสุด", fill=Theme.TEXT_MUTED,
                                   font=("Segoe UI", 8), anchor="w")
                lstm_interval = int(self.brain.get_lstm_status().get("sample_interval_seconds", 15) or 15)
                horizon = (f"LSTM ×3 ช่วง · {lstm_interval}s/ช่วง" if lstm_forecast
                           else "เส้นประ ≈25 วินาที")
                canvas.create_text(right - 3, height - 8, text=horizon, fill=Theme.TEXT_MUTED,
                                   font=("Segoe UI", 8), anchor="e")

            series = [
                ("CPU", histories["cpu"], Theme.ACCENT),
                ("RAM", histories["ram"], Theme.SUCCESS),
                ("Disk", histories["disk"], Theme.WARNING),
            ]
            volatility_by_index = []
            for i in range(count):
                start = max(1, i - 5)
                changes = [abs(values[j] - values[j - 1])
                           for values in histories.values() for j in range(start, i + 1)]
                volatility_by_index.append(sum(changes) / len(changes) if changes else 0.0)
            stability_values = [
                self._stability_index({key: histories[key][i] for key in histories}, volatility_by_index[i])
                for i in range(count)
            ]
            series.append(("เสถียรภาพ", stability_values, Theme.PURPLE))

            if forecast:
                forecast_stability = []
                for i in range(steps + 1):
                    metrics_i = {key: forecast[key][i] for key in ("cpu", "ram", "disk")}
                    changes = [abs(forecast[key][i] - forecast[key][i - 1])
                               for key in metrics_i] if i > 0 else []
                    forecast_stability.append(self._stability_index(
                        metrics_i, sum(changes) / len(changes) if changes else volatility_by_index[-1]))
                forecast["stability"] = forecast_stability

            for key, values, color in series:
                points = []
                for i, value in enumerate(values):
                    points.extend((x_at(i), y_at(value)))
                if len(points) >= 4:
                    canvas.create_line(*points, fill=color, width=2, smooth=True)
                elif len(points) == 2:
                    x, y = points
                    canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")

                forecast_key = "stability" if key == "เสถียรภาพ" else key.lower()
                projected = forecast.get(forecast_key) if forecast else None
                if projected and len(projected) > 1:
                    future_points = [x_at(count - 1), y_at(values[-1])]
                    for step_index, value in enumerate(projected[1:], start=1):
                        future_points.extend((x_at(count - 1 + step_index), y_at(value)))
                    canvas.create_line(*future_points, fill=color, width=2, dash=(5, 3), smooth=True)

            legend = [("CPU", Theme.ACCENT), ("RAM", Theme.SUCCESS), ("Disk", Theme.WARNING),
                      ("เสถียรภาพ", Theme.PURPLE)]
            offset = 0
            for label, color in legend:
                canvas.create_line(right - 330 + offset, top - 5, right - 316 + offset, top - 5,
                                   fill=color, width=2)
                canvas.create_text(right - 312 + offset, top - 5, text=label, fill=color,
                                   font=("Segoe UI", 8), anchor="w")
                offset += 78 if label != "เสถียรภาพ" else 82
        except (tk.TclError, ValueError, TypeError, ZeroDivisionError):
            return
    # แท็บใหม่: 📈 กราฟย้อนหลัง — อ่าน metrics_db.csv แล้วสรุปเป็นรายเดือน
    #   โชว์เส้นค่าเฉลี่ย + จุดพีคของแต่ละวันในเดือนที่เลือก (CPU/RAM/Disk)
    # ══════════════════════════════════════════════════════════
    def _resolve_metrics_csv_path(self):
        """หาตำแหน่งไฟล์ metrics_db.csv — self.collector.db_file เป็น absolute path ที่ถูกต้อง
           อยู่แล้ว (ส่งเข้าไปตอนสร้าง DataCollector ด้วย app_path()) แต่ยังกันเหนียวไว้เผื่อโค้ด
           ที่อื่นสร้าง DataCollector() แบบไม่ส่ง path มาด้วย"""
        p = getattr(self.collector, "db_file", None)
        if isinstance(p, str) and p.lower().endswith(".csv"):
            return p
        return app_path("metrics_db.csv")

    def _load_metrics_rows(self):
        """อ่านไฟล์ metrics_db.csv ทั้งหมด คืนค่าเป็น list of (datetime, cpu, ram, disk)
           แถวที่ parse ไม่ได้ (ข้อมูลเสีย/แถวว่าง) จะถูกข้ามไปเงียบๆ ไม่ให้แอปพัง"""
        path = self._resolve_metrics_csv_path()
        rows = []
        if not os.path.exists(path):
            return rows
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                        rows.append((ts, float(row["cpu"]), float(row["ram"]), float(row["disk"])))
                    except (ValueError, KeyError, TypeError):
                        continue
        except Exception:
            return []
        return rows

    def _compute_period_stats(self, rows, month_str, day: int = None,
                              hour: int = None, week=None):
        """สรุปข้อมูล CPU/RAM/Disk แบบแบ่งกลุ่ม (bucket):
           - day=None (ค่าเริ่มต้น — ไม่ได้เลือกวันไหนเจาะจง): แบ่งกลุ่มรายวันของทั้งเดือน
             month_str (รูปแบบ 'YYYY-MM') → คืนค่า {วันที่: {cpu_avg, cpu_peak, ...}}
           - day=ตัวเลข: กรองวันแล้วแบ่งกลุ่มรายนาทีของวัน
           - hour=ตัวเลข: กรองชั่วโมงแล้วแบ่งกลุ่มรายนาทีของชั่วโมงนั้น"""
        buckets = {}
        for ts, cpu, ram, disk in rows:
            if ts.strftime("%Y-%m") != month_str:
                continue
            if week is not None and not (week[0] <= ts < week[1]):
                continue
            if day is not None and ts.day != day:
                continue
            if hour is not None and ts.hour != hour:
                continue
            if day is None:
                key = ts.day
            elif hour is not None:
                key = ts.minute
            else:
                key = ts.hour * 60 + ts.minute
            b = buckets.setdefault(key, {"cpu": [], "ram": [], "disk": []})
            b["cpu"].append(cpu)
            b["ram"].append(ram)
            b["disk"].append(disk)

        stats = {}
        for key, vals in buckets.items():
            stats[key] = {}
            for metric in ("cpu", "ram", "disk"):
                lst = vals[metric]
                if lst:
                    stats[key][f"{metric}_avg"] = sum(lst) / len(lst)
                    stats[key][f"{metric}_peak"] = max(lst)
        return stats

    _THAI_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
                    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]

    def build_history_graph_tab(self):
        tab = self.tab_history
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_rowconfigure(4, weight=1)

        # ── แถบควบคุมเวลา: เลือกเดือน / สัปดาห์ / วัน / ชั่วโมง ──
        control = ctk.CTkFrame(tab, fg_color="transparent")
        control.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="ew")

        ctk.CTkLabel(control, text="ช่วงเวลา:", font=("Segoe UI", 12, "bold")).pack(
            side="left", padx=(4, 6))
        self.history_granularity_menu = ctk.CTkOptionMenu(
            control, values=["รายเดือน", "รายสัปดาห์", "รายวัน", "รายชั่วโมง"],
            width=115, command=lambda _choice=None: self._on_history_granularity_changed())
        self.history_granularity_menu.set("รายเดือน")
        self.history_granularity_menu.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(control, text="📅 เดือน:", font=("Segoe UI", 12, "bold")).pack(side="left", padx=(4, 6))
        self.history_month_menu = ctk.CTkOptionMenu(
            control, values=["ไม่มีข้อมูล"], width=145,
            command=lambda _choice=None: self._on_history_month_changed())
        self.history_month_menu.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(control, text="สัปดาห์:", font=("Segoe UI", 12, "bold")).pack(
            side="left", padx=(0, 6))
        self.history_week_menu = ctk.CTkOptionMenu(
            control, values=["ทั้งเดือน"], width=145,
            command=lambda _choice=None: self._on_history_controls_changed())
        self.history_week_menu.pack(side="left", padx=(0, 12))

        # ── เลือกวัน (ไม่บังคับ) — เลือก "ทั้งหมด" จะเห็นภาพรวมทั้งเดือนเหมือนเดิม
        #    เลือกวันใดวันหนึ่งจะซูมเข้าไปดูเป็นรายนาทีของวันนั้นแทน (ดูว่าใช้งานหนักช่วงไหน) ──
        ctk.CTkLabel(control, text="📆 วัน:", font=("Segoe UI", 12, "bold")).pack(side="left", padx=(4, 6))
        self.history_day_menu = ctk.CTkOptionMenu(
            control, values=["ทั้งหมด (ทั้งเดือน)"], width=125,
            command=lambda _choice=None: self._on_history_day_changed())
        self.history_day_menu.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(control, text="🕒 ชั่วโมง:", font=("Segoe UI", 12, "bold")).pack(
            side="left", padx=(4, 6))
        self.history_hour_menu = ctk.CTkOptionMenu(
            control, values=["ทุกชั่วโมง"], width=92,
            command=lambda _choice=None: self._on_history_hour_changed())
        self.history_hour_menu.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(control, text="📊 เมตริก:", font=("Segoe UI", 12, "bold")).pack(side="left", padx=(4, 6))
        self.history_metric_menu = ctk.CTkOptionMenu(
            control, values=["ทั้งหมด", "CPU", "RAM", "Disk"], width=92,
            command=lambda _choice=None: self._on_history_controls_changed())
        self.history_metric_menu.pack(side="left", padx=(0, 16))

        self.history_refresh_button = ctk.CTkButton(
            control, text="🔄 รีเฟรช", width=100,
            fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
            command=self._reload_history_data)
        self.history_refresh_button.pack(side="left")

        self.history_status_label = ctk.CTkLabel(
            control, text="", font=("Segoe UI", 11), text_color=Theme.TEXT_MUTED)
        self.history_status_label.pack(side="right", padx=6)

        # ── การ์ดสรุป: ค่าเฉลี่ย + จุดพีค ของเดือนที่เลือก (แยก CPU/RAM/Disk) ──
        summary_row = ctk.CTkFrame(tab, fg_color="transparent")
        summary_row.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        summary_row.grid_columnconfigure((0, 1, 2), weight=1)

        self._history_summary_cards = {}
        for i, (key, label, color) in enumerate([
            ("cpu",  "🧮 CPU",  Theme.ACCENT),
            ("ram",  "💾 RAM",  Theme.SUCCESS),
            ("disk", "🗄️ Disk", Theme.WARNING),
        ]):
            card = ctk.CTkFrame(summary_row, fg_color=Theme.BG_CARD, border_width=1, border_color=Theme.BORDER)
            card.grid(row=0, column=i, padx=4, sticky="nsew")
            ctk.CTkLabel(card, text=label, font=("Segoe UI", 13, "bold"), text_color=color).pack(pady=(10, 2))
            avg_lbl = ctk.CTkLabel(card, text="เฉลี่ย: -", font=("Segoe UI", 12))
            avg_lbl.pack()
            peak_lbl = ctk.CTkLabel(card, text="พีค: -", font=("Segoe UI", 12))
            peak_lbl.pack(pady=(0, 10))
            self._history_summary_cards[key] = (avg_lbl, peak_lbl)

        # ── กราฟหลัก: เส้นทึบ = ค่าเฉลี่ยรายวัน, เส้นประ+จุด = จุดพีครายวัน ──
        graph_outer = ctk.CTkFrame(tab, fg_color=Theme.BG_PANEL, border_width=1, border_color=Theme.BORDER)
        graph_outer.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.history_hint_label = ctk.CTkLabel(
            graph_outer, text="เส้นทึบ = ค่าเฉลี่ยรายวัน   เส้นประ+จุด = จุดพีค (สูงสุด) ของวันนั้น",
            font=("Segoe UI", 11), text_color=Theme.TEXT_MUTED)
        self.history_hint_label.pack(anchor="w", padx=10, pady=(8, 0))
        self.history_canvas = tk.Canvas(graph_outer, bg=Theme.BG_PANEL, height=250, highlightthickness=0)
        self.history_canvas.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.history_canvas.bind(
            "<Configure>", lambda _event: self._schedule_canvas_redraw(
                "history", self._draw_history_graph))

        # Operational KPIs are derived from persisted health probes and incident outcomes.
        kpi_frame = ctk.CTkFrame(tab, fg_color="transparent")
        kpi_frame.grid(row=3, column=0, padx=8, pady=(0, 6), sticky="ew")
        kpi_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self._ops_kpi_title_label = ctk.CTkLabel(
            kpi_frame, text="📌 KPI · เลือกเดือน วัน หรือชั่วโมงจากตัวกรองด้านบน",
            font=("Segoe UI", 10, "bold"), text_color=Theme.TEXT_MUTED)
        self._ops_kpi_title_label.grid(
            row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 2))
        self._ops_kpi_values = {}
        for col, (key, title, color, note) in enumerate((
            ("success", "AI / Auto Heal Success", Theme.SUCCESS, "สำเร็จ ÷ ความพยายาม Auto Heal"),
            ("recovery", "Avg. Recovery Time", Theme.ACCENT, "เหตุการณ์ unhealthy → healthy"),
            ("prevented", "Estimated Downtime Prevented", Theme.WARNING, "เทียบ baseline เหตุการณ์ปกติ ≥3 ครั้ง"),
            ("uptime", "Global Uptime (Observed)", Theme.PURPLE, "คิดเฉพาะช่วง probe ต่อเนื่อง"),
        )):
            card = ctk.CTkFrame(kpi_frame, fg_color=Theme.BG_CARD,
                                border_width=1, border_color=Theme.BORDER)
            card.grid(row=1, column=col, padx=3, sticky="nsew")
            ctk.CTkLabel(card, text=title, font=("Segoe UI", 11, "bold"),
                         text_color=color, wraplength=220).pack(pady=(7, 1))
            value_label = ctk.CTkLabel(card, text="—", font=("Segoe UI", 16, "bold"))
            value_label.pack()
            note_label = ctk.CTkLabel(card, text=note, font=("Segoe UI", 9),
                                      text_color=Theme.TEXT_MUTED, wraplength=220)
            note_label.pack(pady=(0, 7))
            self._ops_kpi_values[key] = (value_label, note_label)

        # Extend Historical graph with per-server probe and resource history.
        server_graph = ctk.CTkFrame(tab, fg_color=Theme.BG_PANEL,
                                    border_width=1, border_color=Theme.BORDER)
        server_graph.grid(row=4, column=0, padx=8, pady=(0, 8), sticky="nsew")
        server_graph.grid_columnconfigure(0, weight=1)
        server_controls = ctk.CTkFrame(server_graph, fg_color="transparent")
        server_controls.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(server_controls, text="🖥️ ประวัติรายเซิร์ฟเวอร์:",
                     font=("Segoe UI", 11, "bold")).pack(side="left", padx=(2, 6))
        self.server_history_server_menu = ctk.CTkOptionMenu(
            server_controls, values=["ยังไม่มีข้อมูล"], width=180,
            command=lambda _choice=None: self._on_server_history_server_changed())
        self.server_history_server_menu.pack(side="left", padx=(0, 8))
        self.server_history_metric_menu = ctk.CTkOptionMenu(
            server_controls,
            values=["Ping latency (ms)", "Online / Uptime (%)", "CPU (%)", "RAM (%)", "Healthy checks (%)"],
            width=180, command=lambda _choice=None: self._draw_server_history_graph())
        self.server_history_metric_menu.set("Ping latency (ms)")
        self.server_history_metric_menu.pack(side="left")
        self.server_history_hint = ctk.CTkLabel(
            server_graph, text="เก็บข้อมูล probe ทุกประมาณ 60 วินาที · เลือกเดือน/สัปดาห์/วัน/ชั่วโมงจากตัวกรองด้านบน",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, anchor="w")
        self.server_history_hint.pack(fill="x", padx=10, pady=(2, 0))
        self.server_history_canvas = tk.Canvas(
            server_graph, bg=Theme.BG_PANEL, height=180, highlightthickness=0)
        self.server_history_canvas.pack(fill="both", expand=True, padx=10, pady=(2, 8))
        self.server_history_canvas.bind(
            "<Configure>", lambda _event: self._schedule_canvas_redraw(
                "server_history", self._draw_server_history_graph))

        self._history_rows = []
        self._server_health_rows = []
        self._server_health_by_month = {}
        self._local_extended_rows = []
        self._local_extended_by_month = {}
        self._ops_events = []
        self._history_month_map = {}
        self._history_week_map = {}
        self._history_daily_stats = {}
        self._history_view_week = None  # (start, end) ของสัปดาห์ที่เลือก
        self._history_view_day = None   # ตัวเลข = วันที่เลือก
        self._history_view_hour = None  # None = ทั้งวัน, ตัวเลข = ซูมดูรายนาทีของชั่วโมงนั้น
        self._history_reload_in_progress = False
        # โหลดข้อมูลหลัง UI สร้างเสร็จเล็กน้อย กันหน้าจอค้างตอนเปิดโปรแกรม
        self.after(150, self._reload_history_data)

    def _reload_history_data(self):
        """Read all historical sources in a worker so large CSV files do not freeze the UI."""
        if self._history_reload_in_progress:
            return
        self._history_reload_in_progress = True
        try:
            self.history_status_label.configure(text="กำลังโหลดข้อมูลกราฟย้อนหลัง…")
            self.history_refresh_button.configure(state="disabled")
        except tk.TclError:
            pass
        threading.Thread(target=self._history_reload_worker, daemon=True).start()

    def _history_reload_worker(self):
        try:
            history_rows = self._load_metrics_rows()
            server_rows = self.collector.get_server_health_history()
            events = read_events()
            local_rows = []
            for row in self.collector.get_extended_history(n=150000):
                try:
                    row["timestamp"] = datetime.strptime(row["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
                except (KeyError, TypeError, ValueError):
                    continue
                local_rows.append(row)
            server_by_month = {}
            for row in server_rows:
                stamp = row.get("timestamp")
                if stamp is not None:
                    server_by_month.setdefault((row.get("server", ""), stamp.strftime("%Y-%m")), []).append(row)
            local_by_month = {}
            for row in local_rows:
                local_by_month.setdefault(row["timestamp"].strftime("%Y-%m"), []).append(row)
            self._safe_after(0, self._finish_history_reload, history_rows, server_rows,
                             local_rows, events, server_by_month, local_by_month, "")
        except Exception as exc:
            self._safe_after(0, self._finish_history_reload, [], [], [], [], {}, {}, str(exc))

    def _finish_history_reload(self, history_rows, server_rows, local_rows, events,
                               server_by_month, local_by_month, error=""):
        self._history_reload_in_progress = False
        self._history_rows = history_rows
        self._server_health_rows = server_rows
        self._local_extended_rows = local_rows
        self._ops_events = events
        self._server_health_by_month = server_by_month
        self._local_extended_by_month = local_by_month
        try:
            self.history_refresh_button.configure(state="normal")
        except tk.TclError:
            pass
        if error:
            self.history_status_label.configure(text=f"โหลดข้อมูลไม่สำเร็จ: {error[:120]}")
            return
        self._update_server_history_selectors()
        months = sorted(
            {ts.strftime("%Y-%m") for ts, *_ in self._history_rows}
            | {row["timestamp"].strftime("%Y-%m") for row in self._server_health_rows
               if row.get("timestamp") is not None}
            | {row["timestamp"].strftime("%Y-%m") for row in self._local_extended_rows
               if row.get("timestamp") is not None}
            | {datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00")).strftime("%Y-%m")
               for event in self._ops_events
               if event.get("timestamp") and self._parse_event_datetime(event.get("timestamp")) is not None})

        self._history_month_map = {}
        display_values = []
        for m in months:
            y, mo = m.split("-")
            label = f"{self._THAI_MONTHS[int(mo)]} {y}"
            self._history_month_map[label] = m
            display_values.append(label)

        if not display_values:
            self.history_month_menu.configure(values=["ไม่มีข้อมูล"])
            self.history_month_menu.set("ไม่มีข้อมูล")
            self.history_status_label.configure(text="⚠️ ยังไม่พบข้อมูลใน metrics_db.csv")
            self._history_daily_stats = {}
            self._history_view_day = None
            self._history_view_hour = None
            self._update_history_summary_cards()
            self._draw_history_graph()
            self._sync_vertical_month_menu()
            self._update_operations_kpis()
            return

        self.history_month_menu.configure(values=display_values)
        self._sync_vertical_month_menu()
        current = self.history_month_menu.get()
        if current not in display_values:
            self.history_month_menu.set(display_values[-1])   # ค่าเริ่มต้น: เดือนล่าสุดที่มีข้อมูล

        self.history_status_label.configure(
            text=(f"Metrics {len(self._history_rows):,} · Server probes "
                  f"{len(self._server_health_rows):,}"))
        self._on_history_month_changed()

    @staticmethod
    def _parse_event_datetime(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _on_history_month_changed(self):
        """ถูกเรียกตอนเปลี่ยนเดือน — อัปเดตรายการ 'วัน' ที่เลือกได้ให้ตรงกับเดือนนั้น (แต่ละเดือน
           มีวันที่ต่างกัน และบางวันอาจไม่มีข้อมูลเก็บไว้เลยก็ได้) แล้วค่อยวาดกราฟใหม่ต่อ"""
        label = self.history_month_menu.get()
        month_str = self._history_month_map.get(label)
        days_with_data = sorted({
            ts.day for ts, *_ in self._history_rows
            if month_str and ts.strftime("%Y-%m") == month_str
        } | {
            row["timestamp"].day for row in self._server_health_rows
            if month_str and row.get("timestamp")
            and row["timestamp"].strftime("%Y-%m") == month_str
        } | {
            row["timestamp"].day for row in self._local_extended_rows
            if month_str and row.get("timestamp")
            and row["timestamp"].strftime("%Y-%m") == month_str
        } | {
            self._parse_event_datetime(event.get("timestamp")).day for event in self._ops_events
            if month_str and self._parse_event_datetime(event.get("timestamp")) is not None
            and self._parse_event_datetime(event.get("timestamp")).strftime("%Y-%m") == month_str
        })
        day_values = ["ทั้งหมด (ทั้งเดือน)"] + [str(d) for d in days_with_data]
        self.history_day_menu.configure(values=day_values)
        selected_day = self.history_day_menu.get()
        if selected_day not in day_values:
            self.history_day_menu.set(day_values[0])
            selected_day = day_values[0]
        if (self.history_granularity_menu.get() in ("รายวัน", "รายชั่วโมง")
                and selected_day == "ทั้งหมด (ทั้งเดือน)" and days_with_data):
            self.history_day_menu.set(str(days_with_data[-1]))
        self._sync_history_week_menu(month_str)
        self._sync_history_hour_menu()
        if self.history_granularity_menu.get() == "รายชั่วโมง":
            hour_values = list(self.history_hour_menu.cget("values"))
            numeric_hours = [value for value in hour_values if str(value)[:2].isdigit()]
            if numeric_hours and self.history_hour_menu.get() == "ทุกชั่วโมง":
                self.history_hour_menu.set(numeric_hours[-1])
                self._on_history_controls_changed()

    def _sync_history_week_menu(self, month_str=None):
        """สร้างตัวเลือกสัปดาห์ตามปฏิทิน โดยตัดขอบเขตให้อยู่ภายในเดือนที่เลือก"""
        month_str = month_str or self._history_month_map.get(self.history_month_menu.get())
        self._history_week_map = {}
        labels = []
        if month_str:
            month_start = datetime.strptime(f"{month_str}-01", "%Y-%m-%d")
            next_month = (datetime(month_start.year + 1, 1, 1) if month_start.month == 12
                          else datetime(month_start.year, month_start.month + 1, 1))
            first_monday = month_start - timedelta(days=month_start.weekday())
            week_start = first_monday
            index = 1
            while week_start < next_month:
                week_end = week_start + timedelta(days=7)
                clipped_start = max(week_start, month_start)
                clipped_end = min(week_end, next_month)
                label = (f"สัปดาห์ {index} ({clipped_start:%d/%m}–"
                         f"{(clipped_end - timedelta(days=1)):%d/%m})")
                self._history_week_map[label] = (clipped_start, clipped_end)
                labels.append(label)
                week_start = week_end
                index += 1
        if not labels:
            labels = ["ไม่มีข้อมูล"]
        self.history_week_menu.configure(values=labels)
        if self.history_week_menu.get() not in labels:
            self.history_week_menu.set(labels[-1])

    def _on_history_granularity_changed(self):
        mode = self.history_granularity_menu.get()
        if mode in ("รายวัน", "รายชั่วโมง"):
            day_values = list(self.history_day_menu.cget("values"))
            numeric_days = [value for value in day_values if str(value).isdigit()]
            if self.history_day_menu.get() == "ทั้งหมด (ทั้งเดือน)" and numeric_days:
                self.history_day_menu.set(numeric_days[-1])
                self._sync_history_hour_menu()
        if mode == "รายชั่วโมง":
            hour_values = list(self.history_hour_menu.cget("values"))
            numeric_hours = [value for value in hour_values if str(value)[:2].isdigit()]
            if self.history_hour_menu.get() == "ทุกชั่วโมง" and numeric_hours:
                self.history_hour_menu.set(numeric_hours[-1])
        self.history_week_menu.configure(state="normal" if mode == "รายสัปดาห์" else "disabled")
        self.history_day_menu.configure(state="normal" if mode in ("รายวัน", "รายชั่วโมง") else "disabled")
        self.history_hour_menu.configure(state="normal" if mode == "รายชั่วโมง" else "disabled")
        self._on_history_controls_changed()

    def _on_history_hour_changed(self):
        if self.history_hour_menu.get() != "ทุกชั่วโมง":
            self.history_granularity_menu.set("รายชั่วโมง")
        self._on_history_granularity_changed()

    def _on_history_day_changed(self):
        if self.history_granularity_menu.get() not in ("รายวัน", "รายชั่วโมง"):
            self.history_granularity_menu.set("รายวัน")
        self._sync_history_hour_menu()

    def _sync_history_hour_menu(self):
        """Offer only hours with recorded data for the selected day."""
        label = self.history_month_menu.get()
        month = self._history_month_map.get(label)
        day_text = self.history_day_menu.get()
        day = int(day_text) if day_text.isdigit() else None
        hours = set()
        if month and day is not None:
            hours.update(ts.hour for ts, *_ in self._history_rows
                         if ts.strftime("%Y-%m") == month and ts.day == day)
            hours.update(row["timestamp"].hour for row in self._server_health_rows
                         if row.get("timestamp") and row["timestamp"].strftime("%Y-%m") == month
                         and row["timestamp"].day == day)
            hours.update(row["timestamp"].hour for row in self._local_extended_rows
                         if row.get("timestamp") and row["timestamp"].strftime("%Y-%m") == month
                         and row["timestamp"].day == day)
            for event in self._ops_events:
                stamp = self._parse_event_datetime(event.get("timestamp"))
                if stamp and stamp.strftime("%Y-%m") == month and stamp.day == day:
                    hours.add(stamp.hour)
        choices = ["ทุกชั่วโมง"] + [f"{hour:02d}:00" for hour in sorted(hours)]
        self.history_hour_menu.configure(values=choices,
                                         state="normal" if day is not None else "disabled")
        if self.history_hour_menu.get() not in choices:
            self.history_hour_menu.set("ทุกชั่วโมง")
        self._on_history_controls_changed()

    def _on_history_controls_changed(self):
        """ถูกเรียกตอนเปลี่ยนวัน/เมตริก (หรือหลังเปลี่ยนเดือนเสร็จแล้ว) — คำนวณสถิติใหม่แล้ววาดกราฟ"""
        label = self.history_month_menu.get()
        month_str = self._history_month_map.get(label)
        mode = self.history_granularity_menu.get()
        self.history_week_menu.configure(state="normal" if mode == "รายสัปดาห์" else "disabled")
        self.history_day_menu.configure(state="normal" if mode in ("รายวัน", "รายชั่วโมง") else "disabled")
        self.history_hour_menu.configure(state="normal" if mode == "รายชั่วโมง" else "disabled")
        self._history_view_week = (self._history_week_map.get(self.history_week_menu.get())
                                   if mode == "รายสัปดาห์" else None)
        day_label = self.history_day_menu.get()
        self._history_view_day = (int(day_label) if mode in ("รายวัน", "รายชั่วโมง")
                                  and day_label.isdigit() else None)
        hour_label = self.history_hour_menu.get()
        self._history_view_hour = (int(hour_label[:2])
                                   if mode == "รายชั่วโมง" and self._history_view_day is not None
                                   and hour_label[:2].isdigit() else None)

        self._history_daily_stats = (
            self._compute_period_stats(self._history_rows, month_str,
                                       self._history_view_day, self._history_view_hour,
                                       self._history_view_week)
            if month_str else {}
        )
        self._update_history_summary_cards()
        self._update_history_hint_label()
        self._update_operations_kpis()
        self._draw_history_graph()
        self._draw_server_history_graph()

    def _update_server_history_selectors(self):
        servers = sorted({row.get("server", "") for row in self._server_health_rows
                          if row.get("server")})
        values = (["เครื่องนี้ (Local Host)"] if self._local_extended_rows else []) + ["เหตุการณ์ระบบ"] + servers
        values = values or ["ยังไม่มีข้อมูล"]
        self.server_history_server_menu.configure(values=values)
        if self.server_history_server_menu.get() not in values:
            self.server_history_server_menu.set(values[0])
        self._on_server_history_server_changed()
        self._update_operations_kpis()
        self._draw_server_history_graph()

    def _on_server_history_server_changed(self):
        local_metrics = [
            "CPU (%)", "RAM (%)", "Disk (%)", "Swap (%)",
            "Disk Read (MB/s)", "Disk Write (MB/s)",
            "Network Receive (MB/s)", "Network Send (MB/s)",
            "Process Count", "Network Connections", "Load Average 1m", "Log Size (MB)",
        ]
        server_metrics = [
            "Ping latency (ms)", "Online / Uptime (%)", "CPU (%)", "RAM (%)", "Healthy checks (%)",
        ]
        event_metrics = ["Anomaly events (count)", "Predictive warnings (count)",
                         "Auto Heal attempts (count)",
                         "Recovery time (seconds)"]
        selected_server = self.server_history_server_menu.get()
        choices = (local_metrics if selected_server == "เครื่องนี้ (Local Host)" else
                   event_metrics if selected_server == "เหตุการณ์ระบบ" else server_metrics)
        current = self.server_history_metric_menu.get()
        self.server_history_metric_menu.configure(values=choices)
        if current not in choices:
            self.server_history_metric_menu.set(choices[0])
        self.server_history_hint.configure(
            text=("Local metrics เก็บประมาณทุก 15 วินาที · ใช้ตัวกรองเดือน/วันด้านบน"
                  if selected_server == "เครื่องนี้ (Local Host)" else
                  "อ่าน anomaly, Auto Heal และ recovery จาก Action Log ที่บันทึกไว้"
                  if selected_server == "เหตุการณ์ระบบ" else
                  "Server probe เก็บประมาณทุก 60 วินาที · ช่วงที่โปรแกรมปิดถือเป็นข้อมูลไม่ทราบสถานะ"))
        self._draw_server_history_graph()

    @staticmethod
    def _format_duration(seconds):
        if seconds is None:
            return "—"
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds} วินาที"
        if seconds < 3600:
            return f"{seconds // 60} นาที {seconds % 60} วินาที"
        hours, remainder = divmod(seconds, 3600)
        return f"{hours} ชม. {remainder // 60} นาที"

    def _update_operations_kpis(self):
        start_at, end_at, period_label = self._selected_history_period()
        self._ops_kpi_title_label.configure(
            text=f"📌 KPI · {period_label} · คำนวณจาก probe และเหตุการณ์ที่บันทึกจริง")
        kpis = calculate_kpis(
            self._server_health_rows, self._ops_events, days=30,
            start_at=start_at, end_at=end_at)
        success, success_note = self._ops_kpi_values["success"]
        recovery, recovery_note = self._ops_kpi_values["recovery"]
        prevented, prevented_note = self._ops_kpi_values["prevented"]
        uptime, uptime_note = self._ops_kpi_values["uptime"]

        success.configure(text=(f"{kpis['auto_heal_success_percent']:.1f}%"
                                if kpis["auto_heal_success_percent"] is not None else "ยังไม่มีข้อมูล"))
        success_note.configure(
            text=(f"{kpis['auto_heal_successes']}/{kpis['auto_heal_attempts']} Auto Heal · ไม่ใช่ model accuracy"
                  if kpis["auto_heal_attempts"] else "รอผล Auto Heal ที่บันทึกจริง"))
        recovery.configure(text=self._format_duration(kpis["average_recovery_seconds"]))
        recovery_note.configure(text=f"จาก {kpis['recovery_samples']} incident ที่ตรวจพบและกลับ healthy")
        if kpis["estimated_prevented_seconds"] is None:
            prevented.configure(text="รอข้อมูล baseline")
            prevented_note.configure(
                text=f"ต้องมี incident ที่ไม่ได้ Auto Heal ≥3 ครั้ง · พบ {kpis['untreated_baseline_incidents']} ครั้ง")
        else:
            prevented.configure(text=self._format_duration(kpis["estimated_prevented_seconds"]))
            prevented_note.configure(
                text=(f"ประมาณการเทียบ median ปกติ {self._format_duration(kpis['untreated_median_recovery_seconds'])} · "
                      f"{kpis['prevented_estimate_samples']} Auto-Healed incident"))
        uptime.configure(text=(f"{kpis['uptime_percent']:.3f}%"
                               if kpis["uptime_percent"] is not None else "ยังไม่มีข้อมูล"))
        uptime_note.configure(
            text=(f"{kpis['servers_observed']} server · มีข้อมูลต่อเนื่อง {kpis['observed_seconds'] / 3600:.1f} ชม."
                  if kpis["observed_seconds"] else "ไม่มี probe ต่อเนื่องในช่วงเวลาที่เลือก"))

    def _selected_history_period(self):
        """Return local-time boundaries and a readable caption for Historical KPI filters."""
        month = self._history_month_map.get(self.history_month_menu.get())
        if not month:
            now = datetime.now()
            return now - timedelta(days=30), now, "30 วันล่าสุด"
        start = datetime.strptime(f"{month}-01", "%Y-%m-%d")
        year, month_number = start.year, start.month
        next_month = (datetime(year + 1, 1, 1) if month_number == 12
                      else datetime(year, month_number + 1, 1))
        mode = self.history_granularity_menu.get()
        day = getattr(self, "_history_view_day", None)
        hour = getattr(self, "_history_view_hour", None)
        week = getattr(self, "_history_view_week", None)
        if mode == "รายสัปดาห์" and week is not None:
            start, end = week
            caption = f"สัปดาห์ {start:%d/%m}–{(end - timedelta(days=1)):%d/%m/%Y}"
        elif day is not None:
            start = start.replace(day=day)
            end = start + timedelta(days=1)
            if hour is not None:
                start = start.replace(hour=hour)
                end = start + timedelta(hours=1)
                caption = f"{start:%Y-%m-%d} {start:%H}:00–{start:%H}:59"
            else:
                caption = f"{start:%Y-%m-%d} (ทั้งวัน)"
        else:
            end = next_month
            caption = f"{start:%Y-%m} (ทั้งเดือน)"
        return start, end - timedelta(microseconds=1), caption

    def _draw_server_history_graph(self):
        canvas = getattr(self, "server_history_canvas", None)
        if canvas is None:
            return
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        server = self.server_history_server_menu.get()
        metric = self.server_history_metric_menu.get()
        month = self._history_month_map.get(self.history_month_menu.get())
        selected_day = getattr(self, "_history_view_day", None)
        selected_hour = getattr(self, "_history_view_hour", None)
        selected_week = getattr(self, "_history_view_week", None)
        key_name = {
            "Ping latency (ms)": "latency_ms",
            "Online / Uptime (%)": "online",
            "CPU (%)": "cpu_pct",
            "RAM (%)": "ram_pct",
            "Healthy checks (%)": "health",
        }.get(metric)
        buckets = {}
        if month and server == "เหตุการณ์ระบบ":
            if metric == "Recovery time (seconds)":
                open_incidents = {}
                recovered = []
                ordered_events = sorted(
                    self._ops_events,
                    key=lambda item: self._parse_event_datetime(item.get("timestamp"))
                    or datetime.min)
                for event in ordered_events:
                    details = event.get("details") or {}
                    if event.get("category") != "service_health":
                        continue
                    name = str(details.get("container") or "")
                    ts = self._parse_event_datetime(event.get("timestamp"))
                    if not name or ts is None:
                        continue
                    if selected_week is not None and not (selected_week[0] <= ts < selected_week[1]):
                        continue
                    if details.get("current") == "unhealthy":
                        open_incidents.setdefault(name, ts)
                    elif details.get("current") == "healthy" and name in open_incidents:
                        started = open_incidents.pop(name)
                        try:
                            duration = float(details.get("observed_recovery_seconds"))
                        except (TypeError, ValueError):
                            duration = max(0.0, ts.timestamp() - started.timestamp())
                        recovered.append((ts, duration))
                for ts, duration in recovered:
                    if (ts.strftime("%Y-%m") == month
                            and (selected_week is None or selected_week[0] <= ts < selected_week[1])
                            and (selected_day is None or ts.day == selected_day)
                            and (selected_hour is None or ts.hour == selected_hour)):
                        slot = (ts.minute if selected_hour is not None else
                                ts.hour * 60 + ts.minute if selected_day is not None else ts.day)
                        buckets.setdefault(slot, []).append(duration)
            else:
                for event in self._ops_events:
                    ts = self._parse_event_datetime(event.get("timestamp"))
                    if ts is None:
                        continue
                    if ts.strftime("%Y-%m") != month or (selected_day is not None and ts.day != selected_day):
                        continue
                    if selected_week is not None and not (selected_week[0] <= ts < selected_week[1]):
                        continue
                    if selected_hour is not None and ts.hour != selected_hour:
                        continue
                    details = event.get("details") or {}
                    if metric == "Anomaly events (count)" and event.get("category") == "anomaly":
                        value = 1.0
                    elif (metric == "Predictive warnings (count)"
                          and event.get("category") == "predictive_alert"):
                        value = 1.0
                    elif (metric == "Auto Heal attempts (count)"
                          and event.get("category") == "auto_remediation"
                          and details.get("phase", "attempt") == "attempt"):
                        value = 1.0
                    else:
                        continue
                    slot = (ts.minute if selected_hour is not None else
                            ts.hour * 60 + ts.minute if selected_day is not None else ts.day)
                    buckets.setdefault(slot, []).append(value)
        elif month and server == "เครื่องนี้ (Local Host)":
            local_fields = {
                "CPU (%)": ("cpu", "%"), "RAM (%)": ("ram", "%"),
                "Disk (%)": ("disk", "%"), "Swap (%)": ("swap_percent", "%"),
                "Disk Read (MB/s)": ("disk_read_mb_s", "MB/s"),
                "Disk Write (MB/s)": ("disk_write_mb_s", "MB/s"),
                "Network Receive (MB/s)": ("network_recv_mb_s", "MB/s"),
                "Network Send (MB/s)": ("network_sent_mb_s", "MB/s"),
                "Process Count": ("process_count", "processes"),
                "Network Connections": ("network_connections", "connections"),
                "Load Average 1m": ("load_1m", "load"),
                "Log Size (MB)": ("log_size_mb", "MB"),
            }
            field = local_fields.get(metric, (None, ""))[0]
            for row in self._local_extended_by_month.get(month, []):
                ts = row.get("timestamp")
                if (ts is None or ts.strftime("%Y-%m") != month
                        or (selected_day is not None and ts.day != selected_day)):
                    continue
                if selected_week is not None and not (selected_week[0] <= ts < selected_week[1]):
                    continue
                if selected_hour is not None and ts.hour != selected_hour:
                    continue
                try:
                    value = float(row.get(field))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(value):
                    continue
                slot = (ts.minute if selected_hour is not None else
                        ts.hour * 60 + ts.minute if selected_day is not None else ts.day)
                buckets.setdefault(slot, []).append(value)
        elif month and server != "ยังไม่มีข้อมูล":
            for row in self._server_health_by_month.get((server, month), []):
                ts = row.get("timestamp")
                if (row.get("server") != server or ts is None
                        or ts.strftime("%Y-%m") != month):
                    continue
                if selected_week is not None and not (selected_week[0] <= ts < selected_week[1]):
                    continue
                if selected_day is not None and ts.day != selected_day:
                    continue
                if selected_hour is not None and ts.hour != selected_hour:
                    continue
                if key_name == "latency_ms":
                    value = row.get("latency_ms") if row.get("online") else None
                elif key_name == "online":
                    value = 100.0 if row.get("online") else 0.0
                elif key_name == "health":
                    health = row.get("health")
                    value = (100.0 if health == "healthy" else 0.0
                             if health == "unhealthy" else None)
                else:
                    value = row.get(key_name)
                if value is None:
                    continue
                slot = (ts.minute if selected_hour is not None else
                        ts.hour * 60 + ts.minute if selected_day is not None else ts.day)
                buckets.setdefault(slot, []).append(float(value))
        values = sorted((slot, sum(items) if metric.endswith("(count)") else sum(items) / len(items))
                        for slot, items in buckets.items())
        if not values:
            msg = "ยังไม่มีข้อมูล probe ในช่วงที่เลือก" if self._server_health_rows else "รอเก็บข้อมูล server probe"
            canvas.create_text(width / 2, height / 2, text=msg,
                               fill=Theme.TEXT_MUTED, font=("Segoe UI", 11))
            return

        pad_l, pad_r, pad_t, pad_b = 42, 18, 18, 25
        plot_w, plot_h = max(1, width - pad_l - pad_r), max(1, height - pad_t - pad_b)
        max_value = max(value for _, value in values)
        percentage_metrics = {"Online / Uptime (%)", "CPU (%)", "RAM (%)", "Healthy checks (%)", "Swap (%)", "Disk (%)"}
        if metric in percentage_metrics:
            y_max = 100.0
        elif metric in ("Ping latency (ms)", "Recovery time (seconds)"):
            y_max = max(50.0, math.ceil(max_value / 50.0) * 50.0)
        elif metric.endswith("(count)"):
            y_max = max(1.0, math.ceil(max_value))
        else:
            y_max = max(1.0, math.ceil(max_value / 10.0) * 10.0)
        x_min, x_max = values[0][0], values[-1][0]
        x_of = lambda index: (pad_l + plot_w / 2 if x_max == x_min else
                              pad_l + ((index - x_min) / (x_max - x_min)) * plot_w)
        y_of = lambda value: pad_t + plot_h - max(0.0, min(y_max, value)) / y_max * plot_h
        for fraction in (0, 0.25, 0.5, 0.75, 1):
            value = y_max * fraction
            y = y_of(value)
            canvas.create_line(pad_l, y, width - pad_r, y, fill=Theme.BORDER, dash=(2, 4))
            canvas.create_text(pad_l - 6, y, text=f"{value:.0f}", fill=Theme.TEXT_MUTED,
                               font=("Segoe UI", 8), anchor="e")
        points = []
        for slot, value in values:
            points.extend((x_of(slot), y_of(value)))
        color = (Theme.ACCENT if metric in ("Ping latency (ms)", "Network Receive (MB/s)",
                                            "Anomaly events (count)", "Predictive warnings (count)") else
                 Theme.SUCCESS if metric == "Online / Uptime (%)" else
                 Theme.WARNING if metric in ("CPU (%)", "Healthy checks (%)", "Disk Read (MB/s)", "Auto Heal attempts (count)") else Theme.PURPLE)
        if len(points) >= 4:
            canvas.create_line(*points, fill=color, width=2, smooth=True)
        for x, y in zip(points[0::2], points[1::2]):
            canvas.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                               fill=color, outline=Theme.BG_PANEL, width=1)
        stride = max(1, len(values) // 8)
        for index, (slot, _value) in enumerate(values):
            if index % stride == 0 or index == len(values) - 1:
                label = (f"{selected_hour:02d}:{slot:02d}" if selected_hour is not None else
                         f"{slot // 60:02d}:{slot % 60:02d}" if selected_day is not None else
                         str(slot))
                canvas.create_text(x_of(slot), height - 10, text=label,
                                   fill=Theme.TEXT_MUTED, font=("Segoe UI", 8))
        unit = ("ms" if metric == "Ping latency (ms)" else
                "seconds" if metric == "Recovery time (seconds)" else
                "events" if metric.endswith("(count)") else
                "%" if metric in percentage_metrics else
                "MB/s" if "MB/s" in metric else
                "processes" if metric == "Process Count" else
                "connections" if metric == "Network Connections" else
                "load" if metric == "Load Average 1m" else "MB")
        summary = (sum(value for _, value in values) if metric.endswith("(count)") else
                   sum(value for _, value in values) / len(values))
        summary_label = "รวม" if metric.endswith("(count)") else "เฉลี่ย"
        canvas.create_text(width - pad_r, pad_t - 7,
                           text=f"{server} · {metric} · {summary_label} {summary:.1f} {unit}",
                           fill=color, font=("Segoe UI", 9), anchor="e")

    def _update_history_hint_label(self):
        mode = self.history_granularity_menu.get()
        if self._history_view_hour is not None:
            self.history_hint_label.configure(
                text=(f"🔎 {self._history_view_day:02d} · ชั่วโมง {self._history_view_hour:02d}:00–"
                      f"{self._history_view_hour:02d}:59 · ค่าเฉลี่ยและจุดพีครายนาที"))
        elif self._history_view_day is not None:
            self.history_hint_label.configure(
                text=f"🔎 รายวัน {self._history_view_day} · เก็บข้อมูลประมาณทุก 15 วินาที — "
                     "เส้นทึบ = ค่าเฉลี่ยรายนาที   "
                     "เส้นประ+จุด = จุดพีคของนาทีนั้น")
        elif mode == "รายสัปดาห์" and self._history_view_week:
            start, end = self._history_view_week
            self.history_hint_label.configure(
                text=(f"🔎 รายสัปดาห์ {start:%d/%m}–{(end - timedelta(days=1)):%d/%m} · "
                      "เส้นทึบ = ค่าเฉลี่ยรายวัน   เส้นประ+จุด = จุดพีคของวันนั้น"))
        else:
            self.history_hint_label.configure(
                text="รายเดือน · เก็บข้อมูลประมาณทุก 15 วินาที · เส้นทึบ = ค่าเฉลี่ยรายวัน   "
                     "เส้นประ+จุด = จุดพีค (สูงสุด) ของวันนั้น")

    def _update_history_summary_cards(self):
        for metric in ("cpu", "ram", "disk"):
            avg_lbl, peak_lbl = self._history_summary_cards[metric]
            avgs  = [v[f"{metric}_avg"]  for v in self._history_daily_stats.values() if f"{metric}_avg"  in v]
            peaks = [v[f"{metric}_peak"] for v in self._history_daily_stats.values() if f"{metric}_peak" in v]
            if avgs:
                avg_lbl.configure(text=f"เฉลี่ย: {sum(avgs) / len(avgs):.1f}%")
                peak_lbl.configure(text=f"พีค: {max(peaks):.1f}%")
            else:
                avg_lbl.configure(text="เฉลี่ย: -")
                peak_lbl.configure(text="พีค: -")

    def _draw_history_graph(self):
        """วาดกราฟรายวัน (เฉลี่ย=เส้นทึบ, พีค=เส้นประ+จุด) ของเดือน/เมตริกที่เลือกอยู่"""
        c = self.history_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return

        pad_l, pad_r, pad_t, pad_b = 34, 16, 10, 24
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)

        if not self._history_daily_stats:
            empty_msg = ("ยังไม่มีข้อมูลสำหรับชั่วโมงนี้" if self._history_view_hour is not None else
                         "ยังไม่มีข้อมูลสำหรับวันนี้" if self._history_view_day is not None
                        else "ยังไม่มีข้อมูลสำหรับเดือนนี้")
            c.create_text(w / 2, h / 2, text=empty_msg,
                           fill=Theme.TEXT_MUTED, font=("Segoe UI", 12))
            return

        days = sorted(self._history_daily_stats.keys())
        n = len(days)

        def x_of(i):
            return pad_l + plot_w / 2 if n <= 1 else pad_l + (i / (n - 1)) * plot_w

        def y_of(v):
            return pad_t + plot_h - (max(0, min(100, v)) / 100.0) * plot_h

        # กริดเส้นแนวนอน + label แกน Y (0/25/50/75/100%)
        for pct in (0, 25, 50, 75, 100):
            y = y_of(pct)
            c.create_line(pad_l, y, w - pad_r, y, fill=Theme.BORDER, dash=(2, 4))
            c.create_text(pad_l - 6, y, text=str(pct), fill=Theme.TEXT_MUTED,
                          font=("Segoe UI", 9), anchor="e")

        # label แกน X (วันที่ในเดือน หรือชั่วโมงถ้าซูมดูวันเดียว) — โชว์ห่างๆ กันตัวเลขทับกัน
        step = max(1, n // 10)
        for i, d in enumerate(days):
            if i % step == 0 or i == n - 1:
                x_label = (f"{self._history_view_hour:02d}:{d:02d}"
                           if self._history_view_hour is not None else
                           f"{d // 60:02d}:{d % 60:02d}"
                           if self._history_view_day is not None else str(d))
                c.create_text(x_of(i), h - pad_b + 12, text=x_label,
                              fill=Theme.TEXT_MUTED, font=("Segoe UI", 9))

        metric_sel = self.history_metric_menu.get()
        metrics_to_draw = {
            "CPU":  [("cpu",  Theme.ACCENT)],
            "RAM":  [("ram",  Theme.SUCCESS)],
            "Disk": [("disk", Theme.WARNING)],
        }.get(metric_sel, [("cpu", Theme.ACCENT), ("ram", Theme.SUCCESS), ("disk", Theme.WARNING)])

        for metric, color in metrics_to_draw:
            avg_pts, peak_pts = [], []
            for i, d in enumerate(days):
                stat = self._history_daily_stats[d]
                if f"{metric}_avg" in stat:
                    avg_pts.extend([x_of(i), y_of(stat[f"{metric}_avg"])])
                if f"{metric}_peak" in stat:
                    px, py = x_of(i), y_of(stat[f"{metric}_peak"])
                    peak_pts.extend([px, py])
                    c.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline="")
            if len(avg_pts) >= 4:
                c.create_line(*avg_pts, fill=color, width=2, smooth=True)
                for px, py in zip(avg_pts[0::2], avg_pts[1::2]):
                    c.create_oval(px - 2.5, py - 2.5, px + 2.5, py + 2.5,
                                  fill=color, outline=Theme.BG_PANEL, width=1)
            elif len(avg_pts) == 2:
                ax, ay = avg_pts
                c.create_oval(ax - 3, ay - 3, ax + 3, ay + 3,
                              fill=color, outline=Theme.BG_PANEL)
            if len(peak_pts) >= 4:
                c.create_line(*peak_pts, fill=color, width=1, dash=(4, 3), smooth=True)

        # legend มุมขวาบน
        legend_items = [("CPU", Theme.ACCENT), ("RAM", Theme.SUCCESS), ("Disk", Theme.WARNING)]
        for i, (label, color) in enumerate(legend_items):
            lx = w - pad_r - 100 + i * 34
            c.create_rectangle(lx, 2, lx + 10, 10, fill=color, outline="")
            c.create_text(lx + 13, 6, text=label, fill=color, font=("Segoe UI", 9), anchor="w")

    # ══════════════════════════════════════════════════════════
    # แท็บ 2: Server Dashboard — ดู CPU/RAM/Disk + Terminal ของแต่ละ server แยกกัน
    # ══════════════════════════════════════════════════════════
    def build_vertical_chart_tab(self):
        """Resource averages and capacity advice for the selected time range."""
        tab = self.tab_vertical_chart
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        controls = ctk.CTkFrame(tab, fg_color="transparent")
        controls.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        ctk.CTkLabel(controls, text="ช่วงเวลา:", font=("Segoe UI", 12, "bold")).pack(
            side="left", padx=(4, 8))
        self.vertical_period_menu = ctk.CTkOptionMenu(
            controls, values=["ภาพรวมทั้งหมด", "รายเดือน", "รายวัน", "รายชั่วโมง"],
            width=140, command=lambda _choice=None: self._on_vertical_period_changed())
        self.vertical_period_menu.set("ภาพรวมทั้งหมด")
        self.vertical_period_menu.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(controls, text="เดือน:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 4))
        self.vertical_month_menu = ctk.CTkOptionMenu(
            controls, values=["กำลังโหลด..."], width=180,
            command=lambda _choice=None: self._sync_vertical_date_menu())
        self.vertical_month_menu.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(controls, text="วัน:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 4))
        self.vertical_day_menu = ctk.CTkOptionMenu(
            controls, values=["ไม่มีข้อมูล"], width=120,
            command=lambda _choice=None: self._sync_vertical_hour_menu())
        self.vertical_day_menu.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(controls, text="ชั่วโมง:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 4))
        self.vertical_hour_menu = ctk.CTkOptionMenu(
            controls, values=["ทุกชั่วโมง"], width=105,
            command=lambda _choice=None: self._render_vertical_chart())
        self.vertical_hour_menu.pack(side="left")
        self.vertical_export_button = ctk.CTkButton(
            controls, text="💾 บันทึก PDF", width=125,
            command=self._export_vertical_chart_pdf)
        self.vertical_export_button.pack(side="left", padx=(10, 4))
        self.vertical_export_status = ctk.CTkLabel(
            controls, text="", font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED)
        self.vertical_export_status.pack(side="left", padx=4)
        self.vertical_data_label = ctk.CTkLabel(
            controls, text="ข้อมูลจาก Historical graph", font=("Segoe UI", 11),
            text_color=Theme.TEXT_MUTED)
        self.vertical_data_label.pack(side="right", padx=8)

        chart_frame = ctk.CTkFrame(tab, fg_color=Theme.BG_PANEL, border_width=1,
                                   border_color=Theme.BORDER)
        chart_frame.grid(row=1, column=0, padx=10, pady=6, sticky="nsew")
        chart_frame.grid_columnconfigure(0, weight=1)
        chart_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            chart_frame, text="ค่าเฉลี่ยการใช้ทรัพยากรในช่วงที่เลือก  ·  ขีดประสีขาว = P95",
            font=("Segoe UI", 12), text_color=Theme.TEXT_MUTED,
        ).grid(row=0, column=0, padx=14, pady=(10, 0), sticky="w")
        self.vertical_chart_canvas = tk.Canvas(
            chart_frame, bg=Theme.BG_PANEL, height=260, highlightthickness=0)
        self.vertical_chart_canvas.grid(row=1, column=0, padx=10, pady=(4, 10), sticky="nsew")
        self.vertical_chart_canvas.bind(
            "<Configure>", lambda _event: self._schedule_canvas_redraw(
                "vertical_chart", self._render_vertical_chart, 140))

        ctk.CTkLabel(
            tab, text="ประมาณการทรัพยากรเพื่อรองรับการใช้งานถัดไป",
            font=("Segoe UI", 15, "bold"), text_color=Theme.TEXT_PRIMARY,
        ).grid(row=2, column=0, padx=12, pady=(8, 4), sticky="w")

        recommendation_row = ctk.CTkFrame(tab, fg_color="transparent")
        recommendation_row.grid(row=3, column=0, padx=8, pady=(0, 6), sticky="ew")
        recommendation_row.grid_columnconfigure((0, 1, 2), weight=1)
        self._vertical_recommendation_labels = {}
        for column, (key, title, color) in enumerate((
            ("cpu", "CPU", Theme.ACCENT),
            ("ram", "RAM", Theme.SUCCESS),
            ("disk", "Disk", Theme.WARNING),
        )):
            card = ctk.CTkFrame(recommendation_row, fg_color=Theme.BG_CARD,
                                border_width=1, border_color=Theme.BORDER)
            card.grid(row=0, column=column, padx=4, sticky="nsew")
            ctk.CTkLabel(card, text=title, font=("Segoe UI", 13, "bold"),
                         text_color=color).pack(pady=(10, 3))
            value_label = ctk.CTkLabel(card, text="กำลังวิเคราะห์",
                                       font=("Segoe UI", 18, "bold"), text_color=Theme.TEXT_PRIMARY)
            value_label.pack()
            detail_label = ctk.CTkLabel(card, text="", font=("Segoe UI", 10),
                                        text_color=Theme.TEXT_MUTED, justify="center")
            detail_label.pack(padx=8, pady=(2, 10))
            self._vertical_recommendation_labels[key] = (value_label, detail_label)

        self.vertical_capacity_note = ctk.CTkLabel(
            tab,
            text=("คำนวณจาก P95 ของช่วงเวลาที่เลือก เผื่อการเติบโต 20% "
                  "และตั้งเป้าใช้งานไม่เกิน CPU 70% · RAM 75% · Disk 80% "
                  "เป็นค่าประมาณจากเครื่องนี้ ไม่ใช่การปรับทรัพยากรให้อัตโนมัติ"),
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, justify="left", anchor="w",
        )
        self.vertical_capacity_note.grid(row=4, column=0, padx=12, pady=(0, 10), sticky="ew")

        self._vertical_month_map = {}
        self._vertical_day_map = {}
        self.after(250, self._sync_vertical_month_menu)

    def _sync_vertical_month_menu(self):
        """Refresh vertical chart selectors from the historical measurements."""
        menu = getattr(self, "vertical_month_menu", None)
        if menu is None:
            return
        month_map = getattr(self, "_history_month_map", {})
        self._vertical_month_map = dict(month_map)
        month_labels = list(month_map)
        if not month_labels:
            menu.configure(values=["ไม่มีข้อมูล"])
            menu.set("ไม่มีข้อมูล")
            self.vertical_day_menu.configure(values=["ไม่มีข้อมูล"])
            self.vertical_day_menu.set("ไม่มีข้อมูล")
            self.vertical_hour_menu.configure(values=["ทุกชั่วโมง"])
            self.vertical_hour_menu.set("ทุกชั่วโมง")
            self.vertical_data_label.configure(text="ยังไม่มีข้อมูลใน Historical graph")
            self._render_vertical_chart()
            return

        menu.configure(values=month_labels)
        selected = menu.get()
        if selected not in month_labels:
            menu.set(month_labels[-1])
        self._sync_vertical_date_menu()

    def _sync_vertical_date_menu(self):
        month_key = self._vertical_month_map.get(self.vertical_month_menu.get())
        dates = sorted({ts.date() for ts, *_ in getattr(self, "_history_rows", [])
                        if month_key and ts.strftime("%Y-%m") == month_key})
        self._vertical_day_map = {date.strftime("%d %b %Y"): date for date in dates}
        labels = list(self._vertical_day_map)
        if not labels:
            labels = ["ไม่มีข้อมูล"]
        self.vertical_day_menu.configure(values=labels)
        selected = self.vertical_day_menu.get()
        self.vertical_day_menu.set(selected if selected in labels else labels[-1])
        self._sync_vertical_hour_menu()

    def _sync_vertical_hour_menu(self):
        day = self._vertical_day_map.get(self.vertical_day_menu.get())
        hours = sorted({ts.hour for ts, *_ in getattr(self, "_history_rows", [])
                        if day and ts.date() == day})
        labels = [f"{hour:02d}:00" for hour in hours]
        options = ["ทุกชั่วโมง"] + labels
        self.vertical_hour_menu.configure(values=options)
        if self.vertical_hour_menu.get() not in options:
            self.vertical_hour_menu.set(labels[-1] if labels else "ทุกชั่วโมง")
        self._on_vertical_period_changed()

    def _on_vertical_period_changed(self):
        period = self.vertical_period_menu.get()
        self.vertical_month_menu.configure(state="normal" if period != "ภาพรวมทั้งหมด" else "disabled")
        self.vertical_day_menu.configure(state="normal" if period in ("รายวัน", "รายชั่วโมง") else "disabled")
        self.vertical_hour_menu.configure(state="normal" if period == "รายชั่วโมง" else "disabled")
        self._render_vertical_chart()

    @staticmethod
    def _percentile95(values):
        cleaned = sorted(float(value) for value in values if math.isfinite(float(value)))
        if not cleaned:
            return 0.0
        index = min(len(cleaned) - 1, max(0, math.ceil(0.95 * len(cleaned)) - 1))
        return cleaned[index]

    def _render_vertical_chart(self):
        """Summarize overall, monthly, daily, or hourly resource usage."""
        canvas = getattr(self, "vertical_chart_canvas", None)
        if canvas is None:
            return
        period = self.vertical_period_menu.get()
        month_label = self.vertical_month_menu.get()
        month_key = self._vertical_month_map.get(month_label)
        selected_day = self._vertical_day_map.get(self.vertical_day_menu.get())
        selected_hour = self.vertical_hour_menu.get()
        period_rows = []
        for timestamp, cpu, ram, disk in getattr(self, "_history_rows", []):
            if period != "ภาพรวมทั้งหมด" and (not month_key or timestamp.strftime("%Y-%m") != month_key):
                continue
            if period in ("รายวัน", "รายชั่วโมง") and (not selected_day or timestamp.date() != selected_day):
                continue
            if period == "รายชั่วโมง" and selected_hour != "ทุกชั่วโมง":
                if not selected_hour[:2].isdigit() or timestamp.hour != int(selected_hour[:2]):
                    continue
            values = (cpu, ram, disk)
            if all(math.isfinite(float(value)) for value in values):
                period_rows.append((timestamp, tuple(max(0.0, min(100.0, float(value))) for value in values)))
        rows = [values for _timestamp, values in period_rows]
        if not rows:
            period_title = {"ภาพรวมทั้งหมด": "ภาพรวม", "รายเดือน": month_label,
                            "รายวัน": self.vertical_day_menu.get(),
                            "รายชั่วโมง": f"{self.vertical_day_menu.get()} {selected_hour}"}.get(period, period)
            self.vertical_data_label.configure(text=f"{period_title} · ไม่มีข้อมูล")
            self._draw_vertical_resource_chart({}, {})
            for key, (value_label, detail_label) in self._vertical_recommendation_labels.items():
                value_label.configure(text="ไม่มีข้อมูล")
                detail_label.configure(text="ไม่มีตัวอย่างในช่วงเวลานี้")
            return

        columns = list(zip(*rows))
        metric_keys = ("cpu", "ram", "disk")
        averages = {key: sum(values) / len(values) for key, values in zip(metric_keys, columns)}
        p95 = {key: self._percentile95(values) for key, values in zip(metric_keys, columns)}
        day_count = len({timestamp.date() for timestamp, _values in period_rows})
        period_title = {"ภาพรวมทั้งหมด": "ภาพรวมทั้งหมด", "รายเดือน": month_label,
                        "รายวัน": self.vertical_day_menu.get(),
                        "รายชั่วโมง": f"{self.vertical_day_menu.get()} {selected_hour}"}.get(period, period)
        self.vertical_data_label.configure(
            text=f"{period_title} · {len(rows):,} ตัวอย่าง · {day_count} วัน")
        self._draw_vertical_resource_chart(averages, p95)

        growth = 1.20
        targets = {"cpu": 0.70, "ram": 0.75, "disk": 0.80}
        cpu_cores = max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 1))
        cpu_required = math.ceil(cpu_cores * (p95["cpu"] / 100.0) * growth / targets["cpu"])
        cpu_add = max(0, cpu_required - cpu_cores)
        cpu_value, cpu_detail = self._vertical_recommendation_labels["cpu"]
        cpu_value.configure(text=f"+{cpu_add} logical core" + ("s" if cpu_add != 1 else "")
                            if cpu_add else "ยังไม่ต้องเพิ่ม")
        cpu_detail.configure(text=f"ปัจจุบัน {cpu_cores} cores · P95 {p95['cpu']:.1f}%"
                             + (f" · รวมแนะนำ {cpu_required}" if cpu_add else ""))

        try:
            ram_current = psutil.virtual_memory().total / (1024 ** 3)
            ram_required = ram_current * (p95["ram"] / 100.0) * growth / targets["ram"]
            ram_total = max(math.ceil(ram_current), math.ceil(ram_required))
            ram_add = max(0, ram_total - math.ceil(ram_current))
            ram_value, ram_detail = self._vertical_recommendation_labels["ram"]
            ram_value.configure(text=f"+{ram_add} GiB RAM" if ram_add else "ยังไม่ต้องเพิ่ม")
            ram_detail.configure(text=f"ปัจจุบัน {ram_current:.1f} GiB · P95 {p95['ram']:.1f}%"
                                 + (f" · รวมแนะนำ {ram_total} GiB" if ram_add else ""))
        except Exception:
            ram_value, ram_detail = self._vertical_recommendation_labels["ram"]
            ram_value.configure(text="อ่านขนาด RAM ไม่ได้")
            ram_detail.configure(text=f"P95 {p95['ram']:.1f}%")

        try:
            root_path = "C:\\" if os.name == "nt" else "/"
            disk_current = psutil.disk_usage(root_path).total / (1024 ** 3)
            disk_required = disk_current * (p95["disk"] / 100.0) * growth / targets["disk"]
            disk_total = max(math.ceil(disk_current), math.ceil(disk_required))
            disk_add = max(0, disk_total - math.ceil(disk_current))
            disk_value, disk_detail = self._vertical_recommendation_labels["disk"]
            disk_value.configure(text=f"+{disk_add} GiB Disk" if disk_add else "ยังไม่ต้องเพิ่ม")
            disk_detail.configure(text=f"พาร์ทิชันระบบ {disk_current:.1f} GiB · P95 {p95['disk']:.1f}%"
                                  + (f" · รวมแนะนำ {disk_total} GiB" if disk_add else ""))
        except Exception:
            disk_value, disk_detail = self._vertical_recommendation_labels["disk"]
            disk_value.configure(text="อ่านขนาด Disk ไม่ได้")
            disk_detail.configure(text=f"P95 {p95['disk']:.1f}%")

    def _export_vertical_chart_pdf(self):
        """Export the selected chart, recommendations, and every matching CSV sample."""
        if getattr(self, "_vertical_pdf_export_active", False):
            return
        period = self.vertical_period_menu.get()
        month_label = self.vertical_month_menu.get()
        month_key = self._vertical_month_map.get(month_label)
        selected_day = self._vertical_day_map.get(self.vertical_day_menu.get())
        selected_hour = self.vertical_hour_menu.get()
        rows = []
        for timestamp, cpu, ram, disk in getattr(self, "_history_rows", []):
            if period != "ภาพรวมทั้งหมด" and (not month_key or timestamp.strftime("%Y-%m") != month_key):
                continue
            if period in ("รายวัน", "รายชั่วโมง") and (not selected_day or timestamp.date() != selected_day):
                continue
            if period == "รายชั่วโมง" and selected_hour != "ทุกชั่วโมง":
                if not selected_hour[:2].isdigit() or timestamp.hour != int(selected_hour[:2]):
                    continue
            values = (float(cpu), float(ram), float(disk))
            if all(math.isfinite(value) for value in values):
                rows.append((timestamp, tuple(max(0.0, min(100.0, value)) for value in values)))

        rows.sort(key=lambda row: row[0])

        if not rows:
            self.vertical_export_status.configure(text="ไม่มีข้อมูลในช่วงเวลานี้", text_color=Theme.WARNING)
            return

        period_title = {"ภาพรวมทั้งหมด": "ภาพรวมทั้งหมด", "รายเดือน": month_label,
                        "รายวัน": self.vertical_day_menu.get(),
                        "รายชั่วโมง": f"{self.vertical_day_menu.get()} {selected_hour}"}.get(period, period)
        columns = list(zip(*(values for _timestamp, values in rows)))
        metric_keys = ("cpu", "ram", "disk")
        averages = {key: sum(values) / len(values) for key, values in zip(metric_keys, columns)}
        p95 = {key: self._percentile95(values) for key, values in zip(metric_keys, columns)}
        minimums = {key: min(values) for key, values in zip(metric_keys, columns)}
        maximums = {key: max(values) for key, values in zip(metric_keys, columns)}
        buckets = self._aggregate_vertical_chart_rows(rows, period)
        recommendations = {
            key: {"suggestion": value_label.cget("text"), "detail": detail_label.cget("text")}
            for key, (value_label, detail_label) in self._vertical_recommendation_labels.items()
        }
        snapshot = {"title": period_title, "rows": rows, "averages": averages,
                    "p95": p95, "minimums": minimums, "maximums": maximums,
                    "recommendations": recommendations, "buckets": buckets,
                    "period": period,
                    "bucket_interval": {"ภาพรวมทั้งหมด": "รายวัน", "รายเดือน": "รายวัน",
                                        "รายวัน": "รายชั่วโมง", "รายชั่วโมง": "ทุก 5 นาที"}.get(period, "รายวัน"),
                    "generated": datetime.now()}

        default_name = f"resource_report_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        destination = filedialog.asksaveasfilename(
            title="บันทึกรายงาน Vertical Chart เป็น PDF",
            defaultextension=".pdf", initialfile=default_name,
            filetypes=[("PDF document", "*.pdf")])
        if not destination:
            return

        self._vertical_pdf_export_active = True
        self.vertical_export_button.configure(state="disabled")
        self.vertical_export_status.configure(text="กำลังสร้าง PDF...", text_color=Theme.TEXT_MUTED)
        threading.Thread(target=self._write_vertical_chart_pdf,
                         args=(destination, snapshot), daemon=True).start()

    def _write_vertical_chart_pdf(self, destination, report):
        try:
            self._create_vertical_chart_pdf(destination, report)
            message = (True, f"บันทึกรายงานแล้ว: {os.path.basename(destination)} "
                             f"({len(report['rows']):,} ตัวอย่าง สรุปเป็น {len(report['buckets']):,} ช่วง)")
        except Exception as exc:
            message = (False, f"สร้าง PDF ไม่สำเร็จ: {exc}")
        self._safe_after(0, self._finish_vertical_pdf_export, *message)

    def _finish_vertical_pdf_export(self, succeeded, message):
        self._vertical_pdf_export_active = False
        self.vertical_export_button.configure(state="normal")
        self.vertical_export_status.configure(
            text=message, text_color=Theme.SUCCESS if succeeded else Theme.DANGER)

    @staticmethod
    def _aggregate_vertical_chart_rows(rows, period):
        """Roll high-frequency samples up to day, hour, or five-minute buckets."""
        grouped = {}
        for timestamp, values in rows:
            if period in ("ภาพรวมทั้งหมด", "รายเดือน"):
                bucket_time = datetime.combine(timestamp.date(), datetime.min.time())
            elif period == "รายวัน":
                bucket_time = timestamp.replace(minute=0, second=0, microsecond=0)
            else:
                bucket_time = timestamp.replace(minute=(timestamp.minute // 5) * 5,
                                                second=0, microsecond=0)
            grouped.setdefault(bucket_time, []).append(values)

        buckets = []
        for bucket_time, samples in sorted(grouped.items()):
            averages = tuple(sum(sample[index] for sample in samples) / len(samples)
                             for index in range(3))
            peaks = tuple(max(sample[index] for sample in samples) for index in range(3))
            buckets.append((bucket_time, averages, peaks, len(samples)))
        return buckets

    @staticmethod
    def _create_vertical_chart_pdf(destination, report):
        """Render a complete PDF report and atomically publish it at the chosen path."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas as pdf_canvas

        target = os.path.abspath(destination)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".resource-report-", suffix=".pdf",
                                         dir=os.path.dirname(target))
        os.close(fd)
        try:
            font_name = "Helvetica"
            font_candidates = (
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "tahoma.ttf"),
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "LeelawUI.ttf"),
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf"),
            )
            font_path = next((path for path in font_candidates if os.path.isfile(path)), None)
            if not font_path:
                raise RuntimeError("ไม่พบฟอนต์ภาษาไทยใน Windows (Tahoma/Leelawadee/Segoe UI)")
            try:
                pdfmetrics.getFont("ResourceThai")
            except KeyError:
                pdfmetrics.registerFont(TTFont("ResourceThai", font_path))
            font_name = "ResourceThai"

            page_width, page_height = landscape(A4)
            page_count = 2 + math.ceil(len(report["buckets"]) / 35)
            pdf = pdf_canvas.Canvas(temporary, pagesize=(page_width, page_height), pageCompression=1)
            pdf.setTitle(f"Resource performance report - {report['title']}")
            pdf.setAuthor("Intelligent Server Operations Platform")

            def text(value, x, y, size=9, color="#253446"):
                pdf.setFillColor(colors.HexColor(color))
                pdf.setFont(font_name, size)
                pdf.drawString(x, y, str(value))

            def wrapped_text(value, x, y, max_width, size=8, color="#35485C"):
                pdf.setFillColor(colors.HexColor(color))
                pdf.setFont(font_name, size)
                words = str(value).split()
                lines = []
                line = ""
                for word in words:
                    candidate = f"{line} {word}".strip()
                    if line and pdfmetrics.stringWidth(candidate, font_name, size) > max_width:
                        lines.append(line)
                        line = word
                    else:
                        line = candidate
                if line:
                    lines.append(line)
                for line in lines:
                    pdf.drawString(x, y, line)
                    y -= size + 2
                return y

            def footer(page_number):
                pdf.setStrokeColor(colors.HexColor("#D7E0E8"))
                pdf.line(34, 28, page_width - 34, 28)
                text(f"Intelligent Server Operations Platform  |  {page_number}/{page_count}",
                     36, 16, 7, "#667789")

            # Overview, summary table, chart, and sizing recommendations.
            text("Resource Performance Report / รายงานการใช้ทรัพยากร", 36, page_height - 38,
                 17, "#13243A")
            text(f"ช่วงข้อมูล: {report['title']}", 36, page_height - 61, 10, "#087E75")
            rows = report["rows"]
            first_time, last_time = rows[0][0], rows[-1][0]
            text(f"สร้างเมื่อ {report['generated']:%Y-%m-%d %H:%M:%S}    "
                 f"จำนวนตัวอย่าง {len(rows):,}    ตั้งแต่ {first_time:%Y-%m-%d %H:%M:%S} "
                 f"ถึง {last_time:%Y-%m-%d %H:%M:%S}", 36, page_height - 80, 8, "#526477")

            table_top = page_height - 108
            table_x = (36, 225, 333, 441, 549, 657)
            headers = ("ทรัพยากร", "ต่ำสุด", "เฉลี่ย", "P95", "สูงสุด")
            pdf.setFillColor(colors.HexColor("#EAF0F5"))
            pdf.roundRect(34, table_top - 22, page_width - 68, 25, 4, fill=1, stroke=0)
            for index, header in enumerate(headers):
                text(header, table_x[index], table_top - 14, 9, "#253446")
            for index, (key, label) in enumerate((("cpu", "CPU"), ("ram", "RAM"), ("disk", "Disk"))):
                y = table_top - 42 - index * 20
                text(label, table_x[0], y, 9, "#253446")
                for column, field in enumerate(("minimums", "averages", "p95", "maximums"), start=1):
                    text(f"{report[field][key]:.1f}%", table_x[column], y, 9, "#253446")

            # The bar chart mirrors the app's vertical chart: averages as bars and P95 as markers.
            chart_left, chart_right = 72, page_width - 42
            chart_bottom, chart_top = 211, 354
            chart_height = chart_top - chart_bottom
            text("Resource average / P95", 36, chart_top + 16, 11, "#13243A")
            text("แท่ง = ค่าเฉลี่ย   เส้นประ = P95", page_width - 205, chart_top + 17, 8, "#526477")
            for level in (0, 25, 50, 75, 100):
                y = chart_bottom + chart_height * level / 100
                pdf.setStrokeColor(colors.HexColor("#DCE4EB"))
                pdf.setDash(2, 3)
                pdf.line(chart_left, y, chart_right, y)
                pdf.setDash()
                text(str(level), 47, y - 3, 7, "#667789")
            slots = (("cpu", "CPU", "#2F80ED"), ("ram", "RAM", "#27AE60"),
                     ("disk", "Disk", "#F2A900"))
            slot_width = (chart_right - chart_left) / 3
            bar_width = min(78, slot_width * 0.32)
            for index, (key, label, color) in enumerate(slots):
                center = chart_left + slot_width * (index + 0.5)
                average = report["averages"][key]
                bar_height = chart_height * average / 100
                pdf.setFillColor(colors.HexColor(color))
                pdf.roundRect(center - bar_width / 2, chart_bottom, bar_width, max(1, bar_height), 3,
                              fill=1, stroke=0)
                percentile95 = report["p95"][key]
                y95 = chart_bottom + chart_height * percentile95 / 100
                pdf.setStrokeColor(colors.HexColor("#34495E"))
                pdf.setLineWidth(2)
                pdf.setDash(4, 2)
                pdf.line(center - bar_width / 2 - 6, y95, center + bar_width / 2 + 6, y95)
                pdf.setDash()
                text(f"Avg {average:.1f}%", center - 31, max(chart_bottom + 5, chart_bottom + bar_height + 5),
                     8, "#253446")
                text(label, center - 12, chart_bottom - 16, 9, "#253446")

            text("คำแนะนำการเพิ่มทรัพยากรจากช่วงที่เลือก", 36, 184, 11, "#13243A")
            recommendation_y = 164
            for index, key in enumerate(("cpu", "ram", "disk")):
                recommendation = report["recommendations"].get(key, {})
                recommendation_y = wrapped_text(
                    f"{key.upper()}: {recommendation.get('suggestion', 'ไม่มีข้อมูล')}  "
                    f"{recommendation.get('detail', '')}", 40, recommendation_y,
                    page_width - 78, 8, "#35485C") - 2
            text("คำแนะนำคำนวณจาก P95 และเผื่อการเติบโต 20% เพื่อเป็นข้อมูลประกอบการวางแผน "
                 "ไม่ได้เปลี่ยนทรัพยากรให้อัตโนมัติ", 36, 105, 7, "#667789")
            text("หน้าถัดไปเป็นกราฟแนวโน้มจาก Historical Graph และตารางค่าเฉลี่ยที่สรุปจากข้อมูลดิบ",
                 36, 91, 7, "#667789")
            footer(1)
            pdf.showPage()

            # Historical Graph trend: solid lines show bucket averages; dashed lines show peaks.
            text("Historical Graph / แนวโน้มการใช้ทรัพยากร", 36, page_height - 38,
                 15, "#13243A")
            text(f"ช่วงข้อมูล: {report['title']}  ·  สรุปค่าเฉลี่ย/{report['bucket_interval']} และค่าสูงสุด",
                 36, page_height - 59, 8, "#526477")
            trend = report["buckets"]
            plot_left, plot_right = 62, page_width - 42
            plot_bottom, plot_top = 92, page_height - 115
            plot_height = plot_top - plot_bottom
            for level in (0, 25, 50, 75, 100):
                y = plot_bottom + plot_height * level / 100
                pdf.setStrokeColor(colors.HexColor("#DCE4EB"))
                pdf.setDash(2, 3)
                pdf.line(plot_left, y, plot_right, y)
                pdf.setDash()
                text(f"{level}%", 30, y - 3, 7, "#667789")

            def x_for(index):
                if len(trend) <= 1:
                    return (plot_left + plot_right) / 2
                return plot_left + index * (plot_right - plot_left) / (len(trend) - 1)

            def y_for(value):
                return plot_bottom + plot_height * max(0.0, min(100.0, value)) / 100

            metric_styles = ((0, "CPU", "#2F80ED"), (1, "RAM", "#27AE60"),
                             (2, "Disk", "#F2A900"))
            for metric_index, _label, color in metric_styles:
                for series_index, series_name in ((1, "average"), (2, "peak")):
                    points = [(x_for(index), y_for(bucket[series_index][metric_index]))
                              for index, bucket in enumerate(trend)]
                    pdf.setStrokeColor(colors.HexColor(color))
                    pdf.setLineWidth(2 if series_name == "average" else 1)
                    if series_name == "average":
                        pdf.setDash()
                    else:
                        pdf.setDash(4, 3)
                    for left_point, right_point in zip(points, points[1:]):
                        pdf.line(left_point[0], left_point[1], right_point[0], right_point[1])
                    pdf.setDash()
                    if series_name == "peak":
                        pdf.setFillColor(colors.HexColor(color))
                        for x, y in points:
                            pdf.circle(x, y, 2, fill=1, stroke=0)
                    elif len(points) == 1:
                        pdf.setFillColor(colors.HexColor(color))
                        pdf.circle(points[0][0], points[0][1], 3, fill=1, stroke=0)

            label_step = max(1, len(trend) // 10)
            for index, (bucket_time, _averages, _peaks, _count) in enumerate(trend):
                if index % label_step == 0 or index == len(trend) - 1:
                    label = bucket_time.strftime("%Y-%m-%d" if report["period"] in
                                                 ("ภาพรวมทั้งหมด", "รายเดือน") else "%H:%M")
                    text(label, x_for(index) - 20, plot_bottom - 18, 7, "#667789")

            legend_y = page_height - 83
            legend_x = 54
            for metric_index, label, color in metric_styles:
                pdf.setStrokeColor(colors.HexColor(color))
                pdf.setLineWidth(2)
                pdf.line(legend_x, legend_y, legend_x + 18, legend_y)
                pdf.setDash(4, 3)
                pdf.line(legend_x + 22, legend_y, legend_x + 40, legend_y)
                pdf.setDash()
                text(f"{label}  เฉลี่ย / พีค", legend_x + 45, legend_y - 3, 8, color)
                legend_x += 185
            text("จำนวนตัวอย่างดิบ: " + f"{len(report['rows']):,}  ·  สรุปเป็น {len(trend):,} ช่วง",
                 36, 56, 8, "#526477")
            footer(2)
            pdf.showPage()

            # Keep every period in the report, but show averaged/peak buckets instead of raw samples.
            chunk_size = 35
            for page_index, start in enumerate(range(0, len(trend), chunk_size), start=3):
                text("Averaged historical data / ข้อมูลสรุปตามช่วงเวลา", 36, page_height - 38,
                     15, "#13243A")
                text(f"ช่วงข้อมูล: {report['title']}  ·  เฉลี่ย{report['bucket_interval']}  ·  "
                     f"{len(rows):,} ตัวอย่างดิบ สรุปเป็น {len(trend):,} ช่วง",
                     36, page_height - 59, 8, "#526477")
                pdf.setFillColor(colors.HexColor("#EAF0F5"))
                pdf.roundRect(34, page_height - 88, page_width - 68, 22, 4, fill=1, stroke=0)
                columns = ((38, "ช่วงเวลา"), (195, "จำนวน"), (260, "CPU เฉลี่ย"),
                           (332, "CPU พีค"), (403, "RAM เฉลี่ย"), (475, "RAM พีค"),
                           (546, "Disk เฉลี่ย"), (621, "Disk พีค"))
                for x, heading in columns:
                    text(heading, x, page_height - 81, 8, "#253446")
                y = page_height - 105
                for bucket_time, averages, peaks, sample_count in trend[start:start + chunk_size]:
                    label = bucket_time.strftime("%Y-%m-%d" if report["period"] in
                                                 ("ภาพรวมทั้งหมด", "รายเดือน") else "%Y-%m-%d %H:%M")
                    text(label, 38, y, 8, "#253446")
                    text(f"{sample_count:,}", 195, y, 8, "#253446")
                    for x, value in zip((260, 332, 403, 475, 546, 621),
                                        (averages[0], peaks[0], averages[1], peaks[1],
                                         averages[2], peaks[2])):
                        text(f"{value:.1f}%", x, y, 8, "#253446")
                    pdf.setStrokeColor(colors.HexColor("#E4EAF0"))
                    pdf.setLineWidth(0.4)
                    pdf.line(38, y - 4, page_width - 38, y - 4)
                    y -= 13
                footer(page_index)
                if start + chunk_size < len(trend):
                    pdf.showPage()
            pdf.save()
            os.replace(temporary, target)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

    def _draw_vertical_resource_chart(self, averages: dict, p95: dict):
        canvas = getattr(self, "vertical_chart_canvas", None)
        if canvas is None:
            return
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        if not averages:
            canvas.create_text(width / 2, height / 2, text="เลือกช่วงเวลาที่มีข้อมูลเพื่อแสดงกราฟ",
                               fill=Theme.TEXT_MUTED, font=("Segoe UI", 12))
            return

        left, right, top, bottom = 52, max(60, width - 24), 28, max(50, height - 36)
        plot_h, plot_w = bottom - top, right - left
        y_at = lambda value: bottom - (max(0.0, min(100.0, float(value))) / 100.0) * plot_h
        for level in (0, 25, 50, 75, 100):
            y = y_at(level)
            canvas.create_line(left, y, right, y, fill=Theme.BORDER, dash=(2, 4))
            canvas.create_text(left - 8, y, text=str(level), fill=Theme.TEXT_MUTED,
                               font=("Segoe UI", 9), anchor="e")

        slots = (("cpu", "CPU", Theme.ACCENT), ("ram", "RAM", Theme.SUCCESS),
                 ("disk", "Disk", Theme.WARNING))
        slot_w = plot_w / len(slots)
        bar_w = min(76, slot_w * 0.36)
        for index, (key, label, color) in enumerate(slots):
            center = left + slot_w * (index + 0.5)
            average_y = y_at(averages[key])
            canvas.create_rectangle(center - bar_w / 2, average_y, center + bar_w / 2, bottom,
                                    fill=color, outline="")
            canvas.create_text(center, average_y - 12, text=f"{averages[key]:.1f}%",
                               fill=Theme.TEXT_PRIMARY, font=("Segoe UI", 10, "bold"))
            p95_y = y_at(p95[key])
            canvas.create_line(center - bar_w / 2 - 5, p95_y, center + bar_w / 2 + 5, p95_y,
                               fill="#ffffff", width=2, dash=(4, 2))
            canvas.create_text(center, bottom + 16, text=label,
                               fill=Theme.TEXT_SECONDARY, font=("Segoe UI", 11, "bold"))
    def build_server_dashboard(self):
        tab = self.tab_server_dashboard

        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=8)
        ctk.CTkLabel(
            top,
            text="ดู CPU / RAM / Disk แยกตามแต่ละ server และเปิด Terminal ดู log แบบเรียลไทม์ได้ที่นี่",
            font=("Segoe UI", 12), text_color=Theme.TEXT_MUTED).pack(side="left", padx=10)

        self.server_dash_frame = ctk.CTkScrollableFrame(tab, height=580)
        self.server_dash_frame.pack(pady=5, fill="both", expand=True)

        self._server_dash_empty_label = ctk.CTkLabel(
            self.server_dash_frame,
            text="ยังไม่มี server ให้แสดง — ไปที่แท็บ 🖥️ Server Manager แล้ว Import YAML / Docker Compose ก่อน",
            text_color=Theme.TEXT_MUTED, font=("Segoe UI", 13))
        self._server_dash_empty_label.pack(pady=30)

    def add_server_dashboard_card(self, name, stype, ip="", yaml_key=None):
        """สร้างการ์ดแสดง CPU/RAM/Disk/Terminal ของ server หนึ่งตัวในหน้า Server Dashboard"""
        if name in self._server_dash_widgets:
            return  # มีการ์ดของ server นี้อยู่แล้ว (เช่นตอน Refresh All Status) ไม่ต้องสร้างซ้ำ

        if len(self._server_dash_widgets) == 0:
            self._server_dash_empty_label.pack_forget()

        card = ctk.CTkFrame(self.server_dash_frame, fg_color=Theme.BG_CARD, border_width=1, border_color=Theme.BORDER)
        card.pack(fill="x", pady=4, padx=6)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 4))
        icon = "📦" if stype == "docker" else "🖥️"
        ctk.CTkLabel(header, text=f"{icon} {name}" + (f" ({ip})" if ip else ""),
                     font=("Segoe UI", 14, "bold")).pack(side="left")
        status_label = ctk.CTkLabel(header, text="⚫ Offline", text_color=Theme.TEXT_MUTED,
                                    font=("Segoe UI", 12, "bold"))
        status_label.pack(side="left", padx=10)
        ping_label = ctk.CTkLabel(header, text="Ping: กำลังตรวจ...",
                                  text_color=Theme.TEXT_SECONDARY,
                                  font=("Segoe UI", 11, "bold"))
        ping_label.pack(side="right", padx=4)

        # ── แสดงค่า CPU/RAM Limit ที่ตั้งไว้ในแท็บ Server Manager (⚙️ Limits) ──
        limit_label = ctk.CTkLabel(card, text="", font=("Segoe UI", 11),
                                   text_color=Theme.TEXT_ACCENT, anchor="w")
        limit_label.pack(fill="x", padx=10, pady=(0, 4))

        widgets = {"card": card, "status": status_label, "ping_label": ping_label, "term": None,
                  "cpu_label": None, "cpu_bar": None, "ram_label": None,
                  "ram_bar": None, "disk_label": None, "cmd_entry": None,
                  "limit_label": limit_label, "remote_metrics_label": None}

        if stype == "docker":
            metrics = ctk.CTkFrame(card, fg_color="transparent")
            metrics.pack(fill="x", padx=10, pady=4)
            metrics.grid_columnconfigure((0, 1, 2), weight=1)

            def metric_col(col, label_text, color):
                f = ctk.CTkFrame(metrics, fg_color="transparent")
                f.grid(row=0, column=col, sticky="ew", padx=4)
                ctk.CTkLabel(f, text=label_text, font=("Segoe UI", 11),
                             text_color=Theme.TEXT_SECONDARY).pack(anchor="w")
                val = ctk.CTkLabel(f, text="—", font=("Segoe UI", 13, "bold"), text_color=color)
                val.pack(anchor="w")
                bar = ctk.CTkProgressBar(f, progress_color=color, height=8)
                bar.set(0)
                bar.pack(fill="x", pady=(2, 0))
                return val, bar

            widgets["cpu_label"], widgets["cpu_bar"] = metric_col(0, "🖥️ CPU", Theme.ACCENT)
            widgets["ram_label"], widgets["ram_bar"] = metric_col(1, "🧠 RAM", Theme.SUCCESS)
            widgets["disk_label"], disk_bar = metric_col(2, "💾 Disk (ขนาดที่ใช้จริง)", Theme.WARNING)
            disk_bar.pack_forget()   # ขนาด disk เป็นค่าสัมบูรณ์ ไม่ใช่ % เลยไม่ต้องมี bar

            term_box = ctk.CTkTextbox(card, height=160, font=("Consolas", 11),
                                      fg_color=Theme.BG_TERMINAL, text_color=Theme.TEXT_ACCENT)
            term_box.insert("end", "— ยังไม่มี log (server offline หรือยังไม่เริ่มสตรีม) —\n")
            term_box.configure(state="disabled")
            widgets["term"] = term_box

            # ── ช่องพิมพ์คำสั่ง cmd รันข้างใน container นี้ (docker exec) ──
            cmd_row = ctk.CTkFrame(card, fg_color="transparent")
            ctk.CTkLabel(cmd_row, text=f"{name} $", font=("Consolas", 12, "bold"),
                         text_color=Theme.WARNING).pack(side="left", padx=(0, 6))
            cmd_entry = ctk.CTkEntry(cmd_row, font=("Consolas", 12),
                                     placeholder_text="พิมพ์คำสั่งรันใน container นี้ เช่น ls -la แล้วกด Enter")
            cmd_entry.pack(side="left", padx=(0, 6), fill="x", expand=True)
            cmd_entry.bind("<Return>", lambda e, n=name: self._run_container_cmd(n))
            ctk.CTkButton(cmd_row, text="▶ Run", width=60,
                          fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER,
                          command=lambda n=name: self._run_container_cmd(n)).pack(side="left")
            self._setup_context_menu(cmd_entry._entry, is_entry=True)
            widgets["cmd_entry"] = cmd_entry
            widgets["cmd_history"] = []
            # ── ↑/↓ ทวนคำสั่งที่เคยรันใน container นี้, Tab เดา/วนดูคำสั่ง shell พื้นฐาน ──
            self._attach_shell_navigation(
                cmd_entry,
                lambda n=name: self._server_dash_widgets.get(n, {}).get("cmd_history", []),
                CONTAINER_SHELL_SUGGESTIONS,
            )

            term_visible = ctk.BooleanVar(value=False)

            def toggle_term():
                if term_visible.get():
                    term_box.pack_forget()
                    cmd_row.pack_forget()
                    term_btn.configure(text="💻 แสดง Terminal")
                    term_visible.set(False)
                else:
                    term_box.pack(fill="x", padx=10, pady=(0, 6))
                    cmd_row.pack(fill="x", padx=10, pady=(0, 10))
                    term_btn.configure(text="🔼 ซ่อน Terminal")
                    term_visible.set(True)

            term_btn = ctk.CTkButton(card, text="💻 แสดง Terminal", width=140,
                                     fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                                     command=toggle_term)
            term_btn.pack(anchor="w", padx=10, pady=(0, 8))

            # ถ้า container กำลังรันอยู่แล้วตอนสร้างการ์ด ให้เริ่มสตรีม log ทันที
            status_text, status_color = self.get_real_status(name, stype, ip)
            status_label.configure(text=status_text, text_color=status_color)
            if "Online" in status_text:
                self.start_log_stream(name)
        else:
            ssh_profile = self._ssh_profiles.get(name)
            remote_metrics_label = ctk.CTkLabel(
                card,
                text=("กำลังอ่านข้อมูล SSH..." if ssh_profile else
                      "เพิ่ม SSH key ผ่านปุ่ม 🔐 SSH ใน Server Manager เพื่อดู CPU / RAM / Disk / Network ของเครื่องนี้"),
                font=("Segoe UI", 11), text_color=Theme.TEXT_MUTED,
                justify="left", anchor="w")
            remote_metrics_label.pack(padx=10, pady=(0, 10), anchor="w")
            widgets["remote_metrics_label"] = remote_metrics_label
            remote_console = ctk.CTkFrame(card, fg_color="transparent")
            remote_output = ctk.CTkTextbox(
                remote_console, height=120, font=("Consolas", 10),
                fg_color=Theme.BG_TERMINAL, text_color=Theme.TEXT_ACCENT)
            remote_output.insert("end", "SSH command console — ใช้คำสั่งแบบไม่ต้องโต้ตอบ\n")
            remote_output.configure(state="disabled")
            remote_output.pack(fill="x", pady=(0, 2))
            command_row = ctk.CTkFrame(remote_console, fg_color="transparent")
            command_entry = ctk.CTkEntry(
                command_row, font=("Consolas", 11),
                placeholder_text="พิมพ์คำสั่ง Linux เช่น uptime หรือ df -h")
            command_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
            command_entry.bind("<Return>", lambda _event, n=name: self._run_remote_ssh_command(n))
            ctk.CTkButton(
                command_row, text="▶ Run", width=75,
                command=lambda n=name: self._run_remote_ssh_command(n),
            ).pack(side="right")
            command_row.pack(fill="x", pady=(6, 0))
            ssh_console_btn = ctk.CTkButton(
                card, text="🔐 SSH Console", width=145,
                fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
            )

            def toggle_ssh_console(frame=remote_console, btn=ssh_console_btn):
                if frame.winfo_manager():
                    frame.pack_forget()
                    btn.configure(text="🔐 SSH Console")
                else:
                    frame.pack(fill="x", padx=10, pady=(0, 10))
                    btn.configure(text="🔼 ซ่อน SSH Console")

            ssh_console_btn.configure(command=toggle_ssh_console)
            ssh_console_btn.pack(anchor="w", padx=10, pady=(0, 8))
            widgets["remote_console"] = remote_output
            widgets["remote_cmd_entry"] = command_entry
            status_text, status_color = self.get_real_status(name, stype, ip)
            status_label.configure(text=status_text, text_color=status_color)

        self._server_dash_widgets[name] = widgets
        self._update_dash_limit_label(name)
        if yaml_key is not None:
            self.yaml_container_names.setdefault(yaml_key, []).append(name)

    def remove_server_dashboard_entries(self, yaml_key):
        """ลบการ์ดของทุก server ที่อยู่ใน yaml ตัวนี้ออกจาก Server Dashboard
           (เรียกพร้อมกับตอนกด Delete YAML ใน Server Manager)"""
        names = self.yaml_container_names.pop(yaml_key, [])
        for name in names:
            self.tracked_containers.pop(name, None)
            self._docker_stats_cache.pop(name, None)
            self._server_ping_cache.pop(name, None)
            self._server_manager_status_labels.pop(name, None)
            self._ssh_profiles.pop(name, None)
            self.stop_log_stream(name)
            widgets = self._server_dash_widgets.pop(name, None)
            if widgets and widgets["card"].winfo_exists():
                widgets["card"].destroy()
            # ── ปิด Quick Tunnel ของ server นี้ (ถ้ากำลังรันอยู่) แล้วเอา state ทิ้งไปเลย
            #    ไม่งั้นจะมี cloudflared ค้างรันอยู่เบื้องหลังทั้งที่ server ถูกลบไปแล้ว ──
            tstate = self._server_quick_tunnels.pop(name, None)
            if tstate is not None and tstate.get("proc") is not None:
                try:
                    tstate["proc"].terminate()
                except Exception:
                    pass
            if tstate is not None and tstate.get("dialog") is not None:
                try:
                    if tstate["dialog"].winfo_exists():
                        tstate["dialog"].destroy()
                except Exception:
                    pass
        self._save_tracked_containers()
        self._save_ssh_profiles()
        if not self._server_dash_widgets:
            self._server_dash_empty_label.pack(pady=30)

    # ── Per-container log streaming (Terminal ของ server) ──────
    def start_log_stream(self, name):
        if self._log_streams.get(name):
            return  # สตรีมอยู่แล้ว ไม่ต้องเปิดซ้ำ
        self._log_streams[name] = True
        threading.Thread(target=self._stream_logs, args=(name,), daemon=True).start()

    def stop_log_stream(self, name):
        self._log_streams[name] = False
        proc = self._log_procs.pop(name, None)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def _stream_logs(self, name):
        try:
            proc = proc_utils.popen_hidden(
                ["docker", "logs", "-f", "--tail", "100", name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            self._log_procs[name] = proc
            for line in proc.stdout:
                if not self._log_streams.get(name):
                    break
                self._safe_after(0, self._append_term_line, name, line)
            try:
                proc.stdout.close()
            except Exception:
                pass
        except Exception as e:
            self._safe_after(0, self._append_term_line, name, f"[Terminal] เปิด log ไม่ได้: {e}\n")
        finally:
            self._log_streams[name] = False
            self._log_procs.pop(name, None)

    def _append_term_line(self, name, line):
        widgets = self._server_dash_widgets.get(name)
        if not widgets:
            return
        tb = widgets.get("term")
        if tb is None or not tb.winfo_exists():
            return
        tb.configure(state="normal")
        tb.insert("end", line)
        # ตัดบรรทัดเก่าทิ้งถ้ายาวเกิน ~500 บรรทัด กัน RAM โตเรื่อยๆ ตอนสตรีมนานๆ (เสถียรขึ้น)
        last_line = int(tb.index("end-1c").split(".")[0])
        if last_line > 500:
            tb.delete("1.0", "2.0")
        tb.see("end")
        tb.configure(state="disabled")

    def _run_container_cmd(self, name):
        """อ่านคำสั่งจากช่อง cmd ของการ์ด server นี้ แล้วสั่งรันใน background thread (ไม่บล็อก UI)"""
        widgets = self._server_dash_widgets.get(name)
        if not widgets:
            return
        entry = widgets.get("cmd_entry")
        if entry is None or not entry.winfo_exists():
            return
        cmd = entry.get().strip()
        if not cmd:
            return
        entry.delete(0, "end")
        hist = widgets.setdefault("cmd_history", [])
        if not hist or hist[-1] != cmd:
            hist.append(cmd)
            if len(hist) > 200:
                hist.pop(0)
        self._append_term_line(name, f"\n$ {cmd}\n")
        threading.Thread(target=self._run_container_cmd_worker, args=(name, cmd), daemon=True).start()

    def _run_container_cmd_worker(self, name, cmd, timeout_sec=30):
        """รันคำสั่งข้างใน container ผ่าน docker exec — มี timeout กันค้างถ้าคำสั่งไม่ยอมจบ
           (เช่น พิมพ์คำสั่งที่รอ input ค้างอยู่) จะถูก kill อัตโนมัติแทนที่จะแฮงค์ไปเรื่อยๆ"""
        try:
            proc = proc_utils.popen_hidden(
                ["docker", "exec", name, "sh", "-c", cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False
            )
            try:
                out, _ = proc.communicate(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self._safe_after(0, self._append_term_line, name,
                          f"[Timeout] คำสั่งทำงานนานเกิน {timeout_sec}s ถูกยกเลิกอัตโนมัติ\n")
                return

            try:
                decoded = out.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    decoded = out.decode("cp874")
                except Exception:
                    decoded = out.decode("cp850", errors="replace")

            if not decoded:
                decoded = "[No output]\n"
            elif not decoded.endswith("\n"):
                decoded += "\n"
            self._safe_after(0, self._append_term_line, name, decoded)
        except FileNotFoundError:
            self._safe_after(0, self._append_term_line, name, "[Error] ไม่พบคำสั่ง docker บนเครื่องนี้\n")
        except Exception as e:
            self._safe_after(0, self._append_term_line, name, f"[Error] {e}\n")

    def _mark_server_dash_online(self, name, online: bool):
        """อัปเดตสถานะ Online/Offline ใน Server Dashboard ทันทีหลังกด Start/Stop/Restart
           (ไม่ต้องรอรอบ poll cache 3 วินาทีถัดไป)"""
        cache_entry = self._docker_stats_cache.get(name, {
            "cpu_pct": 0.0, "mem_usage": "—", "mem_pct": 0.0, "disk_str": "—"
        })
        cache_entry["online"] = online
        self._docker_stats_cache[name] = cache_entry
        self._safe_after(0, self._refresh_server_dashboard_ui)

    # ── Background polling: ดึง CPU/RAM/Disk ของทุก container ──
    def _docker_stats_poll_loop(self):
        while not self._background_stop.is_set():
            try:
                self._refresh_docker_stats_cache()
            except Exception as e:
                print(f"[Server Dashboard] stats poll error: {e}")
            if self._background_stop.wait(3):
                break

    def _server_ping_poll_loop(self):
        """วัด latency ของ server แยกใน background เพื่อไม่ให้หน้า UI ค้าง"""
        while not self._background_stop.is_set():
            servers = list(self.tracked_containers.items())
            if servers:
                try:
                    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
                        futures = {
                            pool.submit(self._measure_dashboard_ping, name, info): name
                            for name, info in servers
                        }
                        for future in as_completed(futures):
                            name = futures[future]
                            previous_ping = self._server_ping_cache.get(name)
                            try:
                                ping_result = future.result()
                                self._server_ping_cache[name] = ping_result
                                if ping_result.get("ssh_metrics"):
                                    self._persist_remote_server_metrics(
                                        name, ping_result["ssh_metrics"])
                            except Exception as exc:
                                print(f"[Server Dashboard] ping error for {name}: {exc}")
                                ping_result = {
                                    "online": False, "latency_ms": None,
                                    "method": "unavailable", "checked_at": time.time(),
                                }
                                self._server_ping_cache[name] = ping_result
                            if (previous_ping is not None
                                    and bool(previous_ping.get("online"))
                                    != bool(ping_result.get("online"))):
                                current_state = "online" if ping_result.get("online") else "offline"
                                previous_state = "online" if previous_ping.get("online") else "offline"
                                icon = "🟢" if current_state == "online" else "🔴"
                                message = (f"{icon} Server probe: {name} เปลี่ยนสถานะ "
                                           f"{previous_state} → {current_state}")
                                try:
                                    web_server.web_state.add_event(
                                        message, category="server_connectivity",
                                        details={"server": name, "previous": previous_state,
                                                 "current": current_state,
                                                 "latency_ms": ping_result.get("latency_ms"),
                                                 "method": ping_result.get("method", "unknown")},
                                        outcome=current_state)
                                except Exception:
                                    pass
                            info = self.tracked_containers.get(name, {})
                            stats = self._docker_stats_cache.get(name, {})
                            remote = ping_result.get("ssh_metrics") or {}
                            self.collector.save_server_health_sample(
                                name, stype=info.get("stype", "unknown"),
                                online=ping_result.get("online", False),
                                latency_ms=ping_result.get("latency_ms"),
                                method=ping_result.get("method", "unknown"),
                                health=stats.get("health", "unknown"),
                                cpu_pct=remote.get("cpu_pct", stats.get("cpu_pct")),
                                ram_pct=remote.get("ram_pct", stats.get("mem_pct")))
                            if self.tracked_containers.get(name, {}).get("stype") == "server":
                                self._safe_after(0, self._sync_server_manager_status,
                                                 name, ping_result.get("online", False))
                    self._safe_after(0, self._refresh_server_dashboard_ui)
                except Exception as exc:
                    print(f"[Server Dashboard] ping poll error: {exc}")
            if self._background_stop.wait(15):
                break

    def _persist_remote_server_metrics(self, name: str, metrics: dict):
        """Append timestamped Linux host telemetry for history and later evaluation."""
        fields = ["timestamp", "server", "cpu_pct", "ram_pct", "ram_used_mb", "ram_total_mb",
                  "disk_pct", "disk_used_gb", "disk_total_gb", "disk_read_mb_s",
                  "disk_write_mb_s", "net_rx_mb_s", "net_tx_mb_s", "load_1m",
                  "process_count", "uptime_sec"]
        row = {key: metrics.get(key, "") for key in fields if key not in ("timestamp", "server")}
        row.update({"timestamp": datetime.now().isoformat(timespec="seconds"), "server": name})
        try:
            with self._remote_metrics_lock:
                exists = (os.path.isfile(self._remote_metrics_file)
                          and os.path.getsize(self._remote_metrics_file) > 0)
                if exists and os.path.getsize(self._remote_metrics_file) > 25 * 1024 * 1024:
                    with open(self._remote_metrics_file, "r", newline="", encoding="utf-8") as source:
                        recent_rows = list(csv.DictReader(source))[-50000:]
                    fd, temporary = tempfile.mkstemp(
                        prefix=".remote-metrics-", suffix=".csv",
                        dir=os.path.dirname(self._remote_metrics_file))
                    try:
                        with os.fdopen(fd, "w", newline="", encoding="utf-8") as target:
                            writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
                            writer.writeheader()
                            writer.writerows(recent_rows)
                        os.replace(temporary, self._remote_metrics_file)
                    except Exception:
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
                        raise
                with open(self._remote_metrics_file, "a", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                    if not exists:
                        writer.writeheader()
                    writer.writerow(row)
        except OSError as exc:
            print(f"[SSH metrics] save failed for {name}: {exc}")

    def _measure_dashboard_ping(self, name, info):
        """คืนผล ping/SSH probe หนึ่ง server; งาน network ทำใน worker เท่านั้น"""
        stype = info.get("stype")
        ip = (info.get("ip") or "").strip()
        if stype == "docker":
            stats = self._docker_stats_cache.get(name, {})
            if not stats.get("online"):
                return {"online": False, "latency_ms": None,
                        "method": "ping", "checked_at": time.time()}
            container_ip = self._get_docker_container_ip(name)
            if container_ip:
                ok, latency = self._ping_host(container_ip)
                if ok:
                    return {"online": True, "latency_ms": latency,
                            "method": "ping", "checked_at": time.time()}
            # Docker Desktop/Windows บางชุดไม่เปิด ICMP ไปยัง network namespace
            # ของ container; ใช้เวลา round-trip ของ docker exec และระบุวิธีให้ชัดเจน
            ok, latency = self._docker_exec_ping(name)
            return {"online": ok, "latency_ms": latency,
                    "method": "exec", "checked_at": time.time()}

        if not ip:
            return {"online": False, "latency_ms": None,
                    "method": "unavailable", "checked_at": time.time()}
        ssh_metrics = None
        ssh_latency = None
        ssh_error = None
        profile = self._ssh_profiles.get(name) or info.get("ssh")
        if profile:
            ssh_metrics, ssh_latency, ssh_error = self._collect_remote_ssh_metrics(ip, profile)
        ping_ok, ping_latency = self._ping_host(ip)
        online = ping_ok or ssh_metrics is not None
        return {"online": online, "latency_ms": ping_latency if ping_ok else ssh_latency,
                "method": "ping" if ping_ok else ("ssh" if ssh_metrics is not None else "ping"),
                "ssh_metrics": ssh_metrics, "ssh_error": ssh_error,
                "checked_at": time.time()}

    @staticmethod
    def _collect_remote_ssh_metrics(host: str, profile: dict):
        """Collect basic Linux telemetry with OpenSSH and a fixed, read-only Python probe."""
        user = str(profile.get("user", "")).strip()
        try:
            port = int(profile.get("port", 22))
        except (TypeError, ValueError):
            return None, None, "invalid port"
        key_path = os.path.abspath(os.path.expanduser(str(profile.get("key_path", "")).strip()))
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user)
                or not re.fullmatch(r"[A-Za-z0-9_.:%\-\[\]]+", host)
                or not 1 <= port <= 65535 or not os.path.isfile(key_path)):
            return None, None, "invalid SSH profile"
        ssh = shutil.which("ssh")
        if not ssh:
            return None, None, "OpenSSH not installed"
        remote_command = "python3 -c " + shlex.quote(SSH_METRICS_SCRIPT)
        command = [
            ssh, "-p", str(port), "-i", key_path,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{user}@{host}", remote_command,
        ]
        started = time.perf_counter()
        try:
            result = proc_utils.run_hidden(
                command, capture_output=True, text=True, timeout=12)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if result.returncode != 0:
                return None, None, (result.stderr or result.stdout or "SSH command failed").strip()[:240]
            for line in reversed((result.stdout or "").splitlines()):
                try:
                    metrics = json.loads(line.strip())
                    if isinstance(metrics, dict) and "cpu_pct" in metrics:
                        return metrics, elapsed_ms, None
                except json.JSONDecodeError:
                    continue
            return None, None, "remote probe returned no metrics"
        except FileNotFoundError:
            return None, None, "OpenSSH not installed"
        except subprocess.TimeoutExpired:
            return None, None, "SSH timeout"
        except Exception as exc:
            return None, None, str(exc)[:240]

    def _refresh_docker_stats_cache(self):
        docker_names = {n for n, info in self.tracked_containers.items()
                        if info.get("stype") == "docker"}
        if not docker_names:
            return

        new_cache = {}
        try:
            r = proc_utils.run_hidden(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"],
                capture_output=True, text=True, timeout=10
            )
            for line in r.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) != 4:
                    continue
                name, cpu_s, mem_usage_s, mem_pct_s = parts
                if name not in docker_names:
                    continue
                try:
                    cpu_pct = float(cpu_s.strip().rstrip("%"))
                except ValueError:
                    cpu_pct = 0.0
                try:
                    mem_pct = float(mem_pct_s.strip().rstrip("%"))
                except ValueError:
                    mem_pct = 0.0
                new_cache[name] = {
                    "online": True, "cpu_pct": cpu_pct,
                    "mem_usage": mem_usage_s.strip(), "mem_pct": mem_pct,
                    "disk_str": self._docker_stats_cache.get(name, {}).get("disk_str", "—"),
                    "health": self._docker_stats_cache.get(name, {}).get("health", "unknown"),
                }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        try:
            r2 = proc_utils.run_hidden(
                ["docker", "ps", "-s", "--format", "{{.Names}}|{{.Size}}|{{.Status}}"],
                capture_output=True, text=True, timeout=10
            )
            for line in r2.stdout.strip().splitlines():
                parts = line.split("|", 2)
                if len(parts) != 3:
                    continue
                name, size_s, status_s = parts
                if name in new_cache:
                    new_cache[name]["disk_str"] = size_s.strip()
                    status_lower = status_s.lower()
                    if "unhealthy" in status_lower:
                        new_cache[name]["health"] = "unhealthy"
                    elif "health: starting" in status_lower:
                        new_cache[name]["health"] = "starting"
                    elif "healthy" in status_lower:
                        new_cache[name]["health"] = "healthy"
                    else:
                        new_cache[name]["health"] = "unknown"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # container ที่ track ไว้แต่ไม่เจอใน docker stats แปลว่า offline
        for name in docker_names:
            if name not in new_cache:
                new_cache[name] = {"online": False, "cpu_pct": 0.0, "mem_usage": "—",
                                   "mem_pct": 0.0, "disk_str": "—"}

        previous_cache = self._docker_stats_cache
        self._docker_stats_cache = new_cache
        now = time.time()
        for name, stats in new_cache.items():
            self._safe_after(0, self._sync_server_manager_status, name,
                             stats.get("online", False), stats.get("health", "unknown"))
            health = stats.get("health", "unknown")
            self._update_auto_heal_state(name, stats, now)
            previous_health = previous_cache.get(name, {}).get("health", "unknown")
            last_event = self._last_health_event_ts.get(name, 0.0)
            if (stats.get("online") and health in ("healthy", "unhealthy", "starting")
                    and health != previous_health and now - last_event >= 15):
                self._last_health_event_ts[name] = now
                self._safe_after(0, self._record_docker_health_transition,
                                 name, previous_health, health)
        self._check_resource_alerts(new_cache)

    def _update_auto_heal_state(self, name: str, stats: dict, now: float):
        """Restart only opted-in containers after repeated unhealthy checks and a cooldown."""
        enabled = bool(self.get_limit(name).get("auto_restart_unhealthy", False))
        if not enabled or not stats.get("online") or stats.get("health") != "unhealthy":
            self._unhealthy_poll_streak[name] = 0
            return
        streak = self._unhealthy_poll_streak.get(name, 0) + 1
        self._unhealthy_poll_streak[name] = streak
        last_restart = self._last_auto_restart_ts.get(name, 0.0)
        if (streak < 2 or name in self._auto_restart_inflight
                or now - last_restart < self._auto_restart_cooldown):
            return
        self._last_auto_restart_ts[name] = now
        self._auto_restart_inflight.add(name)
        threading.Thread(target=self._auto_restart_unhealthy_worker,
                         args=(name,), daemon=True).start()

    def _auto_restart_unhealthy_worker(self, name: str):
        """Perform and audit one policy-authorized Docker health recovery attempt."""
        started_at = time.monotonic()
        try:
            result = proc_utils.run_hidden(
                ["docker", "restart", name], capture_output=True, text=True, timeout=35)
            if result.returncode == 0:
                message = (f"🛡️ Auto Heal: ส่งคำสั่ง restart {name} สำเร็จ หลัง health check unhealthy "
                           "ต่อเนื่อง 2 รอบ — รอตรวจการฟื้นตัว")
                level = "success"
                self._auto_restart_waiting_recovery[name] = time.time()
                current = self._docker_stats_cache.get(name, {})
                current.update({"online": True, "health": "starting"})
                self._docker_stats_cache[name] = current
            else:
                detail = (result.stderr or result.stdout or "ไม่ทราบสาเหตุ").strip()[:240]
                message = f"🛡️ Auto Heal: restart {name} ไม่สำเร็จ — {detail}"
                level = "warning"
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            message = f"🛡️ Auto Heal: restart {name} ไม่สำเร็จ — {exc}"
            level = "warning"
        except Exception as exc:
            message = f"🛡️ Auto Heal: restart {name} เกิดข้อผิดพลาด — {exc}"
            level = "warning"
        finally:
            self._auto_restart_inflight.discard(name)
            self._unhealthy_poll_streak[name] = 0
        self._safe_after(0, self._append_action_log, message, level)
        self._safe_after(0, self._show_toast, message)
        attempt_duration = round(max(0.0, time.monotonic() - started_at), 3)
        try:
            web_server.web_state.add_event(
                message, category="auto_remediation",
                details={"container": name, "action": "restart", "policy": "unhealthy_healthcheck",
                         "phase": "attempt", "attempt_duration_seconds": attempt_duration},
                outcome="accepted" if level == "success" else "failed")
        except Exception:
            pass
        if level == "success":
            self._safe_after(0, self._refresh_server_dashboard_ui)

    def _sync_server_manager_status(self, name: str, online: bool, health: str = "unknown"):
        """อัปเดตป้าย Server Manager จากสถานะ Docker ล่าสุด รวมคำสั่งที่มาจากหน้าเว็บ"""
        label = self._server_manager_status_labels.get(name)
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
            if not online:
                label.configure(text="⚫ Offline", text_color=Theme.TEXT_MUTED)
            elif health == "unhealthy":
                label.configure(text="🔴 Online · Unhealthy", text_color=Theme.DANGER)
            elif health == "starting":
                label.configure(text="🟡 Online · Starting", text_color=Theme.WARNING)
            else:
                label.configure(text="🟢 Online", text_color=Theme.SUCCESS)
        except (tk.TclError, RuntimeError):
            pass

    def _record_docker_health_transition(self, name: str, previous: str, current: str):
        """Persist Docker health-check state transitions for incident review."""
        observed_recovery_seconds = None
        auto_heal_recovered = False
        if current == "unhealthy":
            self._unhealthy_since.setdefault(name, time.monotonic())
            text = f"🔴 [{name}] Docker health check failed (previous: {previous})"
            level = "warning"
        elif current == "healthy":
            unhealthy_since = self._unhealthy_since.pop(name, None)
            if unhealthy_since is not None:
                observed_recovery_seconds = round(max(0.0, time.monotonic() - unhealthy_since), 3)
            restart_requested_at = self._auto_restart_waiting_recovery.pop(name, None)
            auto_heal_recovered = bool(restart_requested_at and time.time() - restart_requested_at <= 1800)
            text = f"🟢 [{name}] Docker health check recovered (previous: {previous})"
            level = "success"
        else:
            text = f"🟡 [{name}] Docker health check is starting"
            level = "warning"
        self._append_action_log(text, level=level)
        try:
            details = {"container": name, "previous": previous, "current": current}
            if observed_recovery_seconds is not None:
                details["observed_recovery_seconds"] = observed_recovery_seconds
                details["duration_note"] = "เวลาที่โปรแกรมสังเกตจากรอบตรวจ Docker; ไม่ใช่ผลทดสอบ MTTR ภายนอก"
            if auto_heal_recovered:
                web_server.web_state.add_event(
                    f"🟢 Auto Heal: {name} กลับมามี health check healthy แล้ว",
                    category="auto_remediation_recovery",
                    details={"container": name, "action": "restart", "policy": "unhealthy_healthcheck",
                             "recovery_seconds": observed_recovery_seconds},
                    outcome="success")
            web_server.web_state.add_event(
                text, category="service_health",
                details=details,
                outcome=current)
        except Exception:
            pass
        if current == "unhealthy" and self.notifier.config.get("popup_enabled", True):
            self._show_toast(text)

    def _check_resource_alerts(self, cache: dict):
        """เทียบ CPU/RAM ของแต่ละ container กับ threshold (ตั้งเองด้วย /ral หรือ default 90%)
           เรียกจาก background thread (_docker_stats_poll_loop) — ห้ามแตะ widget ตรงนี้โดยตรง
           ต้องส่งผ่าน self.after เท่านั้น"""
        now = time.time()
        for name, stats in cache.items():
            if not stats.get("online"):
                continue

            last_ts = self._last_alert_ts.get(name, 0.0)
            if now - last_ts < self._alert_cooldown:
                continue

            alert = self.get_alert(name)
            cpu_pct = stats.get("cpu_pct", 0.0)
            mem_pct = stats.get("mem_pct", 0.0)

            breached = False
            if cpu_pct > alert["cpu_pct"]:
                self._safe_after(0, self._trigger_resource_alert, name, "CPU", cpu_pct, alert["cpu_pct"])
                breached = True
            if mem_pct > alert["ram_pct"]:
                self._safe_after(0, self._trigger_resource_alert, name, "RAM", mem_pct, alert["ram_pct"])
                breached = True

            if breached:
                self._last_alert_ts[name] = now

    def _trigger_resource_alert(self, name, metric, value, threshold):
        """แจ้งเตือนเมื่อ container ใช้ CPU/RAM เกิน threshold — เรียกบน main thread เท่านั้น"""
        msg = f"🚨 [{name}] {metric} = {value:.1f}% เกินค่าที่ตั้งไว้ ({threshold:.0f}%)"
        self._append_action_log(msg)
        try:
            web_server.web_state.add_event(msg)
        except Exception:
            pass
        if self.notifier.config.get("popup_enabled", True):
            self._show_toast(msg)

    def _show_toast(self, message: str, duration_ms: int = 4000):
        """แจ้งเตือนแบบลอยมุมจอ ไม่บล็อก UI (ต่างจาก msgbox ที่ต้องกดปิดก่อนถึงจะใช้งานส่วนอื่นได้)"""
        try:
            toast = ctk.CTkToplevel(self)
            toast.overrideredirect(True)
            toast.attributes("-topmost", True)
            x = self.winfo_x() + self.winfo_width() - 360
            y = self.winfo_y() + self.winfo_height() - 100
            toast.geometry(f"340x70+{max(x, 0)}+{max(y, 0)}")
            frame = ctk.CTkFrame(toast, fg_color=Theme.DANGER_BG)
            frame.pack(fill="both", expand=True)
            ctk.CTkLabel(frame, text=message, font=("Segoe UI", 12, "bold"),
                         text_color=Theme.TEXT_PRIMARY, wraplength=310, justify="left").pack(padx=10, pady=10)
            toast.after(duration_ms, toast.destroy)
        except Exception:
            pass

    def _refresh_server_dashboard_ui(self):
        """อัปเดต UI ของ Server Dashboard จาก cache (ไม่เรียก subprocess ตรงนี้ — เร็ว/ไม่บล็อก UI)"""
        try:
            if self.tabview.get() != "📡 Server Dashboard":
                return
        except (AttributeError, tk.TclError):
            return
        for name, widgets in list(self._server_dash_widgets.items()):
            try:
                info = self.tracked_containers.get(name, {})
                if not widgets["card"].winfo_exists():
                    continue
                ping = self._server_ping_cache.get(name)
                if ping:
                    if ping.get("online"):
                        latency = ping.get("latency_ms")
                        method = ping.get("method")
                        if latency is None:
                            ping_text = "Ping: ตอบกลับ"
                        elif method == "exec":
                            ping_text = f"ตอบสนอง: {latency:.1f} ms (Docker)"
                        elif method == "ssh":
                            ping_text = f"SSH: {latency:.0f} ms"
                        else:
                            ping_text = f"Ping: {latency:.1f} ms"
                        widgets["ping_label"].configure(
                            text=ping_text, text_color=Theme.SUCCESS)
                        if info.get("stype") == "server":
                            widgets["status"].configure(
                                text="🟢 Online", text_color=Theme.SUCCESS)
                    else:
                        unavailable = ping.get("method") == "unavailable"
                        widgets["ping_label"].configure(
                            text="Ping: ตรวจไม่ได้" if unavailable else "Ping: Offline",
                            text_color=Theme.TEXT_MUTED if unavailable else Theme.DANGER)
                        if info.get("stype") == "server":
                            widgets["status"].configure(
                                text="⚫ Offline", text_color=Theme.TEXT_MUTED)
                if info.get("stype") != "docker":
                    metrics_label = widgets.get("remote_metrics_label")
                    if metrics_label is not None and ping:
                        metrics = ping.get("ssh_metrics")
                        if metrics:
                            uptime = max(0, int(float(metrics.get("uptime_sec", 0))))
                            days, remainder = divmod(uptime, 86400)
                            hours, remainder = divmod(remainder, 3600)
                            minutes = remainder // 60
                            uptime_text = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
                            metrics_label.configure(
                                text=(f"CPU {metrics.get('cpu_pct', 0):.1f}%   ·   "
                                      f"RAM {metrics.get('ram_used_mb', 0):,.0f}/"
                                      f"{metrics.get('ram_total_mb', 0):,.0f} MB "
                                      f"({metrics.get('ram_pct', 0):.1f}%)   ·   "
                                      f"Disk {metrics.get('disk_pct', 0):.1f}% "
                                      f"({metrics.get('disk_used_gb', 0):.1f}/"
                                      f"{metrics.get('disk_total_gb', 0):.1f} GB)\n"
                                      f"Disk I/O R {metrics.get('disk_read_mb_s', 0):.3f} / "
                                      f"W {metrics.get('disk_write_mb_s', 0):.3f} MB/s   ·   "
                                      f"Network RX {metrics.get('net_rx_mb_s', 0):.3f} MB/s  ·  "
                                      f"TX {metrics.get('net_tx_mb_s', 0):.3f} MB/s   ·   "
                                      f"Load {metrics.get('load_1m', 0):.2f}   ·   "
                                      f"Processes {int(metrics.get('process_count', 0))}   ·   "
                                      f"Uptime {uptime_text}"),
                                text_color=Theme.TEXT_SECONDARY)
                        elif self._ssh_profiles.get(name) and ping.get("ssh_error"):
                            metrics_label.configure(
                                text=f"SSH ยังอ่าน metrics ไม่ได้: {ping['ssh_error']}",
                                text_color=Theme.WARNING)
                    continue
                stats = self._docker_stats_cache.get(name)
                if not stats:
                    continue
                if stats["online"]:
                    health = stats.get("health", "unknown")
                    if health == "unhealthy":
                        status_text, status_color = "🔴 Online · Unhealthy", Theme.DANGER
                    elif health == "starting":
                        status_text, status_color = "🟡 Online · Starting", Theme.WARNING
                    elif health == "healthy":
                        status_text, status_color = "🟢 Online · Healthy", Theme.SUCCESS
                    else:
                        status_text, status_color = "🟢 Online", Theme.SUCCESS
                    widgets["status"].configure(text=status_text, text_color=status_color)
                    widgets["cpu_label"].configure(text=f'{stats["cpu_pct"]:.1f}%')
                    widgets["cpu_bar"].set(min(1.0, stats["cpu_pct"] / 100.0))
                    widgets["ram_label"].configure(
                        text=f'{stats["mem_usage"]} ({stats["mem_pct"]:.1f}%)')
                    widgets["ram_bar"].set(min(1.0, stats["mem_pct"] / 100.0))
                    widgets["disk_label"].configure(text=stats["disk_str"])
                else:
                    widgets["status"].configure(text="⚫ Offline", text_color=Theme.TEXT_MUTED)
                    widgets["cpu_label"].configure(text="—")
                    widgets["cpu_bar"].set(0)
                    widgets["ram_label"].configure(text="—")
                    widgets["ram_bar"].set(0)
                    widgets["disk_label"].configure(text="—")
            except Exception:
                continue

    # ══════════════════════════════════════════════════════════
    # แท็บ 3: Server Manager
    # ══════════════════════════════════════════════════════════
    def build_server_manager(self):
        top = ctk.CTkFrame(self.tab_servers, fg_color="transparent")
        top.pack(fill="x", pady=8)
        ctk.CTkButton(top, text="📁 Import YML / Docker Compose",
                      command=self.import_yaml_dialog).pack(side="left", padx=10)
        ctk.CTkButton(top, text="🔄 Refresh All Status",
                      fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                      command=self.refresh_all_status).pack(side="left", padx=5)

        self.server_list_frame = ctk.CTkScrollableFrame(self.tab_servers, height=500)
        self.server_list_frame.pack(pady=5, fill="both", expand=True)

    def refresh_all_status(self):
        """Reload YAML files และอัปเดต UI ใหม่"""
        for w in self.server_list_frame.winfo_children():
            w.destroy()
        self.load_saved_yamls()

    def load_saved_yamls(self):
        for entry in self._read_settings():
            fp, label = (entry.get("path"), entry.get("label")) if isinstance(entry, dict) else (entry, None)
            if fp and os.path.exists(fp):
                self.process_yaml_file(fp, label)

    def _read_settings(self):
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Load saved YAML error: {e}")
        return []

    def _load_ssh_profiles(self):
        """Load per-host SSH usernames, ports, and key paths (never passwords/private-key data)."""
        try:
            with open(self._ssh_profiles_file, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_ssh_profiles(self):
        atomic_write_json(self._ssh_profiles_file, self._ssh_profiles, ensure_ascii=False)

    def _write_settings(self, entries):
        atomic_write_json(self.settings_file, entries, ensure_ascii=True)

    def _existing_labels(self):
        """ชื่อที่ถูกตั้งไว้แล้วทั้งหมด (lowercase) ใช้เช็คกันชื่อซ้ำ"""
        return {lbl.strip().lower() for lbl in self.yaml_labels.values()}

    def save_filepath(self, filepath, label):
        normalized = []
        for entry in self._read_settings():
            fp = entry.get("path") if isinstance(entry, dict) else entry
            if fp != filepath:
                normalized.append(entry if isinstance(entry, dict) else {"path": fp, "label": os.path.basename(fp)})
        normalized.append({"path": filepath, "label": label})
        self._write_settings(normalized)

    def remove_yaml_entry(self, filepath):
        """เอาไฟล์ YAML/docker-compose ที่ไม่ใช้แล้วออกจากรายการที่ติดตาม
           (ลบแค่ออกจากรายการของโปรแกรม ไม่ลบไฟล์จริงบนดิสก์ และไม่หยุด container ที่กำลังรันอยู่)"""
        self.yaml_paths.pop(filepath, None)
        self.yaml_labels.pop(filepath, None)
        remaining = [
            e for e in self._read_settings()
            if (e.get("path") if isinstance(e, dict) else e) != filepath
        ]
        self._write_settings(remaining)

    def import_yaml_dialog(self):
        fp = filedialog.askopenfilename(filetypes=[("YAML files", "*.yaml *.yml")])
        if not fp:
            return
        if fp in self.yaml_paths:
            msgbox.showinfo("มีอยู่แล้ว", "ไฟล์นี้ถูก import ไว้ในรายการแล้ว")
            return

        default_name = os.path.basename(fp)
        while True:
            dialog = ctk.CTkInputDialog(
                text=f"ตั้งชื่อสำหรับรายการนี้ (ต้องไม่ซ้ำกับชื่อที่มีอยู่)\n\nไฟล์: {fp}",
                title="ตั้งชื่อก่อน Import"
            )
            label = dialog.get_input()
            if label is None:          # กด Cancel -> ยกเลิกการ import
                return
            label = label.strip() or default_name
            if label.lower() in self._existing_labels():
                msgbox.showerror("ชื่อซ้ำ", f'ชื่อ "{label}" มีอยู่แล้ว กรุณาตั้งชื่ออื่น')
                continue
            break

        self.save_filepath(fp, label)
        self.process_yaml_file(fp, label)

    def process_yaml_file(self, filepath, label=None):
        fname = os.path.basename(filepath)
        fdir  = os.path.dirname(filepath)
        display_name = label or fname
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and ("services" in data or "servers" in data):
                # ใช้ path เต็มเป็น key (ไม่ใช่แค่ชื่อไฟล์) ป้องกัน docker-compose.yml
                # จากคนละโฟลเดอร์ทับ path กันเอง
                self.yaml_paths[filepath] = fdir
                self.yaml_labels[filepath] = display_name
                self.create_collapsible_group(filepath, display_name, data)
            else:
                ctk.CTkLabel(self.server_list_frame,
                             text=f"❌ โครงสร้างไฟล์ {fname} ไม่ถูกต้อง",
                             text_color=Theme.DANGER, font=("Segoe UI", 13)).pack(pady=6)
        except Exception as e:
            ctk.CTkLabel(self.server_list_frame,
                         text=f"❌ {fname}: {e}",
                         text_color=Theme.DANGER).pack(pady=6)

    def _extract_compose_host_port(self, svc_info) -> str:
        """ดึง host port ตัวแรกจาก docker-compose service definition (คีย์ 'ports') มาใช้เป็นค่า
           เริ่มต้นของช่อง Quick Tunnel ให้อัตโนมัติ — รองรับทั้งรูปแบบสั้น 'host:container' /
           '127.0.0.1:host:container' และแบบยาว ({'published': ..., 'target': ...})
           หาไม่เจอก็คืนค่าว่างเฉยๆ (ผู้ใช้กรอก port เองได้อยู่แล้วในหน้าต่าง Quick Tunnel)"""
        if not svc_info:
            return ""
        for p in (svc_info.get("ports") or []):
            if isinstance(p, str):
                parts = p.split(":")
                try:
                    return str(int(parts[-2])) if len(parts) >= 2 else str(int(parts[0]))
                except ValueError:
                    continue
            elif isinstance(p, dict) and p.get("published"):
                return str(p["published"])
        return ""

    def create_collapsible_group(self, yaml_key, filename, data):
        group = ctk.CTkFrame(self.server_list_frame, border_width=1, border_color=Theme.BORDER)
        group.pack(fill="x", pady=5, padx=5)

        header = ctk.CTkFrame(group, fg_color=Theme.ACCENT_BG)
        header.pack(fill="x")

        suffix = "(Docker Compose)" if "services" in data else "(Servers List)"

        title_label = ctk.CTkLabel(header, text=f"📁 {filename} {suffix}",
                                   font=("Segoe UI", 13, "bold"), text_color=Theme.TEXT_PRIMARY)
        title_label.pack(side="left", padx=10, pady=8)

        content = ctk.CTkFrame(group, fg_color="transparent")
        content.pack(fill="x", pady=3)

        is_open = ctk.BooleanVar(value=True)

        def toggle():
            if is_open.get():
                content.pack_forget()
                btn.configure(text="▶ ขยาย")
                is_open.set(False)
            else:
                content.pack(fill="x", pady=3)
                btn.configure(text="▼ พับเก็บ")
                is_open.set(True)

        btn = ctk.CTkButton(header, text="▼ พับเก็บ", width=70,
                            fg_color=Theme.NEUTRAL_DARK, hover_color=Theme.NEUTRAL_DARK_HOVER,
                            command=toggle)
        btn.pack(side="right", padx=10, pady=8)

        def current_label():
            return self.yaml_labels.get(yaml_key, filename)

        def delete_yaml():
            if msgbox.askyesno(
                "ลบรายการ",
                f"ต้องการลบ {current_label()} ออกจากรายการที่ติดตามหรือไม่?\n\n"
                "(ลบแค่ออกจากรายการของโปรแกรม ไม่ลบไฟล์จริงบนดิสก์\n"
                "และไม่หยุด container ที่กำลังรันอยู่)"
            ):
                self.remove_yaml_entry(yaml_key)
                self.remove_server_dashboard_entries(yaml_key)
                group.destroy()

        ctk.CTkButton(header, text="🗑️ Delete", width=70,
                      fg_color=Theme.DANGER, hover_color=Theme.DANGER_HOVER,
                      command=delete_yaml).pack(side="right", padx=(0, 4), pady=8)

        def rename_yaml():
            while True:
                dialog = ctk.CTkInputDialog(
                    text=f"ตั้งชื่อใหม่ (ปัจจุบัน: {current_label()})\nต้องไม่ซ้ำกับชื่อที่มีอยู่:",
                    title="เปลี่ยนชื่อ"
                )
                new_label = dialog.get_input()
                if new_label is None:           # กด Cancel
                    return
                new_label = new_label.strip()
                if not new_label:
                    msgbox.showerror("ผิดพลาด", "ชื่อต้องไม่เป็นค่าว่าง")
                    continue
                if new_label.lower() != current_label().lower() and new_label.lower() in self._existing_labels():
                    msgbox.showerror("ชื่อซ้ำ", f'ชื่อ "{new_label}" มีอยู่แล้ว กรุณาตั้งชื่ออื่น')
                    continue
                break
            self.yaml_labels[yaml_key] = new_label
            self.save_filepath(yaml_key, new_label)
            title_label.configure(text=f"📁 {new_label} {suffix}")

        ctk.CTkButton(header, text="✏️ Rename", width=75,
                      fg_color=Theme.PURPLE, hover_color=Theme.PURPLE_HOVER,
                      command=rename_yaml).pack(side="right", padx=(0, 4), pady=8)

        if "services" in data:
            for svc_name, svc_info in data["services"].items():
                container = svc_info.get("container_name", svc_name) if svc_info else svc_name
                host_port = self._extract_compose_host_port(svc_info)
                self.create_server_row(content, container, "docker",
                                       yaml_filename=yaml_key, service_name=svc_name,
                                       host_port=host_port)
        elif "servers" in data:
            for srv in data.get("servers", []):
                self.create_server_row(content, srv.get("name"), "server",
                                       ip=srv.get("ip", ""), yaml_filename=yaml_key)


    def create_server_row(self, parent, name, stype="server",
                          ip="", yaml_filename="", service_name="", host_port=""):
        row = ctk.CTkFrame(parent, fg_color=Theme.BG_CARD, border_width=1, border_color=Theme.BORDER)
        row.pack(fill="x", pady=2, padx=8)

        info_frame = ctk.CTkFrame(row, fg_color="transparent")
        info_frame.pack(side="left", fill="x", expand=True)

        icon = "📦" if stype == "docker" else "🖥️"
        display = f"{icon} {name} ({ip})" if ip else f"{icon} {name}"
        ctk.CTkLabel(info_frame, text=display, font=("Segoe UI", 13),
                     width=200, anchor="w").pack(side="left", padx=10, pady=6)

        status_text, status_color = self.get_real_status(name, stype, ip)
        status_label = ctk.CTkLabel(info_frame, text=status_text,
                                    text_color=status_color, font=("Segoe UI", 12, "bold"))
        status_label.pack(side="left", padx=8)
        self._server_manager_status_labels[name] = status_label

        btn_frame = ctk.CTkFrame(row, fg_color="transparent")
        btn_frame.pack(side="right", padx=8)

        # inline lambdas (capture by value with default args)
        _l, _n, _t, _yf, _sn, _i = status_label, name, stype, yaml_filename, service_name, ip
        ctk.CTkButton(btn_frame, text="▶", width=30, fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER,
                      command=lambda l=_l, n=_n, t=_t, yf=_yf, sn=_sn, i=_i:
                      self.control_server("start", n, l, t, yf, sn, i)).pack(side="left", padx=2)
        ctk.CTkButton(btn_frame, text="⏹", width=30, fg_color=Theme.DANGER, hover_color=Theme.DANGER_HOVER,
                      command=lambda l=_l, n=_n, t=_t, yf=_yf, sn=_sn, i=_i:
                      self.control_server("stop", n, l, t, yf, sn, i)).pack(side="left", padx=2)
        ctk.CTkButton(btn_frame, text="🔄", width=30, fg_color=Theme.WARNING, text_color="black", hover_color=Theme.WARNING_HOVER,
                      command=lambda l=_l, n=_n, t=_t, yf=_yf, sn=_sn, i=_i:
                      self.control_server("restart", n, l, t, yf, sn, i)).pack(side="left", padx=2)

        if stype == "docker":
            ctk.CTkButton(btn_frame, text="📝 Logs", width=55,
                          fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                          command=lambda n=_n: self.open_docker_logs(n)).pack(side="left", padx=4)
        else:
            ctk.CTkButton(
                btn_frame, text="🔐 SSH", width=58,
                fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                command=lambda n=_n, host=_i: self._open_ssh_profile_dialog(n, host),
            ).pack(side="left", padx=4)

        ctk.CTkButton(btn_frame, text="⚙️ Limits", width=70,
                      fg_color="#0ea5e9", hover_color="#0284c7", text_color="#08090b",
                      command=lambda n=_n: self.open_settings(n)).pack(side="left", padx=4)

        # ── 🤖 AI RAM: ปุ่มเปิด/ปิดต่อ server เดียว — ปิด (ค่าเริ่มต้น) = Manual ตั้ง RAM เองผ่าน
        #    ⚙️ Limits ตามปกติ, เปิด = ให้ RamAIManager ปรับ ram_limit_mb ให้อัตโนมัติตามการใช้งานจริง
        #    (เฉพาะ container docker เท่านั้น — server แบบ physical ต้องตั้งเองที่เครื่องปลายทาง) ──
        if stype == "docker":
            def _ai_btn_style(enabled: bool):
                return (("🤖 AUTO mode: ON", Theme.SUCCESS, Theme.SUCCESS_HOVER) if enabled
                        else ("🤖 Manual", Theme.NEUTRAL, Theme.NEUTRAL_HOVER))

            ai_txt0, ai_fg0, ai_hv0 = _ai_btn_style(self.get_limit(_n).get("ai_ram_enabled", False))
            ai_btn = ctk.CTkButton(btn_frame, text=ai_txt0, width=100,
                                    fg_color=ai_fg0, hover_color=ai_hv0)

            def toggle_ai_ram(n=_n, btn=ai_btn):
                new_val = not self.get_limit(n).get("ai_ram_enabled", False)
                self._set_ai_ram_enabled(n, new_val)
                txt, fg, hv = _ai_btn_style(new_val)
                btn.configure(text=txt, fg_color=fg, hover_color=hv)
                if new_val:
                    self._append_action_log(
                        f"🤖 เปิดใช้งาน AI ปรับ RAM อัตโนมัติสำหรับ {n} แล้ว "
                        "(RAM จะไม่ตั้งค่าด้วยตัวเองอีกจนกว่าจะปิดสวิตช์นี้)", "success")
                    self.ram_ai_manager.check_now(n)   # ประเมิน/ปรับรอบแรกทันที ไม่ต้องรอ 20 วิ
                else:
                    self._append_action_log(
                        f"🖐️ ปิด AI ปรับ RAM ของ {n} แล้ว — กลับไปตั้งค่า RAM ด้วยตัวเอง (Manual) "
                        "ผ่าน ⚙️ Limits", "warning")

            ai_btn.configure(command=toggle_ai_ram)
            ai_btn.pack(side="left", padx=4)

            auto_heal_enabled = bool(self.get_limit(_n).get("auto_restart_unhealthy", False))
            auto_heal_btn = ctk.CTkButton(
                btn_frame,
                text=("🛡️ Auto Heal: ON" if auto_heal_enabled else "🛡️ Auto Heal: OFF"),
                width=118,
                fg_color=Theme.SUCCESS if auto_heal_enabled else Theme.NEUTRAL,
                hover_color=Theme.SUCCESS_HOVER if auto_heal_enabled else Theme.NEUTRAL_HOVER,
            )

            def toggle_auto_heal(n=_n, btn=auto_heal_btn):
                enabled = not bool(self.get_limit(n).get("auto_restart_unhealthy", False))
                self._set_auto_restart_unhealthy(n, enabled)
                btn.configure(
                    text="🛡️ Auto Heal: ON" if enabled else "🛡️ Auto Heal: OFF",
                    fg_color=Theme.SUCCESS if enabled else Theme.NEUTRAL,
                    hover_color=Theme.SUCCESS_HOVER if enabled else Theme.NEUTRAL_HOVER,
                )
                text = (f"🛡️ เปิด Auto Heal สำหรับ {n}: restart เมื่อ Docker health เป็น unhealthy "
                        "ต่อเนื่องอย่างน้อย 2 รอบ (พัก 5 นาทีหลังสั่งงาน)" if enabled else
                        f"🛡️ ปิด Auto Heal สำหรับ {n} แล้ว")
                self._append_action_log(text, "warning" if enabled else "info")
                try:
                    web_server.web_state.add_event(
                        text, category="auto_heal_policy",
                        details={"container": n, "enabled": enabled}, outcome="policy_changed")
                except Exception:
                    pass

            auto_heal_btn.configure(command=toggle_auto_heal)
            auto_heal_btn.pack(side="left", padx=4)

            # ── ⚡ Quick Tunnel: เปิดลิงก์สาธารณะตรงไปที่ localhost:<port> ของ server ตัวนี้ผ่าน
            #    cloudflared บนเครื่องนี้โดยตรง (ไม่ผ่าน docker-compose) — เหมาะใช้แชร์ server ที่
            #    เพิ่ง import ให้คนอื่นดูด่วนๆ โดยไม่ต้องแก้ docker-compose.yml ของโปรแกรมเอง ──
            tunnel_btn = ctk.CTkButton(btn_frame, text="⚡ Tunnel", width=85,
                                       fg_color=Theme.PURPLE, hover_color=Theme.PURPLE_HOVER)
            tunnel_btn.configure(command=lambda n=_n, hp=host_port, b=tunnel_btn:
                                 self.open_quick_tunnel_dialog(n, hp, b))
            tunnel_btn.pack(side="left", padx=4)
            tstate = self._server_quick_tunnels.setdefault(_n, {
                "proc": None, "url": None, "port": host_port or "",
                "dialog": None, "status_label": None, "toggle_btn": None, "port_entry": None,
            })
            tstate["row_btn"] = tunnel_btn
            self._refresh_tunnel_row_button(_n)   # เผื่อมี tunnel ค้างรันอยู่จากก่อน refresh/reopen

        # ลงทะเบียน server นี้ไว้ใช้ในหน้า Server Dashboard (CPU/RAM/Disk/Terminal แยกตัว)
        self.tracked_containers[name] = {
            "yaml_key": yaml_filename, "service_name": service_name,
            "stype": stype, "ip": ip,
        }
        self._save_tracked_containers()
        self.add_server_dashboard_card(name, stype, ip, yaml_key=yaml_filename)

    def _open_ssh_profile_dialog(self, name: str, ip: str):
        """Configure key-based OpenSSH access for a Linux server without storing secrets."""
        if not ip:
            msgbox.showwarning("ตั้งค่า SSH", "รายการนี้ยังไม่มี IP/Hostname ในไฟล์ Servers List")
            return
        profile = self._ssh_profiles.get(name, {})
        win = ctk.CTkToplevel(self)
        win.title(f"SSH Metrics — {name}")
        win.geometry("520x340")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="เชื่อมต่อ Linux Server ผ่าน SSH",
                     font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(
            win, text=("ใช้ OpenSSH และกุญแจส่วนตัวในเครื่องนี้ · ไม่บันทึกรหัสผ่านหรือเนื้อหากุญแจ\n"
                      "ถ้ากุญแจมี passphrase ให้โหลดกุญแจผ่าน ssh-agent ก่อนเชื่อมต่อ"),
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED,
        ).pack(anchor="w", padx=20, pady=(0, 12))

        form = ctk.CTkFrame(win, fg_color="transparent")
        form.pack(fill="x", padx=20)
        ctk.CTkLabel(form, text=f"Host: {ip}", anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=5)
        ctk.CTkLabel(form, text="SSH user", width=110, anchor="w").grid(
            row=1, column=0, sticky="w", pady=5)
        user_entry = ctk.CTkEntry(form, placeholder_text="เช่น ubuntu")
        user_entry.grid(row=1, column=1, sticky="ew", pady=5)
        user_entry.insert(0, profile.get("user", ""))
        ctk.CTkLabel(form, text="Port", width=110, anchor="w").grid(
            row=2, column=0, sticky="w", pady=5)
        port_entry = ctk.CTkEntry(form, width=100)
        port_entry.grid(row=2, column=1, sticky="w", pady=5)
        port_entry.insert(0, str(profile.get("port", 22)))
        ctk.CTkLabel(form, text="Private key", width=110, anchor="w").grid(
            row=3, column=0, sticky="w", pady=5)
        key_row = ctk.CTkFrame(form, fg_color="transparent")
        key_row.grid(row=3, column=1, sticky="ew", pady=5)
        key_entry = ctk.CTkEntry(key_row, placeholder_text="เลือกไฟล์ id_ed25519 / id_rsa")
        key_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        key_entry.insert(0, profile.get("key_path", ""))
        ctk.CTkButton(
            key_row, text="เลือก...", width=70,
            command=lambda: self._select_ssh_key_file(key_entry, win),
        ).pack(side="right")
        form.grid_columnconfigure(1, weight=1)

        status = ctk.CTkLabel(win, text="", anchor="w", text_color=Theme.TEXT_MUTED,
                              wraplength=470, justify="left")
        status.pack(fill="x", padx=20, pady=(10, 2))

        def save_profile():
            user = user_entry.get().strip()
            key_path = os.path.abspath(os.path.expanduser(key_entry.get().strip()))
            try:
                port = int(port_entry.get().strip())
            except ValueError:
                status.configure(text="Port ต้องเป็นตัวเลขตั้งแต่ 1 ถึง 65535", text_color=Theme.DANGER)
                return
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
                status.configure(text="กรุณาระบุ SSH user ให้ถูกต้อง", text_color=Theme.DANGER)
                return
            if not 1 <= port <= 65535:
                status.configure(text="Port ต้องอยู่ระหว่าง 1 ถึง 65535", text_color=Theme.DANGER)
                return
            if not os.path.isfile(key_path):
                status.configure(text="ไม่พบไฟล์กุญแจส่วนตัวที่เลือก", text_color=Theme.DANGER)
                return
            if not shutil.which("ssh"):
                status.configure(text="เครื่องนี้ไม่พบ OpenSSH (คำสั่ง ssh)", text_color=Theme.DANGER)
                return
            self._ssh_profiles[name] = {"user": user, "port": port, "key_path": key_path}
            self._save_ssh_profiles()
            info = self.tracked_containers.get(name)
            if info is not None:
                info["ssh"] = dict(self._ssh_profiles[name])
            widgets = self._server_dash_widgets.get(name, {})
            metrics_label = widgets.get("remote_metrics_label")
            if metrics_label is not None and metrics_label.winfo_exists():
                metrics_label.configure(text="กำลังตรวจ SSH และอ่านทรัพยากรเครื่อง...",
                                        text_color=Theme.TEXT_MUTED)
            status.configure(text="บันทึกโปรไฟล์แล้ว กำลังเชื่อมต่อเพื่อตรวจข้อมูล...",
                             text_color=Theme.SUCCESS)
            threading.Thread(target=self._probe_server_ping_once, args=(name,), daemon=True).start()
            win.after(700, win.destroy)

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(12, 16))
        ctk.CTkButton(buttons, text="บันทึกและเชื่อมต่อ", command=save_profile).pack(side="right")
        if profile:
            def remove_profile():
                self._ssh_profiles.pop(name, None)
                self._save_ssh_profiles()
                info = self.tracked_containers.get(name)
                if info is not None:
                    info.pop("ssh", None)
                metrics_label = self._server_dash_widgets.get(name, {}).get("remote_metrics_label")
                if metrics_label is not None and metrics_label.winfo_exists():
                    metrics_label.configure(
                        text="เพิ่ม SSH key ผ่านปุ่ม 🔐 SSH ใน Server Manager เพื่อดู CPU / RAM / Disk / Network ของเครื่องนี้",
                        text_color=Theme.TEXT_MUTED)
                win.destroy()
            ctk.CTkButton(buttons, text="ลบโปรไฟล์", fg_color=Theme.NEUTRAL,
                          hover_color=Theme.NEUTRAL_HOVER,
                          command=remove_profile).pack(side="left")

    @staticmethod
    def _select_ssh_key_file(entry, parent):
        path = filedialog.askopenfilename(
            parent=parent, title="เลือก private key ของ SSH",
            filetypes=[("SSH private key", "*"), ("All files", "*.*")])
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _probe_server_ping_once(self, name: str):
        info = self.tracked_containers.get(name, {})
        result = self._measure_dashboard_ping(name, info)
        self._server_ping_cache[name] = result
        self._safe_after(0, self._refresh_server_dashboard_ui)

    def _run_remote_ssh_command(self, name: str):
        """Run one administrator-entered, non-interactive command on a configured host."""
        widgets = self._server_dash_widgets.get(name, {})
        entry = widgets.get("remote_cmd_entry")
        output = widgets.get("remote_console")
        if entry is None or output is None:
            return
        command_text = entry.get().strip()
        if not command_text:
            return
        entry.delete(0, "end")
        output.configure(state="normal")
        output.insert("end", f"\n$ {command_text}\nกำลังทำงาน...\n")
        output.see("end")
        output.configure(state="disabled")
        profile = self._ssh_profiles.get(name)
        info = self.tracked_containers.get(name, {})
        if not profile:
            self._append_remote_ssh_output(name, "❌ กรุณาตั้งค่า 🔐 SSH ใน Server Manager ก่อน\n")
            return
        threading.Thread(target=self._run_remote_ssh_command_worker,
                         args=(name, info.get("ip", ""), profile, command_text),
                         daemon=True).start()

    def _run_remote_ssh_command_worker(self, name: str, host: str,
                                       profile: dict, command_text: str):
        user = str(profile.get("user", "")).strip()
        try:
            port = int(profile.get("port", 22))
        except (TypeError, ValueError):
            self._safe_after(0, self._append_remote_ssh_output, name, "❌ SSH port ไม่ถูกต้อง\n")
            return
        key_path = os.path.abspath(os.path.expanduser(str(profile.get("key_path", "")).strip()))
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user)
                or not re.fullmatch(r"[A-Za-z0-9_.:%\-\[\]]+", host)
                or not 1 <= port <= 65535 or not os.path.isfile(key_path)):
            self._safe_after(0, self._append_remote_ssh_output, name,
                             "❌ โปรไฟล์ SSH หรือ host ไม่ถูกต้อง\n")
            return
        ssh = shutil.which("ssh")
        if not ssh:
            self._safe_after(0, self._append_remote_ssh_output, name,
                             "❌ ไม่พบ OpenSSH (คำสั่ง ssh) บนเครื่องนี้\n")
            return
        command = [
            ssh, "-p", str(port), "-i", key_path,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{user}@{host}", "sh -lc " + shlex.quote(command_text),
        ]
        try:
            result = proc_utils.run_hidden(
                command, capture_output=True, text=True, timeout=35)
            text = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                text = f"[exit {result.returncode}]\n{text}"
            self._safe_after(0, self._append_remote_ssh_output, name,
                             (text or "[ไม่มี output]\n")[-120000:])
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._safe_after(0, self._append_remote_ssh_output, name, f"❌ {exc}\n")
        except Exception as exc:
            self._safe_after(0, self._append_remote_ssh_output, name, f"❌ SSH command error: {exc}\n")

    def _append_remote_ssh_output(self, name: str, text: str):
        output = self._server_dash_widgets.get(name, {}).get("remote_console")
        if output is None:
            return
        try:
            if output.winfo_exists():
                output.configure(state="normal")
                output.insert("end", text)
                output.see("end")
                output.configure(state="disabled")
        except tk.TclError:
            pass

    # ══════════════════════════════════════════════════════════
    # ⚡ Quick Tunnel ต่อ server แต่ละตัว (แท็บ 🖥️ Server Manager) — เปิด cloudflared ตรงๆ
    # บนเครื่อง ไปที่ localhost:<port> ของ server นั้น เปิดพร้อมกันได้หลายตัวไม่ชนกัน
    # ══════════════════════════════════════════════════════════
    def _refresh_tunnel_row_button(self, name):
        """อัปเดตสี/ข้อความปุ่ม '⚡ Tunnel' บนแถว server ให้ตรงกับสถานะจริง (เขียว = กำลังเปิดอยู่)
           เรียกได้จากทั้งตอนสร้างแถวใหม่ (เช่น Refresh All Status) และตอน tunnel เปลี่ยนสถานะ"""
        state = self._server_quick_tunnels.get(name)
        if not state:
            return
        btn = state.get("row_btn")
        if btn is None or not btn.winfo_exists():
            return
        if state.get("proc") is not None:
            btn.configure(text="🟢 Tunnel", fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER)
        else:
            btn.configure(text="⚡ Tunnel", fg_color=Theme.PURPLE, hover_color=Theme.PURPLE_HOVER)

    def open_quick_tunnel_dialog(self, name, default_port="", row_btn=None):
        """เปิดหน้าต่าง Quick Tunnel ของ server ตัวนี้ — ปิดหน้าต่างได้โดย tunnel ยังทำงานต่อ
           เบื้องหลัง (state เก็บแยกต่อ server ไว้ใน self._server_quick_tunnels) จนกว่าจะกด 🛑 ปิดเอง"""
        state = self._server_quick_tunnels.setdefault(name, {
            "proc": None, "url": None, "port": default_port or "",
            "dialog": None, "status_label": None, "toggle_btn": None, "port_entry": None,
        })
        if row_btn is not None:
            state["row_btn"] = row_btn

        existing = state.get("dialog")
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = ctk.CTkToplevel(self)
        state["dialog"] = win
        win.title(f"⚡ Quick Tunnel — {name}")
        win.geometry("440x300")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.grab_set()

        def _on_close():
            state["dialog"] = None
            state["status_label"] = None
            state["toggle_btn"] = None
            state["port_entry"] = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        ctk.CTkLabel(win, text="⚡ Quick Tunnel", font=("Segoe UI", 18, "bold")).pack(pady=(20, 2))
        ctk.CTkLabel(win, text=name, font=("Segoe UI", 13), text_color=Theme.ACCENT).pack(pady=(0, 14))

        port_row = ctk.CTkFrame(win, fg_color="transparent")
        port_row.pack(pady=(0, 10))
        ctk.CTkLabel(port_row, text="localhost:", font=("Consolas", 13)).pack(side="left")
        port_entry = ctk.CTkEntry(port_row, width=90, font=("Consolas", 13))
        port_entry.insert(0, state.get("port") or default_port or "")
        port_entry.pack(side="left", padx=6)
        self._setup_context_menu(port_entry._entry, is_entry=True)
        state["port_entry"] = port_entry

        status_label = ctk.CTkLabel(win, text="", font=("Consolas", 12), text_color=Theme.TEXT_MUTED,
                                    justify="left", wraplength=380)
        status_label.pack(padx=20, pady=(0, 10), fill="x")
        state["status_label"] = status_label

        toggle_btn = ctk.CTkButton(win, text="⚡ เปิด Quick Tunnel", width=200,
                                   command=lambda n=name: self._toggle_server_quick_tunnel(n))
        toggle_btn.pack(pady=(0, 10))
        state["toggle_btn"] = toggle_btn

        ctk.CTkLabel(
            win,
            text="เปิด tunnel ตรงไปที่ localhost:<port> ผ่าน cloudflared บนเครื่องนี้ (ต้องติดตั้ง\n"
                 "cloudflared และเพิ่มลง PATH ก่อน) — เหมาะใช้แชร์ server ที่เพิ่ง import ให้คนอื่นดู\n"
                 "ด่วนๆ ปิดหน้าต่างนี้ได้ tunnel จะยังทำงานต่อเบื้องหลัง จนกว่าจะกด 🛑 ปิดเอง",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED, justify="left"
        ).pack(padx=20, pady=(0, 16), fill="x")

        self._refresh_tunnel_dialog_ui(name)   # ให้ตรงกับสถานะจริงทันที เผื่อเปิดซ้ำตอนกำลังรันอยู่

    def _refresh_tunnel_dialog_ui(self, name):
        state = self._server_quick_tunnels.get(name)
        if not state:
            return
        toggle_btn = state.get("toggle_btn")
        status_label = state.get("status_label")
        if toggle_btn is None or status_label is None:
            return
        if state.get("proc") is not None:
            toggle_btn.configure(text="🛑 ปิด Quick Tunnel", state="normal",
                                 fg_color=Theme.DANGER, hover_color=Theme.DANGER_HOVER)
            if state.get("url"):
                status_label.configure(text=f"✅ Quick Tunnel พร้อมใช้งาน:\n{state['url']}",
                                       text_color=Theme.SUCCESS)
            else:
                status_label.configure(text="⏳ กำลังเชื่อมต่อ...", text_color=Theme.TEXT_MUTED)
        else:
            toggle_btn.configure(text="⚡ เปิด Quick Tunnel", state="normal",
                                 fg_color=Theme.PURPLE, hover_color=Theme.PURPLE_HOVER)
            status_label.configure(
                text="ยังไม่ได้เปิด — กด ⚡ เพื่อเปิด tunnel ตรงไปที่ localhost ตาม port ด้านบนทันที",
                text_color=Theme.TEXT_MUTED)

    def _toggle_server_quick_tunnel(self, name):
        state = self._server_quick_tunnels.get(name)
        if not state:
            return
        if state.get("proc") is not None:
            self._stop_server_quick_tunnel(name)
            return

        port_entry = state.get("port_entry")
        port_text = (port_entry.get().strip() if port_entry is not None else state.get("port", ""))
        if not port_text.isdigit():
            if state.get("status_label") is not None and state["status_label"].winfo_exists():
                state["status_label"].configure(
                    text="❌ กรอกหมายเลข port ให้ถูกต้องก่อน (ตัวเลขล้วนๆ เช่น 3002)",
                    text_color=Theme.DANGER)
            return

        state["port"] = port_text
        if state.get("toggle_btn") is not None and state["toggle_btn"].winfo_exists():
            state["toggle_btn"].configure(text="⏳ กำลังเปิด...", state="disabled")
        if state.get("status_label") is not None and state["status_label"].winfo_exists():
            state["status_label"].configure(
                text="⏳ กำลังเชื่อมต่อ Cloudflare Quick Tunnel รอสักครู่...", text_color=Theme.TEXT_MUTED)
        threading.Thread(target=self._run_server_quick_tunnel_worker,
                         args=(name, port_text), daemon=True).start()

    def _run_server_quick_tunnel_worker(self, name, port: str):
        """รัน 'cloudflared tunnel --url http://localhost:<port>' ของ server ตัวนี้ตรงๆ ใน
           background thread แล้วอ่าน output แบบ byte เพื่อดักปัญหา encoding เดียวกับที่เจอใน
           execute_cmd() — เครื่อง Windows ตั้ง locale เป็นภาษาไทย (cp874) มักถอดรหัส unicode
           banner ของ cloudflared ไม่ได้ ถ้าไม่ดักไว้ thread นี้จะตายเงียบๆ ไม่มี error โผล่ให้เห็นเลย"""
        cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
        try:
            proc = proc_utils.popen_hidden(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, text=False
            )
        except FileNotFoundError:
            self._safe_after(0, self._on_server_quick_tunnel_error, name,
                             "❌ ไม่พบโปรแกรม cloudflared บนเครื่องนี้ — ติดตั้งก่อนแล้วเพิ่มลง PATH "
                             "(ดาวน์โหลดที่ github.com/cloudflare/cloudflared/releases)")
            return
        except Exception as e:
            self._safe_after(0, self._on_server_quick_tunnel_error, name,
                             f"❌ เปิด Quick Tunnel ไม่สำเร็จ: {e}")
            return

        state = self._server_quick_tunnels.get(name)
        if state is None:
            # server ถูกลบออกจากระบบไปแล้วระหว่างที่กำลังเปิด tunnel — ปิด process ทิ้ง ไม่ต้องเก็บ state
            try:
                proc.terminate()
            except Exception:
                pass
            return
        state["proc"] = proc
        state["url"] = None
        self._safe_after(0, self._on_server_quick_tunnel_started, name)

        found_url = False
        try:
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if not line:
                    continue
                try:
                    decoded = line.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        decoded = line.decode("cp874")
                    except Exception:
                        decoded = line.decode("cp850", errors="replace")
                m = QUICK_TUNNEL_URL_RE.search(decoded)
                if m and not found_url:
                    found_url = True
                    self._safe_after(0, self._on_server_quick_tunnel_url_found, name, m.group(0))
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            self._safe_after(0, self._on_server_quick_tunnel_stopped, name, found_url)

    def _on_server_quick_tunnel_started(self, name):
        self._refresh_tunnel_row_button(name)
        state = self._server_quick_tunnels.get(name, {})
        btn = state.get("toggle_btn")
        if btn is not None and btn.winfo_exists():
            btn.configure(text="🛑 ปิด Quick Tunnel", state="normal",
                         fg_color=Theme.DANGER, hover_color=Theme.DANGER_HOVER)

    def _on_server_quick_tunnel_url_found(self, name, url: str):
        state = self._server_quick_tunnels.get(name)
        if state is None:
            return
        state["url"] = url
        lbl = state.get("status_label")
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text=f"✅ Quick Tunnel พร้อมใช้งาน (คัดลอกลิงก์ไว้ในคลิปบอร์ดแล้ว):\n{url}",
                         text_color=Theme.SUCCESS)
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
        except Exception:
            pass
        self._append_action_log(f"⚡ Quick Tunnel ของ {name} พร้อมใช้งาน: {url}", "success")

    def _on_server_quick_tunnel_error(self, name, msg: str):
        state = self._server_quick_tunnels.get(name)
        if state is not None:
            state["proc"] = None
        self._refresh_tunnel_row_button(name)
        if state:
            btn = state.get("toggle_btn")
            if btn is not None and btn.winfo_exists():
                btn.configure(text="⚡ เปิด Quick Tunnel", state="normal",
                             fg_color=Theme.PURPLE, hover_color=Theme.PURPLE_HOVER)
            lbl = state.get("status_label")
            if lbl is not None and lbl.winfo_exists():
                lbl.configure(text=msg, text_color=Theme.DANGER)

    def _on_server_quick_tunnel_stopped(self, name, had_url: bool):
        state = self._server_quick_tunnels.get(name)
        if state is not None:
            state["proc"] = None
        self._refresh_tunnel_row_button(name)
        if state:
            btn = state.get("toggle_btn")
            if btn is not None and btn.winfo_exists():
                btn.configure(text="⚡ เปิด Quick Tunnel", state="normal",
                             fg_color=Theme.PURPLE, hover_color=Theme.PURPLE_HOVER)
            lbl = state.get("status_label")
            if had_url and lbl is not None and lbl.winfo_exists():
                lbl.configure(
                    text="⚫ Quick Tunnel ถูกปิดแล้ว — กด ⚡ อีกครั้งเพื่อเปิดใหม่ (จะได้ลิงก์ใหม่ทุกครั้ง)",
                    text_color=Theme.TEXT_MUTED)

    def _stop_server_quick_tunnel(self, name):
        """ปุ่ม 🛑 ปิด Quick Tunnel — สั่ง terminate process cloudflared ของ server นี้ใน
           background thread (กัน UI ค้างถ้า process ไม่ยอมปิดทันที)"""
        state = self._server_quick_tunnels.get(name)
        if not state or state.get("proc") is None:
            return
        proc = state["proc"]
        btn = state.get("toggle_btn")
        if btn is not None and btn.winfo_exists():
            btn.configure(text="⏳ กำลังปิด...", state="disabled")

        def _kill():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        threading.Thread(target=_kill, daemon=True).start()

    # ── Status check ─────────────────────────────────────────
    def get_real_status(self, name, server_type="docker", ip=""):
        if server_type == "docker":
            try:
                r = proc_utils.run_hidden(
                    ["docker", "inspect", "-f", "{{.State.Running}}", name],
                    capture_output=True, text=True, timeout=5
                )
                if "true" in r.stdout.lower():
                    return "🟢 Online", Theme.SUCCESS
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            return "⚫ Offline", Theme.TEXT_MUTED

        elif server_type == "server" and ip:
            param = "-n" if platform.system().lower() == "windows" else "-c"
            try:
                r = proc_utils.run_hidden(
                    ["ping", param, "1", "-w", "1000", ip],
                    capture_output=True, text=True, timeout=4
                )
                if r.returncode == 0:
                    return "🟢 Online", Theme.SUCCESS
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            return "⚫ Offline", Theme.TEXT_MUTED

        return "⚫ Offline", Theme.TEXT_MUTED

    def _resolve_server_name(self, user_input: str):
        """หา key จริงใน tracked_containers จากชื่อที่ผู้ใช้พิมพ์ (ไม่สนตัวพิมพ์เล็ก/ใหญ่)"""
        if not user_input:
            return None
        if user_input in self.tracked_containers:
            return user_input
        low = user_input.strip().lower()
        for key in self.tracked_containers:
            if key.lower() == low:
                return key
        return None

    def _ping_host(self, ip: str):
        """ping ip 1 ครั้ง คืน (สำเร็จหรือไม่, latency_ms หรือ None) — เป็น blocking call
           ต้องเรียกจาก background thread เท่านั้น ไม่งั้น UI จะค้าง"""
        param = "-n" if platform.system().lower() == "windows" else "-c"
        try:
            r = proc_utils.run_hidden(
                ["ping", param, "1", "-w", "1000", ip],
                capture_output=True, text=True, timeout=4
            )
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0:
                m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out, re.IGNORECASE)
                latency = float(m.group(1)) if m else None
                return True, latency
            return False, None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False, None

    def _get_docker_container_ip(self, name: str):
        """หา IP ของ docker container จาก network settings (คืน None ถ้าไม่มี เช่น ใช้ host network)"""
        try:
            r = proc_utils.run_hidden(
                ["docker", "inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                capture_output=True, text=True, timeout=5
            )
            lines = r.stdout.strip().splitlines()
            ip = lines[0].strip() if lines else ""
            return ip or None
        except (FileNotFoundError, subprocess.TimeoutExpired, IndexError):
            return None

    def _docker_exec_ping(self, name: str):
        """วัด response time ของ container ผ่าน docker exec — ใช้เป็นตัวสำรองตอนที่ ICMP
           ไปยัง IP ภายในของ container ใช้ไม่ได้ (พบบ่อยบน Docker Desktop / Windows / Mac
           เพราะ container อยู่คนละ network namespace กับเครื่อง host เลย ping ตรง IP ไม่ถึง
           ทั้งที่ container ทำงานปกติดี)"""
        try:
            start = time.time()
            r = proc_utils.run_hidden(
                ["docker", "exec", name, "echo", "pong"],
                capture_output=True, text=True, timeout=5
            )
            elapsed_ms = (time.time() - start) * 1000
            if r.returncode == 0:
                return True, elapsed_ms
            return False, None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False, None

    def open_docker_logs(self, container_name):
        if os.name == "nt":
            os.system(f'start cmd /k docker logs -f {container_name}')
        else:
            os.system(f"x-terminal-emulator -e docker logs -f {container_name} &")

    def control_server(self, action, name, status_label,
                       server_type, yaml_filename="", service_name="", ip=""):
        work_dir = self.yaml_paths.get(yaml_filename)

        def task():
            if server_type == "docker":
                try:
                    if action == "start":
                        status_label.configure(text="⏳ Starting...", text_color=Theme.WARNING)
                        if work_dir and service_name:
                            r = proc_utils.run_hidden(
                                f"docker-compose up -d {service_name}",
                                shell=True, cwd=work_dir, capture_output=True, text=True
                            )
                        else:
                            r = proc_utils.run_hidden(["docker", "start", name],
                                               capture_output=True, text=True)
                        if r.returncode == 0:
                            status_label.configure(text="🟢 Online", text_color=Theme.SUCCESS)
                            self.apply_resource_limits(name)
                            self._safe_after(0, self._update_dash_limit_label, name)
                            # เปิด Terminal สตรีม log ของ server นี้ใน Server Dashboard ทันที
                            self.start_log_stream(name)
                            self._mark_server_dash_online(name, True)
                        else:
                            status_label.configure(text="⚫ Offline", text_color=Theme.TEXT_MUTED)
                            self._safe_after(0, lambda: msgbox.showerror(
                                "Docker Error", f"ไม่สามารถเปิด {name}\n\n{r.stderr}"))

                    elif action == "stop":
                        status_label.configure(text="⏳ Stopping...", text_color=Theme.WARNING)
                        r = proc_utils.run_hidden(["docker", "stop", name], capture_output=True, text=True)
                        status_label.configure(
                            text="⚫ Offline" if r.returncode == 0 else "🟢 Online",
                            text_color=Theme.TEXT_MUTED if r.returncode == 0 else Theme.SUCCESS
                        )
                        if r.returncode == 0:
                            self.stop_log_stream(name)
                            self._mark_server_dash_online(name, False)

                    elif action == "restart":
                        status_label.configure(text="🟡 Restarting...", text_color=Theme.WARNING)
                        r = proc_utils.run_hidden(["docker", "restart", name], capture_output=True, text=True)
                        status_label.configure(
                            text="🟢 Online" if r.returncode == 0 else "❌ Error",
                            text_color=Theme.SUCCESS if r.returncode == 0 else Theme.DANGER
                        )
                        if r.returncode == 0:
                            self.apply_resource_limits(name)
                            self._safe_after(0, self._update_dash_limit_label, name)
                            self.start_log_stream(name)
                            self._mark_server_dash_online(name, True)

                except Exception as e:
                    status_label.configure(text="❌ Error", text_color=Theme.DANGER)
                    self._safe_after(0, lambda: msgbox.showerror("Error", str(e)))
            else:
                # Physical server
                if action == "start":
                    status_label.configure(text="⏳ Pinging...", text_color=Theme.WARNING)
                    time.sleep(1)
                    txt, col = self.get_real_status(name, server_type, ip)
                    status_label.configure(text=txt, text_color=col)
                elif action == "stop":
                    self._safe_after(0, lambda: msgbox.showinfo(
                        "SSH Required",
                        f"ใช้ Terminal เพื่อ SSH เข้า {name} ({ip})\nแล้วพิมพ์ sudo shutdown -h now"
                    ))
                elif action == "restart":
                    status_label.configure(text="🟡 Restarting...", text_color=Theme.WARNING)
                    time.sleep(2)
                    txt, col = self.get_real_status(name, server_type, ip)
                    status_label.configure(text=txt, text_color=col)

        threading.Thread(target=task, daemon=True).start()

    # ── Settings window (Resource Limits) ────────────────────
    def apply_resource_limits(self, server_name):
        """ใช้งานค่า Resource Limits ที่บันทึกไว้กับ container จริงผ่าน docker update แล้ว
           'ตรวจสอบผลจริง' ก่อนบอกว่าสำเร็จ — ไม่เชื่อแค่ returncode เพราะ docker update
           บางกรณี exit 0 แต่ค่าจริงไม่เปลี่ยนก็มี (เช่นลืมตั้ง --memory-swap คู่กับ --memory
           ทำให้ Docker เก็บ swap limit เดิมไว้แล้วปฏิเสธค่าใหม่แบบเงียบๆ) — เป็นสาเหตุที่ก่อนหน้านี้
           ตั้ง limit ผ่านหน้าโปรแกรมแล้ว "ดูเหมือนสำเร็จ" ทั้งที่ container ยังใช้ค่าเดิมอยู่จริง
           ตอนนี้ใช้ตรรกะเดียวกับฝั่งเว็บ (docker_ops.set_resource_limit) เพื่อไม่ให้สองที่ไม่ตรงกันอีก
           คืน (ok: bool, message: str) เสมอ — ok=False ก็บอกเหตุผลที่ชัดเจนกลับไปด้วย
           CPU 100% = ไม่จำกัด (ล้าง CPU quota), น้อยกว่านั้น = สัดส่วนของ 1 core"""
        limits = self._resource_limits.get(server_name)
        if not limits:
            return False, "ไม่มีค่า limit ที่บันทึกไว้"
        cpu_pct = limits.get("cpu_limit", 100)
        ram_mb  = limits.get("ram_limit_mb", 2048)
        try:
            r = proc_utils.run_hidden(
                ["docker", "update",
                 *docker_ops._cpu_limit_args(cpu_pct),
                 f"--memory={ram_mb}m",
                 f"--memory-swap={ram_mb}m",
                 server_name],
                capture_output=True, text=True, timeout=10
            )
        except FileNotFoundError:
            return False, "ไม่พบคำสั่ง docker บนเครื่องนี้"
        except subprocess.TimeoutExpired:
            return False, "docker update timeout"

        if r.returncode != 0:
            err = (r.stderr or r.stdout or "ไม่ทราบสาเหตุ").strip()
            return False, f"docker update ไม่สำเร็จ: {err}"

        actual_mb = docker_ops.get_actual_memory_mb(server_name)
        if actual_mb != ram_mb:
            shown = f"{actual_mb} MB" if actual_mb else "ไม่จำกัด (unlimited)"
            return False, (f"สั่งไปแล้วแต่ container ยังไม่ได้ใช้ {ram_mb} MB จริง (ตอนนี้เป็น {shown}) — "
                            "ลอง ⏹ Stop แล้ว ▶ Start container ใหม่อีกครั้ง")
        return True, f"ตั้ง RAM {ram_mb} MB / CPU {cpu_pct}% เรียบร้อย และตรวจสอบแล้วว่า container ใช้ค่านี้จริง"

    def _update_dash_limit_label(self, server_name):
        """อัปเดตข้อความ CPU/RAM Limit ที่แสดงในการ์ด Server Dashboard ให้ตรงกับค่าที่ตั้งไว้ล่าสุด
           (เรียกทุกครั้งหลังบันทึก limit ใหม่ ไม่ว่าจะจาก ⚙️ Limits หรือคำสั่ง /limit ram)
           สำหรับ container docker จะตรวจสอบ "ค่าจริง" ที่ container ใช้อยู่คู่กันด้วย (background
           thread) ถ้าไม่ตรงกับค่าที่บันทึกไว้จะขึ้นคำเตือน ⚠️ ต่อท้ายให้เห็นตรงการ์ดเลย ไม่ต้องเดา"""
        widgets = self._server_dash_widgets.get(server_name)
        if not widgets:
            return
        lbl = widgets.get("limit_label")
        if lbl is None or not lbl.winfo_exists():
            return
        limits = self.get_limit(server_name)
        cpu_val = limits.get("cpu_limit", 100)
        cpu_txt = "ไม่จำกัด" if cpu_val >= 100 else f"{cpu_val}%"
        ram_target = limits.get("ram_limit_mb", 2048)
        lbl.configure(
            text=f"⚙️ Resource Limit ที่ตั้งไว้ — CPU: {cpu_txt}  |  RAM: {ram_target} MB",
            text_color=Theme.TEXT_ACCENT)

        info = self.tracked_containers.get(server_name, {})
        if info.get("stype") == "docker":
            threading.Thread(target=self._verify_dash_limit_worker,
                            args=(server_name, ram_target), daemon=True).start()

    def _verify_dash_limit_worker(self, server_name, ram_target):
        actual_mb = docker_ops.get_actual_memory_mb(server_name)
        self._safe_after(0, self._apply_limit_verify_result, server_name, ram_target, actual_mb)

    def _apply_limit_verify_result(self, server_name, ram_target, actual_mb):
        """อัปเดต limit_label อีกรอบพร้อมค่า 'จริง' ที่ตรวจสอบแล้ว — เรียกบน main thread เท่านั้น"""
        widgets = self._server_dash_widgets.get(server_name)
        if not widgets:
            return
        lbl = widgets.get("limit_label")
        if lbl is None or not lbl.winfo_exists():
            return
        limits = self.get_limit(server_name)
        cpu_val = limits.get("cpu_limit", 100)
        cpu_txt = "ไม่จำกัด" if cpu_val >= 100 else f"{cpu_val}%"
        base = f"⚙️ Resource Limit ที่ตั้งไว้ — CPU: {cpu_txt}  |  RAM: {ram_target} MB"
        if actual_mb == ram_target:
            lbl.configure(text=base, text_color=Theme.TEXT_ACCENT)
        else:
            shown = f"{actual_mb} MB" if actual_mb else "ไม่จำกัด"
            lbl.configure(text=f"{base}  ⚠️ (container ใช้จริง: {shown})", text_color=Theme.WARNING)

    def open_settings(self, server_name):
        limits = self.get_limit(server_name)
        ai_on = limits.get("ai_ram_enabled", False)
        win = ctk.CTkToplevel(self)
        win.title(f"Resource Limits — {server_name}")
        win.geometry("420x420" if ai_on else "420x380")
        win.attributes("-topmost", True)
        win.grab_set()

        ctk.CTkLabel(win, text="⚙️ ตั้งค่า Resource Limits",
                     font=("Segoe UI", 18, "bold")).pack(pady=(20, 4))
        ctk.CTkLabel(win, text=server_name, font=("Segoe UI", 13),
                     text_color=Theme.ACCENT).pack(pady=(0, 16))

        # CPU Limit slider
        cpu_frame = ctk.CTkFrame(win, fg_color="transparent")
        cpu_frame.pack(fill="x", padx=24, pady=8)
        ctk.CTkLabel(cpu_frame, text="CPU Limit (%):", font=("Segoe UI", 13)).pack(side="left")
        cpu_val = ctk.StringVar(value=str(limits.get("cpu_limit", 100)))
        ctk.CTkLabel(cpu_frame, textvariable=cpu_val, font=("Segoe UI", 13, "bold"),
                     text_color=Theme.TEXT_ACCENT, width=50).pack(side="right")
        cpu_slider = ctk.CTkSlider(win, from_=10, to=100, number_of_steps=90,
                                   command=lambda v: cpu_val.set(f"{int(v)}"))
        cpu_slider.set(limits.get("cpu_limit", 100))
        cpu_slider.pack(fill="x", padx=24, pady=(0, 12))

        # RAM Limit entry — ปิดแก้ไขถ้า 🤖 AI RAM เปิดอยู่ (ให้ AI คุมค่านี้แทน)
        ram_frame = ctk.CTkFrame(win, fg_color="transparent")
        ram_frame.pack(fill="x", padx=24, pady=8)
        ctk.CTkLabel(ram_frame, text="RAM Limit (MB):", font=("Segoe UI", 13)).pack(side="left")
        ram_entry = ctk.CTkEntry(ram_frame, placeholder_text="เช่น 1024", width=130)
        ram_entry.insert(0, str(limits.get("ram_limit_mb", 2048)))
        if ai_on:
            ram_entry.configure(state="disabled")
        ram_entry.pack(side="right")

        if ai_on:
            ctk.CTkLabel(
                win,
                text="🤖 RAM ถูกจัดการอัตโนมัติโดย AI อยู่ในขณะนี้ (ปุ่ม 🤖 AUTO mode: ON\n"
                     "ที่หน้า Server Manager) — ปิดสวิตช์นั้นก่อนถ้าต้องการตั้งค่า RAM เอง",
                font=("Segoe UI", 11), text_color=Theme.WARNING, justify="left",
            ).pack(fill="x", padx=24, pady=(0, 4))

        # Note label
        ctk.CTkLabel(win,
                     text="💡 ค่านี้จะถูกใช้งานทันทีถ้า container กำลังรันอยู่\n"
                          "และจะถูกใช้งานซ้ำอัตโนมัติทุกครั้งที่กด ▶ Start / 🔄 Restart\n"
                          "(CPU 100% = ไม่จำกัด)",
                     font=("Segoe UI", 11), text_color=Theme.TEXT_MUTED,
                     justify="left").pack(pady=8, padx=24, anchor="w")

        def save():
            try:
                cpu_pct = int(cpu_val.get())
                current = self.get_limit(server_name)
                still_ai_on = current.get("ai_ram_enabled", False)

                if still_ai_on:
                    ram_mb = current.get("ram_limit_mb", 2048)  # RAM ถูก AI คุมอยู่ — ไม่รับค่าจากฟอร์มนี้
                else:
                    ram_mb = int(ram_entry.get())
                    if ram_mb <= 0:
                        raise ValueError

                self._resource_limits[server_name] = {
                    "cpu_limit": cpu_pct,
                    "ram_limit_mb": ram_mb,
                    "ai_ram_enabled": still_ai_on,
                    "auto_restart_unhealthy": current.get("auto_restart_unhealthy", False),
                }
                self._save_limits()
                self._update_dash_limit_label(server_name)

                info = self.tracked_containers.get(server_name, {})
                if info.get("stype") == "docker":
                    ok, msg = self.apply_resource_limits(server_name)
                else:
                    ok, msg = True, "บันทึกแล้ว (server ประเภท physical ต้องตั้งค่านี้เองที่เครื่องปลายทาง)"
                win.destroy()

                if ok:
                    msgbox.showinfo("บันทึกสำเร็จ",
                                    f"Limits ของ {server_name}\nCPU: {cpu_pct}%  RAM: {ram_mb} MB\n\n{msg}")
                else:
                    msgbox.showwarning("บันทึกค่าไว้แล้ว แต่ยังไม่ได้ผลจริงกับ container",
                                       f"Limits ของ {server_name}\nCPU: {cpu_pct}%  RAM: {ram_mb} MB\n\n⚠️ {msg}")
            except ValueError:
                msgbox.showerror("ข้อผิดพลาด", "กรุณาใส่ตัวเลข RAM ที่ถูกต้อง (มากกว่า 0)")

        ctk.CTkButton(win, text="💾 บันทึกการตั้งค่า",
                      fg_color=Theme.SUCCESS, font=("Segoe UI", 14, "bold"),
                      command=save).pack(pady=24)

    # ══════════════════════════════════════════════════════════
    # แท็บ 5: Terminal
    # ══════════════════════════════════════════════════════════
    def build_terminal(self):
        tab = self.tab_terminal
        ctk.CTkLabel(tab, text="💻 Command Terminal",
                     font=("Segoe UI", 15, "bold")).pack(pady=8)

        self.terminal_output = ctk.CTkTextbox(
            tab, width=1000, height=480,
            font=("Consolas", 13), fg_color=Theme.BG_TERMINAL, text_color=Theme.TEXT_ACCENT
        )
        self.terminal_output.pack(pady=6, padx=10, fill="both", expand=True)
        self.terminal_output.insert(
            "end",
            "AI Server Terminal  —  type a command and press Enter\n"
            f"Current dir: {os.getcwd()}\n\n"
        )
        self.terminal_output.configure(state="disabled")

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(bar, text="CMD > ", font=("Consolas", 13, "bold"),
                     text_color=Theme.WARNING).pack(side="left")
        self.cmd_entry = ctk.CTkEntry(bar, width=820, font=("Consolas", 13))
        self.cmd_entry.pack(side="left", padx=8)
        self.cmd_entry.bind("<Return>", self.execute_cmd)
        ctk.CTkButton(bar, text="🚀 Run", width=80,
                      fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER,
                      command=self.execute_cmd).pack(side="left")

        # ── กด ↑/↓ ทวนคำสั่งที่เคยรัน, กด Tab เดา/วนดูคำสั่ง (กดตอนช่องว่างก็ได้) ──
        self._attach_shell_navigation(
            self.cmd_entry, lambda: self.terminal_history, TERMINAL_COMMAND_SUGGESTIONS
        )

        ctk.CTkLabel(
            bar, text="(↑↓ ทวนคำสั่งเก่า • Tab เดา/วนดูคำสั่ง)",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED
        ).pack(side="left", padx=(10, 0))

        self._setup_context_menu(self.cmd_entry._entry, is_entry=True)
        self._setup_context_menu(self.terminal_output._textbox, is_entry=False)

    def write_to_terminal(self, text):
        self.terminal_output.configure(state="normal")
        self.terminal_output.insert("end", text)
        self.terminal_output.see("end")
        self.terminal_output.configure(state="disabled")

    def execute_cmd(self, event=None):
        cmd = self.cmd_entry.get().strip()
        if not cmd:
            return
        self.cmd_entry.delete(0, "end")
        # เก็บลง history ไว้ทวนด้วย ↑/↓ (ไม่เก็บซ้ำติดกันเป๊ะๆ กัน list บวมโดยไม่มีประโยชน์)
        if not self.terminal_history or self.terminal_history[-1] != cmd:
            self.terminal_history.append(cmd)
            if len(self.terminal_history) > 200:
                self.terminal_history.pop(0)
        cwd = os.getcwd()
        self.write_to_terminal(f"\n{cwd}> {cmd}\n")

        cmd_lower = cmd.lower()

        # Drive switch (E:, D:, C:)
        dm = re.match(r"^([a-zA-Z]:)$", cmd_lower)
        if dm:
            try:
                os.chdir(dm.group(1).upper() + "\\")
            except Exception:
                self.write_to_terminal("Cannot find the drive.\n")
            return

        # cd command
        if cmd_lower.startswith("cd") and (
            len(cmd_lower) == 2 or cmd_lower[2] in (" ", "/")
        ) or cmd_lower == "cd..":
            parts = cmd.strip().split(maxsplit=1)
            target = ""
            if cmd_lower == "cd..":
                target = ".."
            elif len(parts) > 1:
                target = parts[1].strip()
                if target.lower().startswith("/d "):
                    target = target[3:].strip()
                target = target.strip('"\'')
            if target:
                try:
                    os.chdir(target)
                    self.write_to_terminal(f"[OK] {os.getcwd()}\n")
                except Exception:
                    self.write_to_terminal("Cannot find path.\n")
            else:
                self.write_to_terminal(f"{os.getcwd()}\n")
            return

        # Other commands via subprocess
        def run_in_thread():
            try:
                # ซ่อนหน้าต่าง CMD ให้อัตโนมัติผ่าน proc_utils แล้ว (ดู proc_utils.py) — ไม่ต้อง
                # คำนวณ STARTUPINFO เองตรงนี้อีก (เดิมส่ง startupinfo=None ตอนไม่ใช่ Windows ทับค่า
                # default ของ proc_utils จนไม่มีผลอะไรเลยตอนเป็น Windows จริงๆ ด้วยซ้ำ)
                proc = proc_utils.popen_hidden(
                    cmd, shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    cwd=os.getcwd(), text=False
                )
                found_output = False
                while True:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                    if line:
                        found_output = True
                        try:
                            decoded = line.decode("utf-8")
                        except UnicodeDecodeError:
                            try:
                                decoded = line.decode("cp874")
                            except Exception:
                                decoded = line.decode("cp850", errors="replace")
                        self._safe_after(0, self.write_to_terminal, decoded)
                if not found_output:
                    self._safe_after(0, self.write_to_terminal, "[No output]\n")
            except Exception as e:
                self._safe_after(0, self.write_to_terminal, f"Error: {e}\n")

        threading.Thread(target=run_in_thread, daemon=True).start()

    def _attach_shell_navigation(self, entry, get_history, suggestions):
        """ผูกปุ่ม ↑/↓ (ทวนคำสั่งเก่าแบบ shell ทั่วไป) และ Tab (เดาคำสั่ง/วนดูคำสั่งที่มี แม้ไม่ได้
           พิมพ์อะไรเลยก็กด Tab ได้ — เผื่อลืมคำสั่งหรือไม่อยากพิมพ์เอง) ให้กับช่องพิมพ์คำสั่ง (CTkEntry)
           ใดๆ ก็ได้ — ใช้ร่วมกันทั้งแท็บ 💻 Terminal หลัก และช่องคำสั่งใน container การ์ดแต่ละใบ

           entry:       CTkEntry ที่จะผูกปุ่มให้
           get_history: callable ไม่รับ argument คืน list[str] ของคำสั่งที่เคยรันไปแล้ว (เก่า→ใหม่)
                        ใช้ callable แทน list ตรงๆ เพราะบาง entry (เช่นของ container) ต้องเปลี่ยน
                        list เป้าหมายได้ตามชื่อ container ที่ปิด/เปิดใหม่ได้
           suggestions: list[str] คำสั่งพื้นฐานที่ควรมีให้เลือกเสมอ (รวมกับ history ตอนกด Tab)
                        หรือจะส่งเป็น callable ไม่รับ argument ที่คืน list[str] ก็ได้ (เผื่อรายการ
                        คำสั่งเปลี่ยนไปตามสถานะปัจจุบัน เช่น ชื่อ server ที่มีอยู่ตอนนี้ในระบบ)

           ออกแบบให้ "ซ่อม state เองได้" (self-healing) แทนการดัก event <Key> ทั่วไปเพื่อ reset —
           เพราะ <Key> จะโดนยิงซ้ำกับ <Up>/<Down>/<Tab> ที่ผูกไว้เฉพาะอยู่แล้ว ทำให้ลำดับ event
           คุมยาก จึงเช็คจากข้อความปัจจุบันในช่องแทนว่าตรงกับผลลัพธ์ล่าสุดที่เราเติมให้เองหรือไม่"""
        nav_state = {"hist_idx": None, "saved_text": "", "tab_matches": None}

        def _build_pool():
            # เอาคำสั่งที่เคยใช้ล่าสุดขึ้นก่อน (ใกล้เคียงความต้องการมากสุด) ตามด้วยคำสั่งพื้นฐาน
            # ที่มีให้เสมอ — ตัดตัวซ้ำออก (คงลำดับที่เจอก่อน)
            sugg_list = suggestions() if callable(suggestions) else suggestions
            seen = set()
            pool = []
            for cmd in list(reversed(get_history())) + list(sugg_list):
                if cmd not in seen:
                    seen.add(cmd)
                    pool.append(cmd)
            return pool

        def on_up(event):
            history = get_history()
            if not history:
                return "break"
            if nav_state["hist_idx"] is None:
                nav_state["saved_text"] = entry.get()
                nav_state["hist_idx"] = len(history)
            if nav_state["hist_idx"] > 0:
                nav_state["hist_idx"] -= 1
            entry.delete(0, "end")
            entry.insert(0, history[nav_state["hist_idx"]])
            return "break"

        def on_down(event):
            if nav_state["hist_idx"] is None:
                return "break"
            history = get_history()
            nav_state["hist_idx"] += 1
            entry.delete(0, "end")
            if nav_state["hist_idx"] >= len(history):
                nav_state["hist_idx"] = None
                entry.insert(0, nav_state["saved_text"])
            else:
                entry.insert(0, history[nav_state["hist_idx"]])
            return "break"

        def on_tab(event):
            current = entry.get()
            matches = nav_state["tab_matches"]
            if matches and current in matches:
                # กด Tab ซ้ำต่อจากผลลัพธ์ที่เราเติมให้เอง → วนไปคำสั่งถัดไปในชุดเดิม
                idx = (matches.index(current) + 1) % len(matches)
            else:
                # เพิ่งเริ่มกด Tab ใหม่ (หรือผู้ใช้พิมพ์อะไรใหม่ก่อนหน้านี้) → กรองคำสั่งจากข้อความ
                # ปัจจุบันเป็น prefix ใหม่ทั้งหมด (ช่องว่าง = โชว์คำสั่งทั้งหมดให้เลือก)
                pool = _build_pool()
                matches = [c for c in pool if c.lower().startswith(current.lower())] if current else pool
                if not matches:
                    return "break"   # ไม่มีคำสั่งไหนตรงเลย ปล่อยข้อความเดิมไว้
                idx = 0
            entry.delete(0, "end")
            entry.insert(0, matches[idx])
            nav_state["tab_matches"] = matches
            return "break"

        entry.bind("<Up>", on_up)
        entry.bind("<Down>", on_down)
        entry.bind("<Tab>", on_tab)

    def _setup_context_menu(self, widget, is_entry=False):
        menu = tk.Menu(self, tearoff=0, font=("Segoe UI", 11))

        def _paste():
            widget.focus_set()
            try:
                text = self.clipboard_get()
                if is_entry:
                    try:
                        widget.delete("sel.first", "sel.last")
                    except Exception:
                        pass
                    widget.insert("insert", text)
            except Exception:
                pass

        menu.add_command(label="📋 Paste", command=_paste)
        menu.add_command(label="📄 Copy",
                         command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_separator()
        menu.add_command(label="✂️ Cut",
                         command=lambda: widget.event_generate("<<Cut>>"))

        def _select_all():
            widget.focus_set()
            if is_entry:
                widget.select_range(0, "end")
            else:
                widget.tag_add("sel", "1.0", "end")

        menu.add_command(label="☑️ Select All", command=_select_all)
        widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        widget.bind("<Button-2>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    # ══════════════════════════════════════════════════════════
    # แท็บ 6: Action Log
    # ══════════════════════════════════════════════════════════
    def build_log_tab(self):
        tab = self.tab_log
        ctk.CTkLabel(tab, text="📋 Action Log · รับเหตุการณ์ระบบอัตโนมัติ",
                     font=("Segoe UI", 16, "bold")).pack(pady=10)

        self.action_log_box = ctk.CTkTextbox(
            tab, width=1000, height=440,
            font=("Consolas", 12), fg_color=Theme.BG_TERMINAL,
            text_color=Theme.LOG_INFO, border_width=1, border_color=Theme.BORDER
        )
        self.action_log_box.pack(pady=6, padx=10, fill="both", expand=True)
        # ── สีข้อความตามระดับความสำคัญ: แจ้งเตือน/ผิดพลาด = แดง, คำเตือน = เหลือง,
        #    สำเร็จ = เขียว, คำสั่งที่พิมพ์ = ฟ้า accent, ข้อมูลทั่วไป = เทา ──
        self.action_log_box.tag_config("lvl_danger",  foreground=Theme.LOG_DANGER)
        self.action_log_box.tag_config("lvl_warning", foreground=Theme.LOG_WARNING)
        self.action_log_box.tag_config("lvl_success", foreground=Theme.LOG_SUCCESS)
        self.action_log_box.tag_config("lvl_prompt",  foreground=Theme.LOG_PROMPT)
        self.action_log_box.tag_config("lvl_info",    foreground=Theme.LOG_INFO)
        self.action_log_box.insert("end", "— ยังไม่มี action —\n", ("lvl_info",))
        self.action_log_box.configure(state="disabled")
        self._action_log_has_entries = False

        # ── แถบสั่งงานด้วยคำสั่งสั้น (slash commands) ──
        ctk.CTkLabel(
            tab,
            text=("คำสั่งที่ใช้ได้:  /ping <ชื่อserver>  |  /ping all  |  "
                  "/limit ram <ชื่อserver> <RAM_MB>  |  /ral <ชื่อserver> ram <%>  |  "
                  "/ral <ชื่อserver> cpu <%>  |  /ral rest <ชื่อserver>\n"
                  "(ชื่อ server ต้องตรงกับที่ตั้งไว้ในแท็บ 🖥️ Server Manager)"),
            font=("Consolas", 11), text_color=Theme.TEXT_MUTED, justify="left", anchor="w"
        ).pack(fill="x", padx=10, pady=(0, 4))

        cmd_bar = ctk.CTkFrame(tab, fg_color="transparent")
        cmd_bar.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkLabel(cmd_bar, text="CMD >", font=("Consolas", 13, "bold"),
                     text_color=Theme.WARNING).pack(side="left")
        self.log_cmd_entry = ctk.CTkEntry(cmd_bar, font=("Consolas", 13),
                                          placeholder_text="เช่น /ping all")
        self.log_cmd_entry.pack(side="left", padx=8, fill="x", expand=True)
        self.log_cmd_entry.bind("<Return>", self._handle_log_command)
        ctk.CTkButton(cmd_bar, text="🚀 Run", width=80,
                      fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER,
                      command=self._handle_log_command).pack(side="left")
        self._setup_context_menu(self.log_cmd_entry._entry, is_entry=True)

        # ── กด ↑/↓ ทวนคำสั่งที่เคยรัน, กด Tab เดา/วนดูคำสั่ง (กดตอนช่องว่างก็ได้) ──
        # รายการ suggestion เป็น callable เพราะอยากให้ใช้ชื่อ server ปัจจุบันเสมอ (เพิ่ม/ลบ server
        # ที่แท็บ 🖥️ Server Manager แล้วรายการต้องอัปเดตตาม ไม่ใช่ค้างรายชื่อเก่าตอนเปิดแอป)
        self._attach_shell_navigation(
            self.log_cmd_entry, lambda: self.log_command_history,
            self._build_log_command_suggestions
        )
        ctk.CTkLabel(
            cmd_bar, text="(↑↓ ทวนคำสั่งเก่า • Tab เดา/วนดูคำสั่ง)",
            font=("Segoe UI", 10), text_color=Theme.TEXT_MUTED
        ).pack(side="left", padx=(10, 0))

        ctk.CTkButton(tab, text="🗑️ ล้าง Log", fg_color=Theme.NEUTRAL,
                      command=self._clear_action_log).pack(pady=6)
        ctk.CTkButton(tab, text="📤 ส่งออกรายงานเหตุการณ์ CSV", fg_color=Theme.ACCENT,
                      command=self._export_incident_report).pack(pady=(0, 10))
        self._sync_action_log_events()

    def _sync_action_log_events(self):
        """Mirror every newly recorded system event into the desktop Action Log."""
        box = getattr(self, "action_log_box", None)
        if box is None:
            return
        try:
            if not box.winfo_exists():
                return
            events = web_server.web_state.event_history(limit=2000)
        except Exception:
            return
        recent_cutoff = time.monotonic() - 8.0
        self._recent_action_log_texts = {
            text: [stamp for stamp in stamps if stamp >= recent_cutoff]
            for text, stamps in self._recent_action_log_texts.items()
            if any(stamp >= recent_cutoff for stamp in stamps)
        }
        found_new_event = False
        for event in reversed(events):
            event_id = str(event.get("event_id") or "")
            if not event_id:
                try:
                    event_id = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    event_id = repr(event)
            if event_id in self._action_event_ids:
                continue
            self._action_event_ids.add(event_id)
            found_new_event = True
            message = str(event.get("text") or "เหตุการณ์ระบบ")
            # Many actions already write a concise line directly to the textbox.
            # Mark their persisted counterpart as seen without showing it twice.
            recent_direct_logs = self._recent_action_log_texts.get(message, [])
            if recent_direct_logs:
                recent_direct_logs.pop(0)
                continue
            stamp = self._parse_event_datetime(event.get("timestamp"))
            time_label = (stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp
                          else str(event.get("time") or "เวลาไม่ทราบ"))
            category = str(event.get("category") or "event")
            outcome = str(event.get("outcome") or "").strip()
            suffix = f" [{outcome}]" if outcome else ""
            line = f"[{time_label}] [{category}]{suffix} {message}"
            details = event.get("details") or {}
            if (outcome in ("failed", "error") or "ล้มเหลว" in message
                    or "ไม่สำเร็จ" in message):
                level = "danger"
            elif (category in ("anomaly", "predictive_alert")
                  or details.get("current") == "unhealthy"):
                level = "warning"
            elif outcome == "success" or category in ("recovery", "auto_remediation_recovery"):
                level = "success"
            else:
                level = self._classify_log_level(message)
            self._append_action_log(line, level=level)
        if found_new_event and hasattr(self, "_ops_events"):
            self._ops_events = events
            if hasattr(self, "_ops_kpi_values"):
                self._update_operations_kpis()
            try:
                if (self.server_history_server_menu.get() == "เหตุการณ์ระบบ"
                        and hasattr(self, "server_history_canvas")):
                    self._draw_server_history_graph()
            except (AttributeError, tk.TclError):
                pass

    def _export_incident_report(self):
        """Export persisted monitoring/health/remediation events without inventing KPIs."""
        from server import EVENT_HISTORY_FILE

        destination = filedialog.asksaveasfilename(
            title="บันทึกรายงานเหตุการณ์", defaultextension=".csv",
            initialfile=f"incident_report_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")])
        if not destination:
            return
        events = []
        try:
            with open(EVENT_HISTORY_FILE, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        except FileNotFoundError:
            events = []
        except OSError as exc:
            msgbox.showerror("ส่งออกรายงานไม่สำเร็จ", str(exc), parent=self)
            return

        try:
            with open(destination, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow(["timestamp", "category", "outcome", "event", "details"])
                for event in events:
                    writer.writerow([
                        event.get("timestamp", ""), event.get("category", ""),
                        event.get("outcome", ""), event.get("text", ""),
                        json.dumps(event.get("details", {}), ensure_ascii=False, default=str),
                    ])
        except OSError as exc:
            msgbox.showerror("บันทึกรายงานไม่สำเร็จ", str(exc), parent=self)
            return
        msgbox.showinfo("ส่งออกรายงานแล้ว", f"บันทึก {len(events):,} เหตุการณ์\n{destination}", parent=self)

    def _clear_action_log(self):
        self.action_log_box.configure(state="normal")
        self.action_log_box.delete("1.0", "end")
        self.action_log_box.insert("end", "— ยังไม่มี action —\n", ("lvl_info",))
        self._action_log_has_entries = False
        self.action_log_box.configure(state="disabled")

    def _classify_log_level(self, text: str) -> str:
        """เดาระดับความสำคัญของข้อความ log จาก emoji/คำนำหน้า เพื่อกำหนดสีตัวหนังสือ
           แจ้งเตือน/ผิดพลาด → แดง, คำเตือน → เหลือง, สำเร็จ → เขียว, คำสั่งที่พิมพ์ → ฟ้า"""
        stripped = text.lstrip("\n").lstrip()
        if stripped.startswith(">"):
            return "prompt"
        if stripped.startswith(("🚨", "❌", "🔴")):
            return "danger"
        if stripped.startswith(("⚠️", "🟡")):
            return "warning"
        if stripped.startswith(("✅", "🟢")):
            return "success"
        if any(k in text for k in ("[Error]", "[Timeout]", "ล้มเหลว", "ไม่สำเร็จ")):
            return "danger"
        return "info"

    def _append_action_log(self, text: str, level: str = None):
        """เพิ่มข้อความลงแท็บ Action Log พร้อมใส่สีตามความสำคัญโดยอัตโนมัติ
           (แจ้งเตือน/ผิดพลาด = ตัวหนังสือสีแดง) — ระบุ level เองได้ถ้าต้องการบังคับสี
           ('danger' | 'warning' | 'success' | 'prompt' | 'info')"""
        if level is None:
            level = self._classify_log_level(text)
        self._recent_action_log_texts.setdefault(text, []).append(time.monotonic())
        tag = f"lvl_{level}"
        self.action_log_box.configure(state="normal")
        if not self._action_log_has_entries:
            self.action_log_box.delete("1.0", "end")
            self._action_log_has_entries = True
        self.action_log_box.insert("end", text + "\n", (tag,))
        self.action_log_box.see("end")
        self.action_log_box.configure(state="disabled")

    def _build_log_command_suggestions(self) -> list:
        """สร้างรายการคำสั่งให้ Tab เดา/วนดูในแท็บ Action Log — ใช้ชื่อ server จริงที่มีอยู่ตอนนี้
           (จากแท็บ 🖥️ Server Manager) แทนที่จะเป็นแค่ placeholder เฉยๆ จะได้กด Tab แล้วรันได้เลย"""
        names = list(self.tracked_containers.keys())
        suggestions = ["/ping all"]
        for n in names:
            suggestions += [f"/ping {n}", f"/limit ram {n} 512",
                            f"/ral {n} ram 80", f"/ral {n} cpu 80", f"/ral rest {n}"]
        return suggestions

    # ── Slash commands ในแท็บ Action Log (/ping, /limit, /ral) ──
    def _handle_log_command(self, event=None):
        raw = self.log_cmd_entry.get().strip()
        if not raw:
            return
        self.log_cmd_entry.delete(0, "end")
        # เก็บลง history ไว้ทวนด้วย ↑/↓ (ไม่เก็บซ้ำติดกันเป๊ะๆ กัน list บวมโดยไม่มีประโยชน์)
        if not self.log_command_history or self.log_command_history[-1] != raw:
            self.log_command_history.append(raw)
            if len(self.log_command_history) > 200:
                self.log_command_history.pop(0)
        self._append_action_log(f"\n> {raw}")

        if not raw.startswith("/"):
            self._append_action_log("❌ คำสั่งต้องขึ้นต้นด้วย / เช่น /ping all")
            return

        parts = raw.split()
        cmd = parts[0].lower()

        if cmd == "/ping":
            self._cmd_ping(parts[1:])
        elif cmd == "/limit":
            self._cmd_limit(parts[1:])
        elif cmd == "/ral":
            self._cmd_ral(parts[1:])
        else:
            self._append_action_log(
                f"❌ ไม่รู้จักคำสั่ง: {cmd}  (ใช้ได้: /ping, /limit, /ral)")

    # /ping <name>  หรือ  /ping all
    def _cmd_ping(self, args):
        if not args:
            self._append_action_log("Usage: /ping <ชื่อserver>  หรือ  /ping all")
            return
        target = args[0]
        if target.lower() == "all":
            if not self.tracked_containers:
                self._append_action_log("⚠️ ยังไม่มี server ในระบบ (ไปเพิ่มที่แท็บ 🖥️ Server Manager ก่อน)")
                return
            self._append_action_log(f"⏳ กำลัง ping server ทั้งหมด ({len(self.tracked_containers)} ตัว)...")
            threading.Thread(target=self._ping_all_worker, daemon=True).start()
        else:
            name = self._resolve_server_name(target)
            if not name:
                self._append_action_log(f"❌ ไม่พบ server ชื่อ '{target}' (เช็คชื่อในแท็บ Server Manager)")
                return
            self._append_action_log(f"⏳ กำลัง ping {name}...")
            threading.Thread(target=self._ping_one_worker, args=(name,), daemon=True).start()

    def _ping_all_worker(self):
        for name in list(self.tracked_containers.keys()):
            self._ping_one_worker(name)

    def _ping_one_worker(self, name):
        """ping server ตัวเดียว — ทำงานใน background thread เท่านั้น (blocking subprocess ข้างใน)"""
        info = self.tracked_containers.get(name, {})
        stype = info.get("stype")
        ip = info.get("ip", "")

        if stype == "docker":
            status_text, _ = self.get_real_status(name, "docker")
            if "Online" not in status_text:
                self._safe_after(0, self._append_action_log, f"🔴 {name} — container ไม่ได้ทำงาน (offline)")
                return

            container_ip = self._get_docker_container_ip(name)
            ok, latency = (False, None)
            if container_ip:
                ok, latency = self._ping_host(container_ip)

            if ok:
                latency_txt = f"{latency:.1f} ms" if latency is not None else "ตอบกลับ (ไม่ทราบ ms)"
                self._safe_after(0, self._append_action_log,
                          f"🟢 {name} ({container_ip}) — ping สำเร็จ: {latency_txt}")
                return

            # ICMP ตรง IP ของ container ใช้ไม่ได้ (พบบ่อยบน Docker Desktop / Windows / Mac
            # เพราะ container อยู่คนละ network namespace กับเครื่อง host) → วัดผ่าน docker exec แทน
            ok2, latency2 = self._docker_exec_ping(name)
            ip_txt = f" ({container_ip})" if container_ip else ""
            if ok2:
                self._safe_after(0, self._append_action_log,
                          f"🟢 {name}{ip_txt} — container ทำงานปกติ ตอบสนอง {latency2:.1f} ms "
                          "(ผ่าน docker exec — ICMP ไปยัง IP ของ container ใช้ไม่ได้บนเครื่องนี้)")
            else:
                self._safe_after(0, self._append_action_log,
                          f"🟡 {name}{ip_txt} — container online แต่ตรวจสอบ response ไม่ได้ (docker exec ล้มเหลว)")
            return

        # physical server
        if not ip:
            self._safe_after(0, self._append_action_log, f"❌ {name}: ไม่มี IP ให้ ping")
            return

        ok, latency = self._ping_host(ip)
        if ok:
            latency_txt = f"{latency:.1f} ms" if latency is not None else "ตอบกลับ (ไม่ทราบ ms)"
            self._safe_after(0, self._append_action_log, f"🟢 {name} ({ip}) — ping สำเร็จ: {latency_txt}")
        else:
            self._safe_after(0, self._append_action_log, f"🔴 {name} ({ip}) — ping ไม่สำเร็จ (timeout/unreachable)")

    # /limit ram <name> <MB>
    def _cmd_limit(self, args):
        # ยอมรับได้ทั้ง "/limit ram <ชื่อserver> <MB>" และ "/limit <ชื่อserver> ram <MB>"
        # (คนสับสนกับคำสั่ง /ral ที่ใช้ 'ram' ต่อท้ายชื่อได้ง่าย) — ตัดคำว่า "ram" ที่เป็นแค่
        # label ออกไปก่อนไม่ว่าจะอยู่ตำแหน่งไหน เหลือแค่ [ชื่อ, ตัวเลข] ให้ parse ต่อ
        filtered = [a for a in args if a.lower() != "ram"]
        if len(filtered) < 2:
            self._append_action_log(
                "Usage: /limit ram <ชื่อserver> <RAM_MB>  (หรือ /limit <ชื่อserver> ram <RAM_MB> ก็ได้)")
            return
        name_raw, amount_raw = filtered[0], filtered[1]
        name = self._resolve_server_name(name_raw)
        if not name:
            self._append_action_log(f"❌ ไม่พบ server ชื่อ '{name_raw}'")
            return
        try:
            ram_mb = int(amount_raw)
            if ram_mb <= 0:
                raise ValueError
        except ValueError:
            self._append_action_log("❌ จำนวน RAM ต้องเป็นตัวเลขจำนวนเต็มมากกว่า 0 (หน่วย MB)")
            return

        current = self.get_limit(name)
        if current.get("ai_ram_enabled", False):
            self._append_action_log(
                f"❌ RAM ของ {name} ถูกจัดการอัตโนมัติโดย 🤖 AUTO mode อยู่ — "
                "ปิดสวิตช์นั้นที่หน้า Server Manager ก่อนถึงจะตั้งค่าด้วยมือได้", "warning")
            return
        self._resource_limits[name] = {
            "cpu_limit": current.get("cpu_limit", 100),
            "ram_limit_mb": ram_mb,
            "ai_ram_enabled": False,
            "auto_restart_unhealthy": current.get("auto_restart_unhealthy", False),
        }
        self._save_limits()
        self._update_dash_limit_label(name)

        info = self.tracked_containers.get(name, {})
        if info.get("stype") == "docker":
            self._append_action_log(f"⏳ ตั้งค่า RAM limit ของ {name} = {ram_mb} MB กำลังนำไปใช้...")
            threading.Thread(target=self._apply_limit_worker, args=(name, ram_mb), daemon=True).start()
        else:
            self._append_action_log(
                f"✅ บันทึก RAM limit ของ {name} = {ram_mb} MB แล้ว "
                "(server ประเภท physical ต้องตั้งค่านี้เองที่เครื่องปลายทาง)")

    def _apply_limit_worker(self, name, ram_mb):
        ok, msg = self.apply_resource_limits(name)
        self._safe_after(0, self._append_action_log, ("✅ " if ok else "⚠️ ") + msg, "success" if ok else "warning")
        self._safe_after(0, self._update_dash_limit_label, name)

    # /ral <name> ram <%>  |  /ral <name> cpu <%>  |  /ral rest <name>
    def _cmd_ral(self, args):
        if len(args) >= 2 and args[0].lower() == "rest":
            name = self._resolve_server_name(args[1])
            if not name:
                self._append_action_log(f"❌ ไม่พบ server ชื่อ '{args[1]}'")
                return
            self._alert_thresholds.pop(name, None)
            self._save_alerts()
            self._append_action_log(
                f"✅ รีเซ็ตการแจ้งเตือนของ {name} กลับเป็นค่าเริ่มต้น "
                f"(แจ้งเมื่อ CPU/RAM > {DEFAULT_ALERT['cpu_pct']:.0f}%)")
            return

        if len(args) < 3:
            self._append_action_log(
                "Usage: /ral <ชื่อserver> ram <%>  |  /ral <ชื่อserver> cpu <%>  |  /ral rest <ชื่อserver>")
            return

        name = self._resolve_server_name(args[0])
        if not name:
            self._append_action_log(f"❌ ไม่พบ server ชื่อ '{args[0]}'")
            return

        metric = args[1].lower()
        if metric not in ("ram", "cpu"):
            self._append_action_log("❌ ระบุประเภทเป็น ram หรือ cpu เท่านั้น เช่น /ral webapp ram 85")
            return

        try:
            pct = float(args[2])
            if not (0 < pct <= 100):
                raise ValueError
        except ValueError:
            self._append_action_log("❌ ค่าที่ตั้งต้องเป็นตัวเลข 1-100 (หน่วย %)")
            return

        current = self.get_alert(name)
        current["ram_pct" if metric == "ram" else "cpu_pct"] = pct
        self._alert_thresholds[name] = current
        self._save_alerts()

        info = self.tracked_containers.get(name, {})
        note = "" if info.get("stype") == "docker" else \
            " ⚠️ (server นี้เป็น physical — ยังไม่มีการเก็บค่า CPU/RAM แบบเรียลไทม์ จึงยังแจ้งเตือนอัตโนมัติไม่ได้)"
        self._append_action_log(
            f"✅ ตั้งค่าแจ้งเตือน {metric.upper()} ของ {name} เมื่อเกิน {pct:.0f}%{note}")

    # ══════════════════════════════════════════════════════════
    # 🚨 หน้าต่างแจ้งเตือน Anomaly แบบ custom — ต้องดูเร่งด่วน/ฉุกเฉินจริงๆ และลอยอยู่บนสุด
    # ต่อให้ตอนนั้นเปิดโปรแกรมอื่นอยู่ก็ตาม (ไม่ใช่ tkinter messagebox ธรรมดาแบบเดิมที่หน้าตา
    # จืดๆ ดูเหมือนป๊อปอัพไวรัสทั่วไป — ใช้หน้าต่างดีไซน์เองแทน มี branding ของโปรแกรมชัดเจน
    # เพื่อให้ดูน่าเชื่อถือ ไม่ใช่ป๊อปอัพหลอกลวง)
    # ══════════════════════════════════════════════════════════
    def _show_anomaly_alert(self, metrics: dict, risk_score: float = 0.0):
        cpu = metrics.get("cpu", 0)
        ram = metrics.get("ram", 0)
        disk = metrics.get("disk", 0)
        now_txt = time.strftime("%Y-%m-%d %H:%M:%S")

        # มีหน้าต่างแจ้งเตือนเปิดค้างอยู่แล้ว → อัปเดตตัวเลขในบานเดิมแทนที่จะเปิดซ้อนบานที่ 2
        win = self._anomaly_alert_win
        if win is not None and win.winfo_exists():
            self._anomaly_body_label.configure(
                text=self._format_anomaly_body(cpu, ram, disk, risk_score, now_txt))
            self._force_window_to_front(win)
            return

        win = ctk.CTkToplevel(self)
        self._anomaly_alert_win = win
        win.title("🚨 Intelligent Server Operations Platform — แจ้งเตือนฉุกเฉิน")
        win.resizable(False, False)
        win.configure(fg_color=Theme.BG_ROOT)
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_anomaly_alert(win))

        # จัดกลาง "จอ" ไม่ใช่กลางหน้าต่างหลักเฉยๆ — เผื่อหน้าต่างหลักถูกย่อ/อยู่มุมจอก็ยังเห็นชัด
        win_w, win_h = 480, 420
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{win_w}x{win_h}+{(sw - win_w) // 2}+{(sh - win_h) // 2}")

        # ── แถบหัวสีแดงเข้ม กระพริบเบาๆ ดึงความสนใจ (ไม่ฉูดฉาดจนดูเหมือนสแปม) ──
        header = ctk.CTkFrame(win, fg_color=Theme.DANGER, corner_radius=0, height=90)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="🚨", font=("Segoe UI", 34)).pack(side="left", padx=(24, 10))
        head_txt = ctk.CTkFrame(header, fg_color="transparent")
        head_txt.pack(side="left", fill="y", pady=14)
        ctk.CTkLabel(head_txt, text="ตรวจพบความผิดปกติของระบบ", font=("Segoe UI", 18, "bold"),
                     text_color="#ffffff", anchor="w").pack(anchor="w")
        ctk.CTkLabel(head_txt, text="Intelligent Server Operations Platform ตรวจพบพฤติกรรมผิดปกติ (Anomaly)",
                     font=("Segoe UI", 12), text_color="#ffe8e8", anchor="w").pack(anchor="w")

        # ── รายละเอียดตัวเลข ──
        body = ctk.CTkFrame(win, fg_color=Theme.BG_CARD, border_width=1, border_color=Theme.BORDER)
        body.pack(fill="both", expand=True, padx=18, pady=18)
        self._anomaly_body_label = ctk.CTkLabel(
            body, text=self._format_anomaly_body(cpu, ram, disk, risk_score, now_txt),
            font=("Consolas", 14), justify="left", anchor="nw")
        self._anomaly_body_label.pack(fill="both", expand=True, padx=18, pady=18)

        # ── ปุ่มด้านล่าง ──
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=18, pady=(0, 18))
        ctk.CTkButton(btn_row, text="📋 ดู Action Log", width=150,
                      fg_color=Theme.NEUTRAL, hover_color=Theme.NEUTRAL_HOVER,
                      command=lambda: self._goto_action_log_from_alert(win)).pack(side="left")
        ctk.CTkButton(btn_row, text="✅ รับทราบ", width=150,
                      fg_color=Theme.SUCCESS, hover_color=Theme.SUCCESS_HOVER,
                      command=lambda: self._close_anomaly_alert(win)).pack(side="right")

        self._force_window_to_front(win)
        self._pulse_anomaly_header(header, cycles=12, bright=True)
        try:
            self.bell()   # เสียงเตือนสั้นๆ ของระบบ เสริมความรู้สึกเร่งด่วน
        except Exception:
            pass

    def _format_anomaly_body(self, cpu, ram, disk, risk_score, now_txt) -> str:
        def sev(v):
            return "🔴 สูงมาก" if v >= 90 else ("🟡 สูง" if v >= 70 else "🟢 ปกติ")
        return (
            f"เวลา:         {now_txt}\n\n"
            f"CPU:          {cpu:.1f}%   {sev(cpu)}\n"
            f"RAM:          {ram:.1f}%   {sev(ram)}\n"
            f"Disk:         {disk:.1f}%   {sev(disk)}\n\n"
            f"Risk Score:   {risk_score:.0f}%\n\n"
            "ระบบกำลังตรวจสอบและอาจดำเนินการแก้ไขอัตโนมัติให้\n"
            "(ดูรายละเอียด/ประวัติเพิ่มเติมได้ที่แท็บ 📋 Action Log)"
        )

    def _force_window_to_front(self, win):
        """บังคับหน้าต่างแจ้งเตือนให้ลอยอยู่บนสุดจริงๆ ต่อให้ตอนนั้นโฟกัสอยู่ที่โปรแกรมอื่น
           (เบราว์เซอร์/เกม/ฯลฯ) — Windows มักกันไม่ให้โปรแกรมพื้นหลังแย่ง focus ไปตรงๆ ได้ง่ายๆ
           เลยใช้เทคนิคสลับ -topmost ปิด/เปิดสองสามรอบ + lift()/focus_force() ซ้ำๆ ช่วยดันขึ้นมา"""
        try:
            win.deiconify()
            win.attributes("-topmost", True)
            win.lift()
            win.focus_force()
            win.after(80, lambda: win.attributes("-topmost", False) if win.winfo_exists() else None)
            win.after(160, lambda: self._reassert_topmost(win))
        except Exception:
            pass

    def _reassert_topmost(self, win):
        if not win.winfo_exists():
            return
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()

    def _pulse_anomaly_header(self, header, cycles=12, bright=True):
        """กระพริบแถบหัวสีแดงเบาๆ สลับเข้ม/อ่อน ทุก ~0.5 วินาที ดึงสายตาโดยไม่ฉูดฉาดเกินไป
           หยุดเองหลังครบจำนวนรอบ หรือหยุดทันทีถ้าหน้าต่างถูกปิดไปก่อนแล้ว"""
        if not header.winfo_exists() or cycles <= 0:
            self._anomaly_pulse_after_id = None
            return
        header.configure(fg_color=Theme.DANGER if bright else Theme.DANGER_HOVER)
        self._anomaly_pulse_after_id = self.after(
            500, lambda: self._pulse_anomaly_header(header, cycles - 1, not bright))

    def _goto_action_log_from_alert(self, win):
        self._close_anomaly_alert(win)
        self.tabview.set("📋 Action Log")

    def _close_anomaly_alert(self, win):
        if self._anomaly_pulse_after_id is not None:
            try:
                self.after_cancel(self._anomaly_pulse_after_id)
            except Exception:
                pass
            self._anomaly_pulse_after_id = None
        if self._anomaly_alert_win is win:
            self._anomaly_alert_win = None
        try:
            win.destroy()
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # Main update loop (2 วินาที)
    # ══════════════════════════════════════════════════════════
    def _check_predictive_capacity_alerts(self, forecasts: dict | None,
                                          interval_seconds: int,
                                          *, new_sample: bool) -> str | None:
        """Raise a logged early warning when the trained LSTM projects sustained >90% use.

        This reports a forecast, not a confirmed incident. Forecast alerts are
        generated only from the trained LSTM and only once per projected episode.
        """
        if not new_sample:
            return None
        state_by_metric = self._predictive_alert_state
        alert_messages = []
        sample_interval = max(1, int(interval_seconds or 15))
        forecasts = forecasts if isinstance(forecasts, dict) else {}
        horizon_seconds = sample_interval * max(
            (len(forecasts.get(key, [])) - 1 for key in ("cpu", "ram", "disk")),
            default=0)
        for metric in ("cpu", "ram", "disk"):
            state = state_by_metric.setdefault(metric, {
                "streak": 0, "clear_streak": 0, "active": False, "last_sent": 0.0,
            })
            try:
                values = [float(value) for value in (forecasts or {}).get(metric, [])]
            except (TypeError, ValueError):
                values = []
            values = [value for value in values if math.isfinite(value)]
            current = values[0] if values else None
            crossing = None
            if current is not None and current < 90.0:
                for index in range(1, len(values) - 1):
                    if values[index] >= 90.0 and values[index + 1] >= 90.0:
                        crossing = index
                        break

            if crossing is None:
                state["streak"] = 0
                if not values or max(values) < 85.0:
                    state["clear_streak"] += 1
                    if state["clear_streak"] >= 3:
                        state["active"] = False
                else:
                    state["clear_streak"] = 0
                continue

            state["clear_streak"] = 0
            state["streak"] += 1
            if state["active"] or state["streak"] < 2:
                continue
            now = time.time()
            if now - float(state.get("last_sent", 0.0)) < 600:
                continue

            lead_seconds = crossing * sample_interval
            predicted_peak = max(values[crossing:])
            label = metric.upper()
            minutes = max(1, round(lead_seconds / 60))
            message = (f"🔮 LSTM คาดการณ์ว่า {label} อาจแตะ 90% ในประมาณ {minutes} นาที "
                       f"(ตอนนี้ {current:.1f}%, คาดสูงสุด {predicted_peak:.1f}%)")
            alert_messages.append(message)
            state.update({"active": True, "last_sent": now, "streak": 0})
            details = {
                "metric": metric, "current_percent": round(current, 2),
                "threshold_percent": 90.0,
                "predicted_peak_percent": round(predicted_peak, 2),
                "lead_time_seconds": lead_seconds,
                "forecast_horizon_seconds": horizon_seconds,
                "sample_interval_seconds": sample_interval,
                "model": "LSTM",
                "note": "ค่าพยากรณ์เพื่อเตือนล่วงหน้า ไม่ใช่เหตุการณ์ที่ยืนยันแล้ว",
            }
            self._append_action_log(message, level="warning")
            try:
                web_server.web_state.add_event(
                    message, category="predictive_alert", details=details,
                    outcome="forecast")
            except Exception:
                pass
            if self.notifier.config.get("popup_enabled", True):
                self._show_toast(message)

        return " · ".join(alert_messages) if alert_messages else None

    def update_realtime_status(self):
        """Keep the UI refresh loop alive when an isolated update fails."""
        try:
            self._sync_action_log_events()
            self._update_realtime_status_once()
        except Exception as exc:
            now = time.monotonic()
            if now - self._last_status_error_log >= 30:
                self._last_status_error_log = now
                try:
                    self._append_action_log(f"⚠️ อัปเดตสถานะล้มเหลวชั่วคราว: {exc}", level="warning")
                except Exception:
                    pass
            self._safe_after(5000, self.update_realtime_status)

    def _update_realtime_status_once(self):
        # ─ อ่าน metrics จริงจาก psutil ─
        current = self.collector.get_current_metrics()
        history_saved = self.collector.save_to_db(current)
        if history_saved:
            self.brain.add_lstm_sample(current)
            self._lstm_samples_since_fit += 1
            if (self._lstm_samples_since_fit >= 36
                    and not self.brain.get_lstm_status().get("trained")
                    and not self._lstm_setup_in_progress):
                self._prepare_lstm_model(force=True)

        # ─ อัปเดตหน้า Server Dashboard จาก cache (เร็ว ไม่บล็อก UI) ─
        self._refresh_server_dashboard_ui()

        cpu  = current.get("cpu",  0)
        ram  = current.get("ram",  0)
        disk = current.get("disk", 0)

        # ─ อัปเดต history (สำหรับ graph) ─
        for lst, val in [(self._cpu_hist, cpu), (self._ram_hist, ram), (self._disk_hist, disk)]:
            lst.append(val)
            if len(lst) > 60:
                lst.pop(0)

        # ─ อัปเดต labels & bars ─
        def bar_color(v):
            if v >= 90: return Theme.DANGER
            if v >= 70: return Theme.WARNING
            return None  # ใช้ default

        self.cpu_label.configure(text=f"{cpu}%",
                                 text_color=bar_color(cpu) or Theme.ACCENT)
        self.cpu_bar.set(cpu / 100.0)
        self.ram_label.configure(text=f"{ram}%",
                                 text_color=bar_color(ram) or Theme.SUCCESS)
        self.ram_bar.set(ram / 100.0)
        self.disk_label.configure(text=f"{disk}%",
                                  text_color=bar_color(disk) or Theme.WARNING)
        self.disk_bar.set(disk / 100.0)

        # ─ Risk score (EMA แบบเสถียร ไม่ต้องเทรน) ─
        status = self.brain.check_anomaly(current)
        risk    = self.brain.get_risk_score()
        trend   = self.brain.predict_trend()
        self.risk_label.configure(text=f"⚠️ Risk Score: {risk:.0f}%")
        self.risk_bar.set(risk / 100.0)
        self.trend_label.configure(text=f"📈 แนวโน้ม CPU: {trend}")
        volatility = 0.0
        recent_changes = [abs(values[-1] - values[-2]) for values in
                          (self._cpu_hist, self._ram_hist, self._disk_hist) if len(values) >= 2]
        if recent_changes:
            volatility = sum(recent_changes) / len(recent_changes)
        stability = self._stability_index(current, volatility)
        stability_color = Theme.SUCCESS if stability >= 85 else (Theme.WARNING if stability >= 65 else Theme.DANGER)
        self.stability_label.configure(text=f"เสถียรภาพ {stability:.0f}/100", text_color=stability_color)
        lstm_interval = int(self.brain.get_lstm_status().get("sample_interval_seconds", 15) or 15)
        # Cap work at one forecast per persisted sample. Very sparse training
        # data cannot support a useful 10-minute warning, so alerts stay disabled.
        lstm_steps = (min(40, max(2, 600 // max(1, lstm_interval)))
                      if 0 < lstm_interval <= 300 else 0)
        if history_saved:
            self._cached_lstm_capacity_forecast = (
                self.brain.forecast_lstm(steps=lstm_steps) if lstm_steps else None)
        lstm_predictions = self._cached_lstm_capacity_forecast
        predictive_warning = self._check_predictive_capacity_alerts(
            lstm_predictions, lstm_interval, new_sample=history_saved)
        predictions = lstm_predictions or self.brain.forecast_metrics(
            {"cpu": self._cpu_hist, "ram": self._ram_hist, "disk": self._disk_hist}, steps=10)
        if predictions:
            projected = {key: values[-1] for key, values in predictions.items()}
            projected_stability = self._stability_index(projected)
            if lstm_predictions:
                interval = int(self.brain.get_lstm_status().get("sample_interval_seconds", 15) or 15)
                horizon_minutes = max(1, round((len(lstm_predictions["cpu"]) - 1) * interval / 60))
                self.forecast_label.configure(
                    text=(f"LSTM คาดการณ์ ~{horizon_minutes} นาที · CPU {projected['cpu']:.1f}% / "
                          f"RAM {projected['ram']:.1f}% / Disk {projected['disk']:.1f}% "
                          f"(ช่วงข้อมูล ~{interval} วินาที)"
                          + (f" · {predictive_warning}" if predictive_warning else "")
                          ))
            else:
                self.forecast_label.configure(
                    text=f"เส้นทึบ: ข้อมูลจริง   เส้นประ: คาดการณ์ ~25 วินาที · เสถียรภาพอาจอยู่ที่ {projected_stability:.0f}/100")
        else:
            self.forecast_label.configure(text="เส้นทึบ: ข้อมูลจริง   เส้นประ: รอข้อมูลอย่างน้อย 6 ตัวอย่างก่อนคาดการณ์")

        if status == -1:
            self.ai_status_frame.configure(fg_color=Theme.DANGER_BG)
            self.ai_status_label.configure(
                text="⚠️ ตรวจพบพฤติกรรมผิดปกติ! (Anomaly)",
                text_color=Theme.TEXT_PRIMARY)

            # ─ แจ้งเตือน Popup บนเครื่องนี้ ─
            self.notifier.notify_anomaly(self, current, risk)
            # เขียนเหตุการณ์เฉพาะตอนเปลี่ยนจากปกติเป็นผิดปกติ ป้องกัน log ซ้ำทุก 2 วินาที
            if not self._last_anomaly_state:
                web_server.web_state.add_event(
                    f"ตรวจพบความผิดปกติ: CPU {cpu}% / RAM {ram}% / Disk {disk}% "
                    f"/ Swap {current.get('swap_percent', 0)}%",
                    category="anomaly",
                    details={**current, "risk": risk,
                             "isolation_forest_score": self.brain.last_anomaly_score,
                             "model": self.brain.get_model_status()})

            # ─ Remediation พร้อม cooldown ป้องกันการสั่งซ้ำถี่เกินไปตอนผิดปกติต่อเนื่อง ─
            action_text = ""
            now = time.time()
            if now - self._last_remediation_ts >= self._remediation_cooldown:
                if ram > 90:
                    action_text = self.remediator.execute_action("High RAM Usage")
                elif cpu > 90 and current.get("cpu_high_duration_sec", 0) >= 60:
                    action_text = self.remediator.execute_action("Anomalous CPU Behavior")
                elif disk > 90:
                    action_text = self.remediator.execute_action("Disk Full")
                if action_text:
                    self._last_remediation_ts = now
                    self.healing_label.configure(text=action_text, text_color=Theme.WARNING)
                    self._append_action_log(action_text, level="warning")
                    web_server.web_state.add_event(
                        f"การจัดการเหตุการณ์: {action_text}", category="remediation",
                        details={**current, "risk": risk}, outcome=action_text)

            web_server.web_state.update(
                **current, thresholds=self.brain.get_thresholds(), risk=risk, trend=trend,
                model_status=self.brain.get_model_status(),
                lstm_status=self.brain.get_lstm_status(),
                isolation_forest_score=self.brain.last_anomaly_score,
                is_anomaly=True,
                ai_status_text="ตรวจพบพฤติกรรมผิดปกติ",
                last_action=action_text or web_server.web_state.snapshot().get("last_action", ""),
            )
        else:
            if self._last_anomaly_state:
                web_server.web_state.add_event(
                    f"สถานะกลับสู่ปกติ: CPU {cpu}% / RAM {ram}% / Disk {disk}%",
                    category="recovery", details={**current, "risk": risk})
            self.ai_status_frame.configure(fg_color=Theme.ACCENT_BG)
            self.ai_status_label.configure(
                text="✅ ระบบปกติ — กำลังเฝ้าระวัง",
                text_color=Theme.TEXT_ACCENT)
            self.healing_label.configure(text="", text_color=Theme.TEXT_SECONDARY)

            web_server.web_state.update(
                **current, thresholds=self.brain.get_thresholds(), risk=risk, trend=trend,
                model_status=self.brain.get_model_status(),
                lstm_status=self.brain.get_lstm_status(),
                isolation_forest_score=self.brain.last_anomaly_score,
                is_anomaly=False,
                ai_status_text="ระบบทำงานปกติ",
            )

        self._last_anomaly_state = status == -1

        # Update the visible dashboard graphs only. Coalesce redraw work so
        # continuous window resizing does not rebuild the canvases per event.
        try:
            dashboard_visible = self.tabview.get() == "📊 Dashboard"
        except (AttributeError, tk.TclError):
            dashboard_visible = True
        if dashboard_visible:
            self._schedule_canvas_redraw("dashboard_mini", self._draw_graph, 80)
            self._schedule_canvas_redraw("dashboard_trend", self._draw_dashboard_trend_graph, 80)

        self.after(2000, self.update_realtime_status)


# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Start with a single CTk/Tk root. Creating and destroying a separate Tk
    # root for the splash can leave Tcl unusable for AIServerApp on Windows.
    app = AIServerApp()
    app.mainloop()
