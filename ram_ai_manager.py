"""
ram_ai_manager.py — ระบบปรับ RAM Limit ให้แต่ละ container โดยอัตโนมัติ ("AI RAM" ต่อ server)

แนวทางเดียวกับ brain.py (deterministic ไม่ต้องเทรน ไม่ใช่ ML):
1) อ่านค่า RAM ที่ container ใช้จริง (จาก docker stats ที่โปรแกรมหลัก poll อยู่แล้วทุก 3 วิ)
2) ทำ EMA (Exponential Moving Average) แยกทีละ container ลด noise/spike ชั่วขณะ
3) คำนวณ RAM limit ใหม่ = ค่าที่ใช้จริงแบบ smoothed x ส่วนเผื่อ (headroom) แล้วปัดขึ้นเป็นก้อน
4) Hysteresis — ต้องต่างจากค่าปัจจุบันเกิน CHANGE_THRESHOLD_PCT ก่อนถึงจะสั่ง docker update จริง
   กันไม่ให้ปรับถี่จนน่ารำคาญ/container กระตุก
5) เพดานสูงสุด = ค่า "RAM สูงสุด" ที่ตั้งไว้ในหน้า ⚙️ ตั้งค่า (ใช้ร่วมกันทุก container)

ทำงานเฉพาะ container ที่ (1) เป็น docker (2) online อยู่ (3) เปิดสวิตช์ 🤖 AI RAM ไว้เท่านั้น
— ถ้าปิดสวิตช์ (ค่าเริ่มต้น) = Manual ทั้งหมด ระบบนี้จะไม่ไปยุ่งกับ container นั้นเลย
"""
import math
import re
import threading
import time

_MEM_UNIT_MULT_MB = {
    "b": 1 / (1024 * 1024),
    "kib": 1 / 1024,
    "kb": 1 / 1024,
    "mib": 1,
    "mb": 1,
    "gib": 1024,
    "gb": 1024,
    "tib": 1024 * 1024,
    "tb": 1024 * 1024,
}
_MEM_RE = re.compile(r"([\d.]+)\s*([a-zA-Z]+)")


def parse_mem_to_mb(text: str):
    """แปลงข้อความหน่วยความจำสไตล์ docker stats (เช่น '31.09MiB', '1.95GiB') เป็น MB (float)
       คืน None ถ้า parse ไม่ได้ (เช่นค่าว่างหรือ container offline ที่โชว์ '—')"""
    if not text:
        return None
    m = _MEM_RE.match(text.strip())
    if not m:
        return None
    value, unit = m.groups()
    mult = _MEM_UNIT_MULT_MB.get(unit.lower())
    if mult is None:
        return None
    try:
        return float(value) * mult
    except ValueError:
        return None


