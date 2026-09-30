"""
paths.py — จุดเดียวสำหรับคำนวณ "โฟลเดอร์ฐาน" ของโปรแกรมทั้งระบบ

ทำไมต้องมีไฟล์นี้ (สรุปบั๊กที่เจอก่อนแก้):
  เดิมแต่ละไฟล์คำนวณโฟลเดอร์ฐานกันเอง 2 แบบ ซึ่งทั้งคู่พังตอนแพ็คเป็น .exe:

  1) แบบ __file__  (main.py, docker_ops.py เดิม)
     APP_DIR = os.path.dirname(os.path.abspath(__file__))
     ตอนรันเป็น .py ปกติ ใช้ได้ปกติ แต่ตอนแพ็คเป็น .exe แบบ onefile ด้วย
     PyInstaller, __file__ จะไม่ใช่ตำแหน่งของ main.exe อีกต่อไป แต่ชี้เข้าไปใน
     โฟลเดอร์ชั่วคราว (เช่น C:\\Users\\...\\AppData\\Local\\Temp\\_MEIxxxxxx) ที่
     PyInstaller แตกไฟล์ไปให้ตอนรัน — และโฟลเดอร์นี้ "สุ่มชื่อใหม่ทุกครั้ง" ที่เปิดโปรแกรม
     ผลคือ:
       - เปิดโปรแกรมไม่เจอไฟล์ตั้งค่าที่เคยเซฟไว้ (เพราะมันมองหาที่โฟลเดอร์ temp ใหม่)
         เข้าใจว่ายังไม่มี เลยสร้างไฟล์ default ใหม่ทับ → "ไฟล์มั่ว" ทุกครั้งที่เปิด
       - docker-compose.yml ที่ bundle อยู่ใน EXE ถูกใช้จาก _MEIPASS ซึ่งเปลี่ยนชื่อทุกครั้ง
         ทำให้ Docker bind-mount ไปยัง path ชั่วคราว และไม่เหมาะกับ container ที่ต้องอยู่ข้าม restart

  2) แบบชื่อไฟล์ relative เฉยๆ (server.py, collector.py, notifier.py และ
     config json บางตัวใน main.py เช่น "app_settings.json")
     ไฟล์พวกนี้จะถูกอ่าน/เขียนที่ current working directory ตอนรัน ซึ่งไม่ตรงกับ
     ตำแหน่งของ main.exe เสมอไป (เช่น เปิดผ่าน shortcut ที่ตั้ง "Start in" ไว้คนละที่,
     ลาก .exe ไปเปิดจากที่อื่น, เปิดผ่าน Task Scheduler ฯลฯ) ทำให้ไฟล์กระจัดกระจาย
     คนละที่กันไปหมด และบางไฟล์ (resource_limits.json, tracked_containers.json)
     ที่ตั้งใจให้ main.py กับ docker_ops.py/server.py "ใช้ร่วมกัน" ก็อาจกลายเป็นคนละไฟล์
     ทำให้ข้อมูลไม่ sync กันระหว่างหน้าเดสก์ท็อปกับหน้าเว็บ dashboard

ทางแก้: ให้ทุกไฟล์ .py ในโปรเจกต์ import get_app_dir()/app_path() จากที่นี่ที่เดียว
แล้วต่อ path แบบเต็ม (absolute) เสมอ — ได้ตำแหน่งเดียวกันเป๊ะๆ ไม่ว่าจะ:
  - รันเป็น .py ตรงๆ ระหว่างพัฒนา
  - แพ็คเป็น .exe แบบ onefile แล้วเปิดจากที่ไหนก็ตาม (double-click, shortcut, ฯลฯ)

หมายเหตุ: resource ภายใน EXE ใช้ resource_path() ส่วนข้อมูลผู้ใช้/ไฟล์ runtime ที่เขียนได้
ใช้ app_path() ใน LocalAppData เสมอ จึงไม่ต้องวางไฟล์ใดๆ ข้าง .exe
"""
import os
import sys
import json
import tempfile


def get_app_dir() -> str:
    """คืนโฟลเดอร์ที่ตัวโปรแกรม/executable อยู่จริง"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR: str = get_app_dir()


def get_data_dir() -> str:
    """โฟลเดอร์ข้อมูลที่เขียนได้ถาวรของแอป

    เมื่อแจกแบบ PyInstaller --onefile จะไม่เขียนข้อมูลผู้ใช้ลงข้าง .exe
    เพราะโฟลเดอร์นั้นอาจเขียนไม่ได้ (เช่น Program Files) และทำให้ข้อมูล
    หาย/ปนกับไฟล์โปรแกรม จึงใช้ LocalAppData บน Windows แทน
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dir = os.path.join(base, "AI Server Management")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


DATA_DIR: str = get_data_dir()


def atomic_write_json(path: str, data, *, indent: int = 2, ensure_ascii: bool = False):
    """Replace a JSON file only after its complete contents have been written."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".json-write-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=indent, ensure_ascii=ensure_ascii)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def app_path(*parts: str) -> str:
    """ต่อ path สำหรับไฟล์ข้อมูล/การตั้งค่าที่ผู้ใช้แก้ไขหรือโปรแกรมเขียนได้"""
    return os.path.join(DATA_DIR, *parts)


def resource_path(*parts: str) -> str:
    """ต่อ path สำหรับ resource ที่ bundle อยู่ภายใน executable

    PyInstaller --onefile จะแตก resource เหล่านี้ลงโฟลเดอร์ temp ของ process
    อัตโนมัติขณะโปรแกรมทำงาน และลบออกเมื่อ process จบ
    """
    base = getattr(sys, "_MEIPASS", None) or get_app_dir()
    return os.path.join(base, *parts)
