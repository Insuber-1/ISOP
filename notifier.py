"""
notifier.py — แจ้งเตือนเมื่อ AI ตรวจพบ Anomaly
เหลือเฉพาะ Popup (tkinter messagebox) — เวอร์ชันนี้ตัดการส่ง Gmail ออกแล้ว
(เปลี่ยนไปแสดงผลผ่านเว็บ dashboard แทน ดู server.py)
"""
import json
import os
import time
import tkinter.messagebox as msgbox

from paths import app_path, atomic_write_json


DEFAULT_CONFIG = {
    "popup_enabled": True,
    "cooldown_seconds": 60,
}


class Notifier:
    # เดิม default เป็น "notifier_config.json" แบบ relative — ขึ้นกับ current working directory
    # ตอนรัน เปลี่ยนเป็น absolute path ผ่าน app_path() กันไว้เฉยๆ (main.py ส่ง path มาเองอยู่แล้ว)
    def __init__(self, config_file: str = None, on_alert=None):
        self.config_file = config_file or app_path("notifier_config.json")
        self.config = self._load_config()
        self._last_sent: float = 0.0  # timestamp ของการแจ้งเตือนครั้งล่าสุด (ป้องกันสแปม)
        # on_alert(metrics: dict, risk_score: float) — ถ้ามีการส่งเข้ามา (main.py ส่งมาเสมอ)
        # จะเรียกตัวนี้แทน tkinter messagebox ธรรมดา เพื่อให้ main.py แสดงหน้าต่างแจ้งเตือน
        # แบบ custom ที่ดูเร่งด่วน/ลอยอยู่บนสุดจริงๆ ได้ (หน้าตาเหมือนป๊อปอัพไวรัสธรรมดาไม่พอ
        # กับความสำคัญของการแจ้งเตือนนี้) — ถ้าไม่ส่งมา (None) จะ fallback ไปใช้ popup เดิม
        self.on_alert = on_alert

    # ------------------------------------------------------------------
    # โหลด / สร้าง config
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    merged = dict(DEFAULT_CONFIG)
                    merged.update(data)
                    return merged
            except Exception as e:
                print(f"[Notifier] Config read error: {e}")
        self._save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    def _save_config(self, config: dict):
        try:
            atomic_write_json(self.config_file, config)
        except Exception as e:
            print(f"[Notifier] Config save error: {e}")

    def update_config(self, **kwargs):
        self.config.update(kwargs)
        self._save_config(self.config)

    # ------------------------------------------------------------------
    # จุดเข้าหลัก — เรียกตอนตรวจพบ anomaly
    # parent: หน้าต่าง tkinter (สำหรับ popup ต้องมี mainloop ทำงานอยู่)
    # ------------------------------------------------------------------
    def notify_anomaly(self, parent, metrics: dict, risk_score: float = 0.0):
        now = time.time()
        cooldown = self.config.get("cooldown_seconds", 60)
        if now - self._last_sent < cooldown:
            return  # ยังอยู่ในช่วง cooldown ไม่ส่งซ้ำ
        self._last_sent = now

        if not self.config.get("popup_enabled", True):
            return

        # ── ถ้า main.py ส่ง on_alert callback มา (ปกติจะส่งมาเสมอ) ให้ใช้ตัวนั้นแสดงหน้าต่าง
        #    แจ้งเตือนแบบ custom (topmost จริง + ดีไซน์ดูเร่งด่วน) แทน tkinter messagebox ธรรมดา ──
        if self.on_alert is not None:
            try:
                parent.after(0, lambda: self.on_alert(metrics, risk_score))
                return
            except Exception as e:
                print(f"[Notifier] on_alert callback error, ใช้ popup ธรรมดาแทน: {e}")
                # ไม่ return ตรงนี้ — ถ้า callback custom พังให้ตกไปใช้ popup เดิมด้านล่างแทน
                # ดีกว่าไม่แจ้งเตือนอะไรเลยเงียบๆ

        cpu = metrics.get("cpu", 0)
        ram = metrics.get("ram", 0)
        disk = metrics.get("disk", 0)

        title = "⚠️ Intelligent Server Operations Platform — Anomaly Detected"
        body = (
            f"ตรวจพบความผิดปกติของระบบ\n\n"
            f"CPU:  {cpu}%\n"
            f"RAM:  {ram}%\n"
            f"Disk: {disk}%\n"
            f"Risk Score: {risk_score:.0f}%\n\n"
            f"เวลา: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._show_popup(parent, title, body)

    # ------------------------------------------------------------------
    # Popup
    # ------------------------------------------------------------------
    def _show_popup(self, parent, title: str, body: str):
        try:
            # ใช้ after(0, ...) เพื่อให้แน่ใจว่ารันบน main thread ของ tkinter
            parent.after(0, lambda: msgbox.showwarning(title, body))
        except Exception as e:
            print(f"[Notifier] Popup error: {e}")