class RamAIManager:
    HEADROOM              = 1.4    # จองพื้นที่เผื่อ 40% เหนือค่าที่ใช้จริง กัน container ชนเพดานพอดี
    MIN_RAM_MB             = 128    # ต่ำสุดที่จะตั้งให้ไม่ว่ากรณีใด
    ROUND_STEP_MB          = 64     # ปัดขึ้นเป็นก้อน 64MB กันเลขทศนิยมประหลาดๆ
    CHANGE_THRESHOLD_PCT   = 0.15   # ต้องต่างจากค่าปัจจุบันอย่างน้อย 15% ถึงจะสั่งปรับจริง (กัน flapping)
    EMA_ALPHA              = 0.4    # น้ำหนักค่าใหม่ใน EMA — มากกว่า brain.py เล็กน้อยเพราะ RAM ของ
                                    # แต่ละ container เปลี่ยนช้ากว่าค่า CPU/RAM รวมทั้งเครื่อง

    def __init__(self, get_candidates, is_enabled, get_usage_mb, get_ceiling_mb,
                 get_current_limit_mb, apply_limit, on_log=None, interval_sec: int = 20):
        """
        get_candidates()            -> list[str]   ชื่อ container docker ที่ track อยู่ทั้งหมด
        is_enabled(name)            -> bool         container นี้เปิด 🤖 AI RAM ไว้หรือยัง (และ online)
        get_usage_mb(name)          -> float|None   RAM ที่ใช้จริงตอนนี้ (MB) — None ถ้าอ่านไม่ได้ตอนนี้
        get_ceiling_mb()            -> float         เพดานสูงสุดรวม (จาก ⚙️ ตั้งค่า > RAM สูงสุด)
        get_current_limit_mb(name)  -> float         ram_limit_mb ปัจจุบันของ container นี้
        apply_limit(name, new_mb)   -> (ok, msg)     สั่งตั้งค่าจริง + บันทึกลง resource_limits.json
        on_log(text, level)         callback แจ้งผล — เรียกจาก background thread ฝั่งเรียกต้อง
                                     สลับกลับ UI thread เอง (เช่นผ่าน self.after)
        interval_sec                ระยะเวลาระหว่างรอบตรวจ/ปรับ (ค่าเริ่มต้น 20 วินาที)
        """
        self._get_candidates = get_candidates
        self._is_enabled = is_enabled
        self._get_usage_mb = get_usage_mb
        self._get_ceiling_mb = get_ceiling_mb
        self._get_current_limit_mb = get_current_limit_mb
        self._apply_limit = apply_limit
        self._on_log = on_log or (lambda text, level="info": None)
        self.interval_sec = interval_sec

        self._ema: dict = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._evaluation_lock = threading.Lock()

    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                return
            self._stop_event = threading.Event()
            self._running = True
            self._thread = threading.Thread(target=self._loop, args=(self._stop_event,), daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._running = False
            self._stop_event.set()

    def wait(self, timeout: float = 15.0) -> bool:
        """Wait briefly for any in-flight automatic Docker limit update to finish."""
        deadline = time.monotonic() + max(0.0, timeout)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        remaining = max(0.0, deadline - time.monotonic())
        acquired = self._evaluation_lock.acquire(timeout=remaining)
        if acquired:
            self._evaluation_lock.release()
        return acquired and (thread is None or not thread.is_alive())

    def check_now(self, name: str):
        """ประเมิน/ปรับ container ตัวเดียวทันที (เรียกตอนเพิ่งเปิดสวิตช์ ไม่ต้องรอรอบถัดไป)"""
        threading.Thread(target=self._safe_evaluate, args=(name,), daemon=True).start()

    # ------------------------------------------------------------------
    def _loop(self, stop_event):
        try:
            while not stop_event.wait(self.interval_sec):
                self._run_all()
        finally:
            with self._lock:
                if self._stop_event is stop_event:
                    self._running = False

    def _run_all(self):
        try:
            names = list(self._get_candidates() or [])
        except Exception as e:
            self._on_log(f"❌ AI RAM: อ่านรายการ container ไม่สำเร็จ — {e}", "danger")
            return
        for name in names:
            self._safe_evaluate(name)

    def _safe_evaluate(self, name: str):
        with self._evaluation_lock:
            try:
                self._evaluate(name)
            except Exception as e:
                self._on_log(f"❌ AI RAM: ประเมิน {name} ล้มเหลว — {e}", "danger")

    def _evaluate(self, name: str):
        if not self._is_enabled(name):
            self._ema.pop(name, None)   # ปิด/ยังไม่เปิด → ล้าง EMA เก่าทิ้ง กันเอาค่าเก่ามาปนตอนเปิดใหม่
            return

        usage_mb = self._get_usage_mb(name)
        if usage_mb is None or usage_mb <= 0:
            return  # อ่านค่าไม่ได้ตอนนี้ (เช่น container เพิ่ง restart) — ข้ามรอบนี้ไปก่อน

        prev = self._ema.get(name)
        smoothed = usage_mb if prev is None else (self.EMA_ALPHA * usage_mb + (1 - self.EMA_ALPHA) * prev)
        self._ema[name] = smoothed

        ceiling = max(self.MIN_RAM_MB, float(self._get_ceiling_mb() or 4096))
        target = max(self.MIN_RAM_MB, min(ceiling, smoothed * self.HEADROOM))
        target = math.ceil(target / self.ROUND_STEP_MB) * self.ROUND_STEP_MB
        if target > ceiling:
            target = math.floor(ceiling / self.ROUND_STEP_MB) * self.ROUND_STEP_MB or ceiling
        target = int(target)

        current = self._get_current_limit_mb(name) or 0
        if current > 0 and abs(target - current) / current < self.CHANGE_THRESHOLD_PCT:
            return  # ต่างกันไม่พอ (hysteresis) — ไม่ต้องปรับรอบนี้
        if target == current:
            return

        ok, msg = self._apply_limit(name, target)
        if ok:
            self._on_log(
                f"🤖 AI RAM: ปรับ {name} จาก {int(current)}MB → {target}MB "
                f"(ใช้จริง ~{usage_mb:.0f}MB, เผื่อไว้ {int((self.HEADROOM - 1) * 100)}%)",
                "success",
            )
        else:
            self._on_log(f"⚠️ AI RAM: ปรับ {name} เป็น {target}MB ไม่สำเร็จ — {msg}", "warning")
