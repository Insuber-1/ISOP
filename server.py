"""
server.py — Web server สำหรับดูสถานะระบบจากระยะไกล (มือถือ/คอมอื่น)
ใช้ Flask สำหรับ Server Dashboard และ API ควบคุมเซิร์ฟเวอร์

วิธีทำงาน:
- main.py เก็บข้อมูล metrics/anomaly แล้วเรียก web_state.update(...) ทุกครั้งที่เช็คสถานะ
- หน้าเว็บ dashboard.html จะ poll API /api/status ทุก 2 วินาทีเพื่ออัปเดตหน้าจอ
- Dashboard และ API เปิดโดยไม่ต้อง Login/Setup

รันแยกจาก main.py ไม่ได้ตั้งใจ — ถูกออกแบบให้ main.py import แล้วสั่ง start
เป็น background thread ในตัวเอง (รันไฟล์เดียวจบ)
"""
import json
import os
import subprocess
import threading
import time
import uuid
from flask import Flask, request, jsonify, render_template, redirect, url_for
import docker_ops
import proc_utils
from paths import app_path, resource_path

# ── ไฟล์เดียวกับที่ main.py เซฟไว้ตอน Import/ลบ YAML ใน Server Manager ──
# ใช้ filter /api/containers ไม่ให้เว็บโชว์ container ที่ยังไม่ได้ import ผ่าน UI
# (เช่น container ที่มาจาก docker-compose.yml ของตัวโปรแกรมเองที่ auto-start ตอนเปิดแอป)
# ต้องเป็น path เดียวกันเป๊ะกับ main.py (ทั้งคู่ใช้ app_path() จาก paths.py) ไม่งั้นจะกลายเป็น
# คนละไฟล์กัน แล้วเว็บ dashboard กับหน้าเดสก์ท็อปจะเห็นรายชื่อ container ไม่ตรงกัน
TRACKED_CONTAINERS_FILE = app_path("tracked_containers.json")
EVENT_HISTORY_FILE = app_path("event_history.jsonl")


def _load_tracked_container_names() -> set:
    """อ่านรายชื่อ container ที่ถูก Import เข้า Server Manager แล้ว (เซฟโดย main.py)
       คืน set ว่างถ้ายังไม่มีไฟล์/อ่านไม่ได้ — จะได้ไม่โชว์อะไรเลยแทนที่จะพัง"""
    if os.path.exists(TRACKED_CONTAINERS_FILE):
        try:
            with open(TRACKED_CONTAINERS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _get_container_stats(names: list[str]) -> dict[str, dict]:
    """Read Docker CPU and memory usage for running tracked containers in one call."""
    if not names:
        return {}
    try:
        result = proc_utils.run_hidden(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}", *names],
            capture_output=True, text=True, timeout=12
        )
        if result.returncode != 0:
            return {}
        stats = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                name, cpu, memory, memory_percent = parts
                stats[name] = {
                    "cpu_percent": cpu.strip(),
                    "memory_usage": memory.strip(),
                    "memory_percent": memory_percent.strip(),
                }
        return stats
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}



