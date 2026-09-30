"""
proc_utils.py — เรียก subprocess (docker, docker-compose, ping ฯลฯ) แบบไม่ให้มีหน้าต่าง
CMD ผุดขึ้นมาบนหน้าจอ (Windows)

ปัญหาที่เจอ:
  ตัวโปรแกรม main.exe เอง build แบบ console=False (ไม่มีหน้าต่าง console ของตัวเอง) แต่ทุก
  ครั้งที่เรียก subprocess.run()/Popen() ไปยังโปรแกรม console จริง (docker.exe,
  docker-compose.exe, ping.exe) บน Windows โดยไม่บอกให้ซ่อนหน้าต่างไว้ Windows จะผุดหน้าต่าง
  CMD ดำๆ ขึ้นมาสั้นๆ ทุกครั้งที่เรียก — โปรแกรมนี้เรียก subprocess ถี่มาก (poll docker stats
  ทุก 3 วิ, เช็คสถานะ container เป็นระยะ ฯลฯ) เลยเห็นเป็นจอกระพริบรัวๆ

ทางแก้: ครอบ subprocess.run()/Popen() ด้วยฟังก์ชันในไฟล์นี้แทนการเรียกตรงๆ ทุกที่ในโปรเจกต์
— จะใส่ทั้ง creationflags=CREATE_NO_WINDOW และ STARTUPINFO(SW_HIDE) ให้อัตโนมัติเฉพาะตอนรันบน
Windows เท่านั้น (Linux/macOS ไม่มีแนวคิดหน้าต่าง console แบบนี้ จึงไม่ต้องทำอะไรเพิ่ม)
"""
import platform
import subprocess

_IS_WINDOWS = platform.system() == "Windows"


def _hidden_window_kwargs() -> dict:
    """คืน kwargs พิเศษสำหรับซ่อนหน้าต่าง console — ใช้เฉพาะบน Windows เท่านั้น
       ใส่ทั้งสองแบบ (creationflags + startupinfo) เพราะบางเวอร์ชัน Windows/บางโปรแกรม
       ที่ถูกเรียก ตอบสนองกับวิธีใดวิธีหนึ่งดีกว่าอีกวิธี ใส่คู่กันไว้กันเหนียวที่สุด"""
    if not _IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _apply_text_encoding_defaults(kwargs: dict) -> dict:
    """ถ้าผู้เรียกขอ text mode (text=True หรือ universal_newlines=True) แต่ไม่ได้ระบุ encoding เอง
       บังคับ decode เป็น utf-8 เสมอ (errors="replace" กัน exception ถ้าเจอ byte แปลกๆ) แทนที่จะ
       ปล่อยให้ python เดา encoding จาก locale ของเครื่อง (ปัญหาจริงที่เจอ: Windows ภาษาไทยมักตั้ง
       ค่าเริ่มต้นเป็น cp874 ซึ่งถอดรหัส byte บางตัวจาก docker logs ไม่ได้ เช่นสัญลักษณ์ unicode
       ที่ cloudflared ใช้พิมพ์ banner ตอนเชื่อมต่อ tunnel สำเร็จ — พอถอดรหัสไม่ได้ subprocess จะ
       โยน UnicodeDecodeError ขึ้นมา ถ้าไม่ได้ดักไว้ thread ที่กำลังรอผลอยู่จะตายเงียบๆ ทันที โดยไม่มี
       error โผล่ให้เห็นเลย เพราะ .exe ไม่มี console ให้ print stderr ออกมา (อาการ: กด 'เปิด
       docker-compose ใหม่' แล้วค้างที่ 'กำลังรอ tunnel เชื่อมต่อ...' ตลอดไป ไม่มี error ไม่มีลิงก์)"""
    wants_text = kwargs.get("text") or kwargs.get("universal_newlines")
    if wants_text and "encoding" not in kwargs:
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return kwargs


def run_hidden(*args, **kwargs):
    """เหมือน subprocess.run() ทุกอย่าง แต่ไม่ผุดหน้าต่าง CMD บน Windows และไม่พังเพราะปัญหา
       encoding ของ locale เครื่อง (ดู _apply_text_encoding_defaults ด้านบน)
       ถ้าผู้เรียกส่ง startupinfo/creationflags/encoding มาเองอยู่แล้ว จะไม่ไปทับของเดิม"""
    hidden = _hidden_window_kwargs()
    for key, value in hidden.items():
        kwargs.setdefault(key, value)
    kwargs = _apply_text_encoding_defaults(kwargs)
    return subprocess.run(*args, **kwargs)


def popen_hidden(*args, **kwargs):
    """เหมือน subprocess.Popen() ทุกอย่าง แต่ไม่ผุดหน้าต่าง CMD บน Windows และไม่พังเพราะปัญหา
       encoding ของ locale เครื่อง (ดู _apply_text_encoding_defaults ด้านบน)
       ถ้าผู้เรียกส่ง startupinfo/creationflags/encoding มาเองอยู่แล้ว จะไม่ไปทับของเดิม"""
    hidden = _hidden_window_kwargs()
    for key, value in hidden.items():
        kwargs.setdefault(key, value)
    kwargs = _apply_text_encoding_defaults(kwargs)
    return subprocess.Popen(*args, **kwargs)
