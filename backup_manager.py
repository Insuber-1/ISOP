"""
backup_manager.py — ระบบสำรองข้อมูล (Backup) อัตโนมัติของ Intelligent Server Operations Platform

การทำงาน:
- เมื่อเปิดใช้งาน (ผู้ใช้เปิดสวิตช์ในหน้า ⚙️ ตั้งค่า + เลือกโฟลเดอร์ปลายทางแล้ว) จะทำการ
  backup ทันที 1 ครั้งตอนเซิร์ฟเวอร์เริ่มทำงาน จากนั้นทำซ้ำทุก 1 ชั่วโมง
- แต่ละรอบ: บีบอัดไฟล์/โฟลเดอร์ที่เกี่ยวข้องกับระบบทั้งหมด (docker-compose.yml ที่ import ไว้,
  ไฟล์ตั้งค่า resource limits/alert/threshold/บัญชีเว็บ/preferences ฯลฯ) เป็น .zip ไฟล์เดียว
  ไปไว้ที่โฟลเดอร์ปลายทางที่เลือกไว้ แล้วลบไฟล์ backup ชุดก่อนหน้าทิ้ง (เก็บไว้แค่ชุดล่าสุด)
- เขียนไฟล์ zip ใหม่ลง path ชั่วคราวก่อน แล้วค่อยลบของเก่า/เปลี่ยนชื่อไฟล์จริงทีหลัง
  (กันกรณีเครื่องดับ/โปรแกรมถูกปิดกลางคันตอนกำลังเขียนไฟล์ จะได้ไม่เสีย backup เก่าไปฟรีๆ
  โดยที่ backup ใหม่ยังไม่เสร็จ)
"""
import os
import glob
import time
import threading
import zipfile

BACKUP_PREFIX = "ai_server_backup_"


