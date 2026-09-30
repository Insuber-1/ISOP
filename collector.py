"""
collector.py — เก็บข้อมูล metrics จากเครื่องจริงด้วย psutil
รองรับทั้ง Windows และ Linux/macOS
"""
import csv
import json
import os
import random
import threading
import time
from collections import deque
import psutil
from datetime import datetime, timedelta

from paths import app_path


class DataCollector:
    HISTORY_INTERVAL_SEC = 15.0
    SERVER_HEALTH_INTERVAL_SEC = 60.0
    DISK_DETAILS_INTERVAL_SEC = 60.0
    LOG_SCAN_INTERVAL_SEC = 60.0
    # เดิม default เป็น "metrics_db.csv" แบบ relative — ขึ้นกับ current working directory ตอนรัน
    # เปลี่ยนเป็น absolute path ผ่าน app_path() กันไว้เฉยๆ (main.py ปกติจะส่ง path มาเองอยู่แล้ว)
    def __init__(self, db_file: str = None):
        self.db_file = db_file or app_path("metrics_db.csv")
        # เก็บ metrics ที่เพิ่มจาก schema เดิมแยกไฟล์ เพื่อไม่ทำให้กราฟ/รายงานเดิม
        # ที่อ่าน metrics_db.csv เสียรูปแบบ
        self.extended_db_file = os.path.join(os.path.dirname(self.db_file), "metrics_extended.csv")
        self.server_health_file = app_path("server_health_history.csv")
        self._server_health_lock = threading.Lock()
        self._server_health_saved_at = {}
        self._last_save_monotonic = 0.0
        self._last_sample_monotonic = None
        self._last_disk_io = None
        self._last_net_io = None
        self._last_network_scan = 0.0
        self._network_connection_count = ""
        self._cpu_high_since = None
        self._last_disk_details_scan = float("-inf")
        self._disk_usage_by_mount = {}
        self._last_log_size_scan = float("-inf")
        self._log_size_bytes = 0

    # ------------------------------------------------------------------
    # อ่านค่าจริงจากระบบปฏิบัติการ
    # ------------------------------------------------------------------
    def get_current_metrics(self) -> dict:
        try:
            # Do not block Tk's UI thread while taking a sample. psutil keeps a
            # system-wide baseline between calls, so subsequent reads are live.
            cpu  = round(psutil.cpu_percent(interval=None), 1)
            ram  = round(psutil.virtual_memory().percent, 1)

            # Disk: ใช้พาร์ทิชันหลัก (C:\ บน Windows, / บน Linux)
            disk_path = "C:\\" if os.name == "nt" else "/"
            try:
                disk = round(psutil.disk_usage(disk_path).percent, 1)
            except Exception:
                disk = 0.0

            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            sample_time = time.monotonic()
            elapsed = (sample_time - self._last_sample_monotonic
                       if self._last_sample_monotonic is not None else 0.0)
            self._last_sample_monotonic = sample_time

            if cpu >= 90.0:
                if self._cpu_high_since is None:
                    self._cpu_high_since = sample_time
            else:
                self._cpu_high_since = None
            cpu_high_duration_sec = (round(sample_time - self._cpu_high_since, 1)
                                     if self._cpu_high_since is not None else 0.0)

            # Counters เป็นยอดสะสมตั้งแต่เปิดเครื่อง จึงแปลงเป็นอัตราต่อวินาที
            # จากผลต่างระหว่างตัวอย่าง และตั้งเป็น 0 ในตัวอย่างแรก
            disk_io = psutil.disk_io_counters()
            disk_read_mb_s = disk_write_mb_s = 0.0
            if disk_io is not None:
                current_disk_io = (disk_io.read_bytes, disk_io.write_bytes)
                if self._last_disk_io is not None and elapsed > 0:
                    disk_read_mb_s = max(0.0, (current_disk_io[0] - self._last_disk_io[0]) / elapsed / 1048576)
                    disk_write_mb_s = max(0.0, (current_disk_io[1] - self._last_disk_io[1]) / elapsed / 1048576)
                self._last_disk_io = current_disk_io

            net_io = psutil.net_io_counters()
            net_recv_mb_s = net_sent_mb_s = 0.0
            if net_io is not None:
                current_net_io = (net_io.bytes_recv, net_io.bytes_sent)
                if self._last_net_io is not None and elapsed > 0:
                    net_recv_mb_s = max(0.0, (current_net_io[0] - self._last_net_io[0]) / elapsed / 1048576)
                    net_sent_mb_s = max(0.0, (current_net_io[1] - self._last_net_io[1]) / elapsed / 1048576)
                self._last_net_io = current_net_io

            try:
                load_1m, load_5m, load_15m = psutil.getloadavg()
            except (AttributeError, OSError):
                load_1m = load_5m = load_15m = None

            # Connection enumeration can be slow or restricted on Windows;
            # cache it at the history interval instead of querying on every UI tick.
            if sample_time - self._last_network_scan >= self.HISTORY_INTERVAL_SEC:
                try:
                    self._network_connection_count = len(psutil.net_connections(kind="inet"))
                except (psutil.AccessDenied, OSError, NotImplementedError):
                    self._network_connection_count = ""
                self._last_network_scan = sample_time

            # Disk partition enumeration and log directory walks are slower
            # than reading counters. Cache these low-frequency details so the
            # 2-second UI sampling loop stays lightweight.
            if sample_time - self._last_disk_details_scan >= self.DISK_DETAILS_INTERVAL_SEC:
                disk_by_mount = {}
                try:
                    for part in psutil.disk_partitions(all=False):
                        if "cdrom" in (part.opts or "").lower():
                            continue
                        try:
                            disk_by_mount[part.mountpoint] = round(psutil.disk_usage(part.mountpoint).percent, 1)
                        except (PermissionError, OSError):
                            continue
                except (PermissionError, OSError, RuntimeError):
                    pass
                self._disk_usage_by_mount = disk_by_mount
                self._last_disk_details_scan = sample_time

            if sample_time - self._last_log_size_scan >= self.LOG_SCAN_INTERVAL_SEC:
                log_size_bytes = 0
                try:
                    with os.scandir(os.path.dirname(self.db_file) or ".") as entries:
                        for entry in entries:
                            if entry.is_file() and entry.name.lower().endswith((".log", ".jsonl")):
                                try:
                                    log_size_bytes += entry.stat().st_size
                                except OSError:
                                    continue
                except (OSError, PermissionError):
                    pass
                self._log_size_bytes = log_size_bytes
                self._last_log_size_scan = sample_time

            return {
                "cpu": cpu, "ram": ram, "disk": disk,
                "ram_used_mb": round(vm.used / 1048576, 1),
                "ram_available_mb": round(vm.available / 1048576, 1),
                "swap_percent": round(swap.percent, 1),
                "swap_used_mb": round(swap.used / 1048576, 1),
                "disk_read_mb_s": round(disk_read_mb_s, 3),
                "disk_write_mb_s": round(disk_write_mb_s, 3),
                "network_recv_mb_s": round(net_recv_mb_s, 3),
                "network_sent_mb_s": round(net_sent_mb_s, 3),
                "process_count": len(psutil.pids()),
                "cpu_high_duration_sec": cpu_high_duration_sec,
                "network_connections": self._network_connection_count,
                "log_size_mb": round(self._log_size_bytes / 1048576, 2),
                "disk_usage_by_mount": json.dumps(self._disk_usage_by_mount, ensure_ascii=False),
                "uptime_sec": round(time.time() - psutil.boot_time(), 1),
                "load_1m": round(load_1m, 2) if load_1m is not None else "",
                "load_5m": round(load_5m, 2) if load_5m is not None else "",
                "load_15m": round(load_15m, 2) if load_15m is not None else "",
            }

        except Exception as e:
            print(f"[Collector] Error reading metrics: {e}")
            return {"cpu": 0.0, "ram": 0.0, "disk": 0.0}

    # ------------------------------------------------------------------
    # อ่านข้อมูลของ process เฉพาะ (Docker container / PID)
    # ------------------------------------------------------------------
    def get_process_metrics(self, pid: int) -> dict | None:
        try:
            proc = psutil.Process(pid)
            children = proc.children(recursive=True)
            all_procs = [proc] + children

            ram_mb = sum(p.memory_info().rss for p in all_procs if p.is_running()) / 1024 / 1024
            cpu = sum(p.cpu_percent(interval=0) for p in all_procs if p.is_running())

            return {
                "pid": pid,
                "name": proc.name(),
                "ram_mb": round(ram_mb, 1),
                "cpu": round(cpu, 1),
                "status": proc.status(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    # ------------------------------------------------------------------
    # บันทึกลง CSV (append)
    # ------------------------------------------------------------------
    def save_to_db(self, metrics: dict):
        now = time.monotonic()
        if now - self._last_save_monotonic < self.HISTORY_INTERVAL_SEC:
            return False
        try:
            file_exists = os.path.isfile(self.db_file) and os.path.getsize(self.db_file) > 0
            ts = metrics.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))

            with open(self.db_file, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["timestamp", "cpu", "ram", "disk"],
                    extrasaction="ignore",
                )
                if not file_exists:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": ts,
                    "cpu":  metrics.get("cpu",  0),
                    "ram":  metrics.get("ram",  0),
                    "disk": metrics.get("disk", 0),
                })
            # บันทึก metrics รายละเอียดแยกต่างหาก โดยไม่ต้องเปลี่ยน header เก่าของกราฟ
            extended_fields = [
                "timestamp", "cpu", "ram", "disk", "ram_used_mb", "ram_available_mb",
                "swap_percent", "swap_used_mb", "disk_read_mb_s", "disk_write_mb_s",
                "network_recv_mb_s", "network_sent_mb_s", "process_count",
                "load_1m", "load_5m", "load_15m", "cpu_high_duration_sec",
                "network_connections", "log_size_mb", "disk_usage_by_mount", "uptime_sec",
            ]
            ext_exists = os.path.isfile(self.extended_db_file) and os.path.getsize(self.extended_db_file) > 0
            with open(self.extended_db_file, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=extended_fields, extrasaction="ignore")
                if not ext_exists:
                    writer.writeheader()
                writer.writerow({"timestamp": ts, **metrics})
            self._last_save_monotonic = now
            return True
        except Exception as e:
            print(f"[Collector] Save error: {e}")
            return False

    # ------------------------------------------------------------------
    # ดึงประวัติ N แถวล่าสุดจาก CSV (สำหรับ graph)
    # ------------------------------------------------------------------
    def get_history(self, n: int = 60) -> list[dict]:
        if not os.path.isfile(self.db_file):
            return []
        try:
            import pandas as pd
            df = pd.read_csv(self.db_file)
            df.columns = [c.lower().strip() for c in df.columns]
            return df.tail(n).to_dict("records")
        except Exception:
            return []

    def get_extended_history(self, n: int = 1000) -> list[dict]:
        """ดึงประวัติ metrics แบบละเอียดล่าสุด (network, disk I/O, swap, process/load)."""
        if not os.path.isfile(self.extended_db_file):
            return []
        try:
            with open(self.extended_db_file, "r", newline="", encoding="utf-8") as stream:
                rows = deque(csv.DictReader(stream), maxlen=max(1, int(n)))
            return list(rows)
        except (OSError, ValueError, csv.Error):
            return []

    def save_server_health_sample(self, server: str, *, stype: str, online: bool,
                                  latency_ms=None, method: str = "unknown",
                                  health: str = "unknown", cpu_pct=None, ram_pct=None,
                                  timestamp: datetime = None) -> bool:
        """Persist one server probe so uptime, latency, and resource history are measurable."""
        fields = ["timestamp", "server", "stype", "online", "latency_ms", "method",
                  "health", "cpu_pct", "ram_pct"]
        now_epoch = time.time()
        if now_epoch - self._server_health_saved_at.get(server, 0.0) < self.SERVER_HEALTH_INTERVAL_SEC:
            return True
        row = {
            "timestamp": (timestamp or datetime.now()).isoformat(timespec="seconds"),
            "server": str(server), "stype": str(stype), "online": int(bool(online)),
            "latency_ms": latency_ms if latency_ms is not None else "",
            "method": method or "unknown", "health": health or "unknown",
            "cpu_pct": cpu_pct if cpu_pct is not None else "",
            "ram_pct": ram_pct if ram_pct is not None else "",
        }
        try:
            os.makedirs(os.path.dirname(self.server_health_file), exist_ok=True)
            with self._server_health_lock:
                exists = (os.path.isfile(self.server_health_file)
                          and os.path.getsize(self.server_health_file) > 0)
                if exists and os.path.getsize(self.server_health_file) > 64 * 1024 * 1024:
                    with open(self.server_health_file, "r", newline="", encoding="utf-8-sig") as source:
                        recent = list(csv.DictReader(source))[-350000:]
                    with open(self.server_health_file, "w", newline="", encoding="utf-8") as target:
                        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(recent)
                with open(self.server_health_file, "a", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                    if not exists:
                        writer.writeheader()
                    writer.writerow(row)
                self._server_health_saved_at[server] = now_epoch
            return True
        except OSError as exc:
            print(f"[Server history] save failed for {server}: {exc}")
            return False

    def get_server_health_history(self, n: int = 350000) -> list[dict]:
        if not os.path.isfile(self.server_health_file):
            return []
        try:
            with open(self.server_health_file, "r", newline="", encoding="utf-8-sig") as stream:
                rows = deque(csv.DictReader(stream), maxlen=max(1, int(n)))
            result = []
            # `rows` is already bounded to the newest n records by deque(maxlen=...).
            # deque does not support slicing; iterate directly to keep history loading safe.
            for row in rows:
                try:
                    row["timestamp"] = datetime.fromisoformat(row["timestamp"])
                    row["online"] = bool(int(row.get("online", 0)))
                except (KeyError, TypeError, ValueError):
                    continue
                for key in ("latency_ms", "cpu_pct", "ram_pct"):
                    try:
                        row[key] = float(row[key]) if row.get(key) not in (None, "") else None
                    except (TypeError, ValueError):
                        row[key] = None
                result.append(row)
            return result
        except (OSError, ValueError, csv.Error):
            return []

    def get_training_history(self, days: int = 30, max_rows: int = 3000) -> list[dict]:
        """Read a bounded, evenly sampled training set from the last N days.

        Supports both the extended history schema and existing legacy CPU/RAM/Disk
        CSVs, so previous project data remains useful without rewriting it.
        """
        source = self.extended_db_file if os.path.isfile(self.extended_db_file) else self.db_file
        if not os.path.isfile(source) or max_rows < 1:
            return []
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
        rng = random.Random(1701)
        reservoir = []
        eligible = 0
        try:
            with open(source, "r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    try:
                        timestamp = datetime.strptime(row.get("timestamp", "")[:19], "%Y-%m-%d %H:%M:%S")
                    except (TypeError, ValueError):
                        continue
                    if timestamp < cutoff:
                        continue
                    try:
                        cleaned = {key: float(row[key]) for key in
                                   ("cpu", "ram", "disk") if row.get(key) not in (None, "")}
                        if len(cleaned) != 3:
                            continue
                    except (TypeError, ValueError, KeyError):
                        continue
                    cleaned.update({key: row.get(key, "") for key in
                                    ("disk_read_mb_s", "disk_write_mb_s", "network_recv_mb_s",
                                     "network_sent_mb_s", "process_count", "swap_percent")})
                    cleaned["timestamp"] = timestamp
                    eligible += 1
                    if len(reservoir) < max_rows:
                        reservoir.append(cleaned)
                    else:
                        index = rng.randrange(eligible)
                        if index < max_rows:
                            reservoir[index] = cleaned
            reservoir.sort(key=lambda item: item["timestamp"])
            return reservoir
        except (OSError, csv.Error):
            return []

    def get_lstm_training_history(self, days: int = 30, max_rows: int = 12000) -> list[dict]:
        """Return evenly spaced, chronological metrics for sequence-model training.

        Unlike the reservoir sample used by Isolation Forest, LSTM inputs must
        preserve time order and nearby observations. Two CSV passes keep memory
        bounded while covering the requested history window.
        """
        source = self.extended_db_file if os.path.isfile(self.extended_db_file) else self.db_file
        if not os.path.isfile(source) or max_rows < 36:
            return []
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))

        def parse_row(row):
            try:
                timestamp = datetime.strptime(row.get("timestamp", "")[:19], "%Y-%m-%d %H:%M:%S")
                values = {key: float(row[key]) for key in ("cpu", "ram", "disk")}
                if timestamp < cutoff or not all(0.0 <= value <= 100.0 for value in values.values()):
                    return None
                return {"timestamp": timestamp, **values}
            except (TypeError, ValueError, KeyError):
                return None

        try:
            eligible = 0
            with open(source, "r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    if parse_row(row) is not None:
                        eligible += 1
            if eligible < 1:
                return []
            stride = max(1, (eligible + max_rows - 1) // max_rows)
            rows, seen = [], 0
            with open(source, "r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    cleaned = parse_row(row)
                    if cleaned is None:
                        continue
                    if seen % stride == 0:
                        rows.append(cleaned)
                    seen += 1
            return rows[-max_rows:]
        except (OSError, csv.Error):
            return []
