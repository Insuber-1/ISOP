"""
remediator.py — ดำเนินการแก้ไขปัญหาจริงด้วย Docker API / psutil / subprocess
"""
import os
import subprocess
import platform
import time
import stat

# ครอบ subprocess.run() ทุกจุดด้วย proc_utils แทนเรียกตรงๆ กันหน้าต่าง CMD ผุดขึ้นมา
# บน Windows ทุกครั้งที่ remediate (ดูเหตุผลเต็มๆ ใน proc_utils.py)
import proc_utils


class Remediator:
    def __init__(self):
        self.action_log: list[str] = []   # บันทึกทุก action ที่ทำ

    # ------------------------------------------------------------------
    # จุดเข้าหลัก — เลือก action ตามชื่อปัญหา
    # ------------------------------------------------------------------
    def execute_action(self, issue_name: str) -> str:
        ts = time.strftime("%H:%M:%S")
        result = ""

        if issue_name == "High RAM Usage":
            result = self._clear_memory_cache()
        elif issue_name == "Anomalous CPU Behavior":
            result = self._kill_top_cpu_process()
        elif issue_name == "Disk Full":
            result = self._clean_temp_files()
        elif issue_name.startswith("docker_restart:"):
            container = issue_name.split(":", 1)[1]
            result = self._docker_restart(container)
        elif issue_name.startswith("docker_stop:"):
            container = issue_name.split(":", 1)[1]
            result = self._docker_stop(container)
        elif issue_name.startswith("docker_start:"):
            container = issue_name.split(":", 1)[1]
            result = self._docker_start(container)
        else:
            result = f"[Remediator] ไม่รู้จัก action: {issue_name}"

        log_line = f"[{ts}] {result}"
        self.action_log.append(log_line)
        print(log_line)
        return result

    # ------------------------------------------------------------------
    # ล้าง memory cache (Windows: empty working set / Linux: drop_caches)
    # ------------------------------------------------------------------
    def _clear_memory_cache(self) -> str:
        # OS page cache is reclaimable by the kernel. Forcing a flush/drop can
        # degrade service latency and does not reliably reduce process RAM.
        return "ℹ️ [Remediator] ระบบปฏิบัติการจัดการ memory cache ให้อัตโนมัติ — บันทึกเหตุการณ์และตรวจสอบ process แทน"

    # ------------------------------------------------------------------
    # Kill process ที่กิน CPU มากที่สุด (ยกเว้น system + ตัวเอง)
    # ------------------------------------------------------------------
    def _kill_top_cpu_process(self) -> str:
        return "ℹ️ [Remediator] พบ CPU สูง แต่ยังไม่ปิด process อัตโนมัติ เพราะอาจเป็นงานสำคัญ — ตรวจสอบรายชื่อ process ก่อนสั่งหยุด"

    # ------------------------------------------------------------------
    # ลบเฉพาะไฟล์ temp เก่ามาก (อายุ 30 วันขึ้นไป) จำกัดจำนวนและไม่ตาม symlink
    # ------------------------------------------------------------------
    def _clean_temp_files(self) -> str:
        try:
            temp_dir = os.path.abspath(os.environ.get("TEMP", "C:\\Windows\\Temp")
                                       if platform.system() == "Windows" else "/tmp")
            if not os.path.isdir(temp_dir) or os.path.islink(temp_dir):
                return "ℹ️ [Remediator] ไม่พบโฟลเดอร์ temp ที่ปลอดภัยสำหรับล้าง"
            cutoff = time.time() - 30 * 24 * 60 * 60
            freed = deleted = 0
            max_files = 2000
            for root, dirs, files in os.walk(temp_dir, topdown=True, followlinks=False):
                dirs[:] = [name for name in dirs
                           if not os.path.islink(os.path.join(root, name))]
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        info = os.lstat(path)
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                            continue
                        os.remove(path)
                        freed += info.st_size
                        deleted += 1
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    if deleted >= max_files:
                        break
                if deleted >= max_files:
                    break
            return (f"💾 [Remediator] ลบไฟล์ temp ที่เก่ากว่า 30 วัน {deleted:,} ไฟล์ "
                    f"คืนพื้นที่ {freed / (1024 * 1024):.1f} MB")
        except Exception as e:
            return f"💾 [Remediator] Clean temp error: {e}"

    # ------------------------------------------------------------------
    # Docker operations
    # ------------------------------------------------------------------
    def _docker_restart(self, container: str) -> str:
        return self._run_docker(["docker", "restart", container], f"🔄 Restart '{container}'")

    def _docker_stop(self, container: str) -> str:
        return self._run_docker(["docker", "stop", container], f"⏹ Stop '{container}'")

    def _docker_start(self, container: str) -> str:
        return self._run_docker(["docker", "start", container], f"▶ Start '{container}'")

    def _run_docker(self, cmd: list, label: str) -> str:
        try:
            r = proc_utils.run_hidden(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return f"✅ [Docker] {label} สำเร็จ"
            else:
                return f"❌ [Docker] {label} ล้มเหลว: {r.stderr.strip()}"
        except FileNotFoundError:
            return "❌ [Docker] ไม่พบ docker ในระบบ — ติดตั้ง Docker Desktop ก่อน"
        except subprocess.TimeoutExpired:
            return f"❌ [Docker] {label} timeout"
        except Exception as e:
            return f"❌ [Docker] {label} error: {e}"

    # ------------------------------------------------------------------
    # ส่ง log กลับให้ UI แสดง
    # ------------------------------------------------------------------
    def get_recent_log(self, n: int = 10) -> list[str]:
        return self.action_log[-n:]