class BackupScheduler:
    """รัน backup ใน background thread ของตัวเอง ไม่บล็อก UI thread หลัก"""

    def __init__(self, get_sources, get_dest_dir, on_log=None, interval_sec: int = 3600):
        """
        get_sources   : callable ที่คืน list ของ path (ไฟล์/โฟลเดอร์) ที่จะเอาไป backup
                        (เรียกใหม่ทุกรอบ เพราะรายการไฟล์อาจเปลี่ยนได้ระหว่างที่โปรแกรมรันอยู่)
        get_dest_dir  : callable ที่คืน path โฟลเดอร์ปลายทาง (เรียกใหม่ทุกรอบเช่นกัน
                        เผื่อผู้ใช้เปลี่ยนที่เก็บ backup ระหว่างที่ระบบทำงานอยู่)
        on_log        : callback(text: str, level: str) สำหรับแจ้งผลลัพธ์กลับไปที่หน้า UI
                        (ต้องปลอดภัยเวลาถูกเรียกจาก thread อื่น — ฝั่งเรียกใช้เป็นคนจัดการเอง)
        interval_sec  : ระยะเวลาระหว่างรอบ backup (ค่าเริ่มต้น 1 ชั่วโมง)
        """
        self._get_sources = get_sources
        self._get_dest_dir = get_dest_dir
        self._on_log = on_log or (lambda text, level="info": None)
        self.interval_sec = interval_sec

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._backup_lock = threading.Lock()

    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._running

    def start(self):
        """เริ่มระบบ backup — backup ทันที 1 ครั้ง แล้วนับถอยหลังทำซ้ำทุก interval_sec"""
        with self._lock:
            if self._running:
                return
            self._stop_event = threading.Event()
            self._running = True
            self._thread = threading.Thread(target=self._loop, args=(self._stop_event,), daemon=True)
            self._thread.start()

    def stop(self):
        """ปิดระบบ backup (ไม่ลบ backup ที่มีอยู่แล้ว แค่หยุดทำรอบถัดไป)"""
        with self._lock:
            self._running = False
            self._stop_event.set()

    def wait(self):
        """Wait for the scheduler and any manual backup already in progress."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._backup_lock.acquire()
        self._backup_lock.release()

    def backup_now(self):
        """สั่ง backup ทันที 1 ครั้งแบบ manual (ไม่รอรอบถัดไป) — รันใน thread แยกกัน UI ค้าง"""
        threading.Thread(target=self._do_backup_once, daemon=True).start()

    # ------------------------------------------------------------------
    def _loop(self, stop_event):
        try:
            self._do_backup_once()
            # wait() คืนค่า True ถ้าถูกสั่ง stop ระหว่างรอ — ใช้แทน time.sleep เพื่อให้ stop() มีผลทันที
            while not stop_event.wait(self.interval_sec):
                self._do_backup_once()
        finally:
            with self._lock:
                if self._stop_event is stop_event:
                    self._running = False

    def _do_backup_once(self):
        # ป้องกัน manual backup ซ้อนกับรอบตั้งเวลา แล้วลบ/เขียนไฟล์ทับกัน
        if not self._backup_lock.acquire(blocking=False):
            self._safe_log("⏳ Backup กำลังทำงานอยู่ — ข้ามคำสั่งซ้ำ", "warning")
            return
        try:
            self._do_backup_once_locked()
        except Exception as e:
            self._safe_log(f"❌ Backup ล้มเหลว: {e}", "danger")
        finally:
            self._backup_lock.release()

    def _safe_log(self, text: str, level: str):
        try:
            self._on_log(text, level)
        except Exception as e:
            print(f"[Backup] Log callback error: {e}")

    def _do_backup_once_locked(self):
        dest_dir = (self._get_dest_dir() or "").strip()
        if not dest_dir:
            self._safe_log("⚠️ Backup: ยังไม่ได้เลือกตำแหน่งจัดเก็บ — ข้ามรอบนี้", "warning")
            return
        if not os.path.isdir(dest_dir):
            self._safe_log(f"❌ Backup: ไม่พบโฟลเดอร์ปลายทาง '{dest_dir}' (ไดรฟ์ถูกถอด/ยังไม่เชื่อมต่อ?)", "danger")
            return

        try:
            sources = [p for p in (self._get_sources() or []) if p and os.path.exists(p)]
        except Exception as e:
            self._safe_log(f"❌ Backup: อ่านรายการไฟล์ไม่สำเร็จ — {e}", "danger")
            return

        if not sources:
            self._safe_log("⚠️ Backup: ไม่พบไฟล์ข้อมูลของระบบให้สำรอง — ข้ามรอบนี้", "warning")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        unique_suffix = f"{os.getpid()}_{threading.get_ident()}"
        tmp_path = os.path.join(dest_dir, f".{BACKUP_PREFIX}tmp_{timestamp}_{unique_suffix}.zip")
        final_path = os.path.join(dest_dir, f"{BACKUP_PREFIX}{timestamp}_{unique_suffix}.zip")

        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for src in sources:
                    self._add_path(zf, src)
        except Exception as e:
            self._safe_log(f"❌ Backup ล้มเหลว: {e}", "danger")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return

        # เผยแพร่ backup ใหม่ให้สำเร็จก่อน จึงค่อยลบชุดเก่า เผื่อ rename ล้มเหลวจะยังมีชุดเดิม
        try:
            os.replace(tmp_path, final_path)
        except Exception as e:
            self._safe_log(f"❌ Backup: สร้างไฟล์สำเร็จแต่ย้ายไฟล์ไม่ได้ — {e}", "danger")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return

        removed = 0
        for old in glob.glob(os.path.join(dest_dir, f"{BACKUP_PREFIX}*.zip")):
            if os.path.abspath(old) == os.path.abspath(final_path):
                continue
            try:
                os.remove(old)
                removed += 1
            except OSError:
                pass  # ลบไม่ได้ก็เก็บชุดเก่าไว้ เพื่อไม่ให้เสีย backup ที่เพิ่งสำเร็จ

        size_mb = os.path.getsize(final_path) / (1024 * 1024)
        self._safe_log(
            f"💾 Backup สำเร็จ ({len(sources)} รายการ, {size_mb:.2f} MB) → {final_path}"
            + (f" (ลบชุดเก่า {removed} ไฟล์)" if removed else ""),
            "success",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _add_path(zf: zipfile.ZipFile, path: str):
        """เพิ่มไฟล์เดี่ยวหรือทั้งโฟลเดอร์เข้า zip โดยคงโครงสร้างชื่อไฟล์/โฟลเดอร์เดิมไว้"""
        if os.path.isdir(path):
            base = os.path.basename(os.path.normpath(path))
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.join(base, os.path.relpath(full, path))
                    try:
                        zf.write(full, rel)
                    except Exception:
                        pass  # ไฟล์เดี่ยวๆ อ่านไม่ได้ (เช่นกำลังถูกใช้งานอยู่) ข้ามไปแทนที่จะพังทั้งรอบ
        else:
            zf.write(path, os.path.basename(path))