class WebState:
    """เก็บสถานะล่าสุดแบบ thread-safe ง่ายๆ ให้ Flask อ่านได้"""
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "cpu": 0, "ram": 0, "disk": 0,
            "risk": 0, "trend": "รอข้อมูล...",
            "is_anomaly": False,
            "ai_status_text": "⏳ กำลังเริ่มระบบเฝ้าระวัง",
            "last_action": "",
            "updated_at": 0,
        }
        # โหลดเหตุการณ์ล่าสุดจากดิสก์ เพื่อให้ประวัติยังอยู่หลังปิดโปรแกรม
        self._events: list[dict] = self._load_events()

    @staticmethod
    def _load_events() -> list[dict]:
        if not os.path.isfile(EVENT_HISTORY_FILE):
            return []
        events = []
        try:
            with open(EVENT_HISTORY_FILE, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict) and event.get("text"):
                        events.append(event)
            for index, event in enumerate(events[-2000:]):
                event.setdefault("event_id", f"legacy-{index}-{event.get('timestamp', '')}")
            return events[-2000:][::-1]
        except OSError:
            return []

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)
            self._data["updated_at"] = time.time()

    def add_event(self, text: str, *, category: str = "event",
                  details: dict | None = None, outcome: str | None = None):
        with self._lock:
            event = {"event_id": uuid.uuid4().hex,
                     "text": text, "time": time.strftime("%H:%M:%S"),
                     "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                     "category": category}
            if details is not None:
                event["details"] = details
            if outcome is not None:
                event["outcome"] = outcome
            self._events.insert(0, event)
            self._events = self._events[:2000]
            try:
                os.makedirs(os.path.dirname(EVENT_HISTORY_FILE), exist_ok=True)
                # Keep the append-only log bounded while preserving the newest
                # 2,000 records for incident review.
                try:
                    if os.path.getsize(EVENT_HISTORY_FILE) > 5 * 1024 * 1024:
                        with open(EVENT_HISTORY_FILE, "r", encoding="utf-8") as old:
                            recent = old.readlines()[-2000:]
                        with open(EVENT_HISTORY_FILE, "w", encoding="utf-8") as old:
                            old.writelines(recent)
                except FileNotFoundError:
                    pass
                with open(EVENT_HISTORY_FILE, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                print(f"[WebState] บันทึก event history ไม่สำเร็จ: {exc}")

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._data)
            # Keep the frequently polled web status payload compact.
            d["events"] = list(self._events[:200])
            return d

    def event_history(self, limit: int = 2000) -> list[dict]:
        """Return recent full event records for the desktop Action Log."""
        with self._lock:
            return [dict(event) for event in self._events[:max(1, int(limit))]]


web_state = WebState()


def create_app() -> Flask:
    app = Flask(__name__, template_folder=resource_path("templates"))

    @app.route("/")
    def index():
        return redirect(url_for("dashboard"))

    # Old links remain valid but the login and setup pages are no longer served.
    @app.route("/login", methods=["GET", "POST"])
    def login():
        return redirect(url_for("dashboard"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        return redirect(url_for("dashboard"))

    @app.route("/logout")
    def logout():
        return redirect(url_for("dashboard"))

    def _is_tracked_server(name: str) -> bool:
        """Allow remote operations only for servers imported into Server Manager."""
        return name in _load_tracked_container_names()

    @app.route("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/api/status")
    def api_status():
        return jsonify(web_state.snapshot())

    @app.route("/api/events")
    def api_events():
        try:
            limit = max(1, min(200, int(request.args.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        return jsonify({"events": web_state.snapshot().get("events", [])[:limit]})

    # ══════════════════════════════════════════════════════════
    # Remote control API — คุม container จากเว็บได้เหมือนโปรแกรมเดสก์ท็อป
    # ทุก route ใช้ docker_ops.py (standalone, ไม่พึ่ง Tkinter) จึงปลอดภัย
    # เรียกจาก thread ของ Flask เอง ไม่กระทบ main.py เลย
    # ══════════════════════════════════════════════════════════
    @app.route("/api/containers")
    def api_containers():
        # ── กรองเฉพาะ container ที่ Import เข้า Server Manager แล้วเท่านั้น ──
        # (ก่อนหน้านี้เรียก docker_ops.list_containers() ตรงๆ เลยโชว์ container
        #  ทุกตัวบนเครื่อง รวมถึงตัวที่ auto-start จาก docker-compose.yml ของโปรแกรมเอง
        #  ทั้งที่ยังไม่ได้กด Import ผ่านหน้าเว็บ/แอปเลย)
        tracked = _load_tracked_container_names()
        containers = [c for c in docker_ops.list_containers() if c["name"] in tracked]
        running_names = [c["name"] for c in containers if c.get("running")]
        stats = _get_container_stats(running_names)
        for c in containers:
            limit = docker_ops.get_resource_limit(c["name"])
            c["limit_cpu"] = limit.get("cpu_limit", 100)
            c["limit_ram_mb"] = limit.get("ram_limit_mb", 2048)
            c["resources"] = stats.get(c["name"], {})
        return jsonify({"containers": containers})

    @app.route("/api/action", methods=["POST"])
    def api_action():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        action = (data.get("action") or "").strip()
        if not name or action not in ("start", "stop", "restart"):
            return jsonify({"ok": False, "message": "ข้อมูลไม่ครบ (name/action)"}), 400
        if not _is_tracked_server(name):
            return jsonify({"ok": False, "message": "ไม่พบเซิร์ฟเวอร์นี้ใน Server Manager"}), 404
        ok, msg = docker_ops.docker_action(name, action)
        if ok:
            web_state.add_event(f"🌐 [เว็บ] {action} '{name}' — {msg}")
        else:
            web_state.add_event(f"🌐 [เว็บ] {action} '{name}' ล้มเหลว — {msg}")
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/exec", methods=["POST"])
    def api_exec():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        cmd = (data.get("cmd") or "").strip()
        if not name or not cmd:
            return jsonify({"ok": False, "message": "ข้อมูลไม่ครบ (name/cmd)"}), 400
        if not _is_tracked_server(name):
            return jsonify({"ok": False, "message": "ไม่พบเซิร์ฟเวอร์นี้ใน Server Manager"}), 404
        ok, output = docker_ops.docker_exec_cmd(name, cmd)
        web_state.add_event(f"🌐 [เว็บ] exec '{name}' {'สำเร็จ' if ok else 'ล้มเหลว'}")
        return jsonify({"ok": ok, "output": output})

    @app.route("/api/ping", methods=["POST"])
    def api_ping():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "message": "ไม่ได้ระบุชื่อ server"}), 400
        if not _is_tracked_server(name):
            return jsonify({"ok": False, "message": "ไม่พบเซิร์ฟเวอร์นี้ใน Server Manager"}), 404
        ok, msg = docker_ops.docker_ping(name)
        web_state.add_event(f"🌐 [เว็บ] ตรวจสอบ '{name}' {'สำเร็จ' if ok else 'ไม่สำเร็จ'} — {msg}")
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/limit", methods=["POST"])
    def api_limit():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        try:
            cpu_pct = float(data.get("cpu_pct", 100))
            ram_mb = int(data.get("ram_mb", 2048))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "ค่า CPU/RAM ต้องเป็นตัวเลข"}), 400
        if not name or ram_mb <= 0 or not (0 < cpu_pct <= 100):
            return jsonify({"ok": False, "message": "ข้อมูลไม่ถูกต้อง"}), 400
        if not _is_tracked_server(name):
            return jsonify({"ok": False, "message": "ไม่พบเซิร์ฟเวอร์นี้ใน Server Manager"}), 404
        ok, msg = docker_ops.set_resource_limit(name, cpu_pct, ram_mb)
        web_state.add_event(f"🌐 [เว็บ] ตั้ง limit '{name}' RAM {ram_mb}MB/CPU {cpu_pct}% — {msg}")
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/logs/<name>")
    def api_logs(name):
        if not _is_tracked_server(name):
            return jsonify({"ok": False, "message": "ไม่พบเซิร์ฟเวอร์นี้ใน Server Manager"}), 404
        text = docker_ops.get_logs(name, lines=150)
        web_state.add_event(f"🌐 [เว็บ] เปิดดู logs ของ '{name}'")
        return jsonify({"logs": text})

    return app


def run_server(host: str = "0.0.0.0", port: int = 5000):
    """เรียกจาก main.py ใน background thread"""
    app = create_app()
    # use_reloader=False สำคัญมาก ไม่งั้น Flask จะพยายามรันตัวเองซ้ำ 2 ครั้งตอนอยู่ใน thread
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def start_server_background(host: str = "0.0.0.0", port: int = 5000) -> threading.Thread:
    t = threading.Thread(target=run_server, args=(host, port), daemon=True)
    t.start()
    return t
