# Clean EXE Build

ชุดนี้ถูกเตรียมสำหรับ Build แจกโดยไม่ติดข้อมูลจากเครื่องผู้พัฒนา

## ไฟล์ที่ถูกนำออกก่อน Build

- `email_config.json` — ข้อมูล SMTP/App Password
- `app_settings.json` — การตั้งค่าจากเครื่องเดิม
- `app_preferences.json` — preference จากเครื่องเดิม
- `alert_thresholds.json` — ค่า alert เดิม
- `resource_limits.json` — ค่า RAM/CPU เดิม
- `tracked_containers.json` — รายชื่อ container เดิม
- `notifier_config.json` — config notification เดิม
- `metrics_db.csv` — ประวัติ metrics เดิม

## พฤติกรรมหลังติดตั้ง

เปิด URL เว็บแล้วเข้าสู่ Server Dashboard ได้ทันทีโดยไม่มีหน้า Login/Setup
ผู้ที่เข้าถึง URL จะควบคุมเซิร์ฟเวอร์และใช้ API `exec` ได้โดยตรง

ไฟล์ข้อมูลของแอปจะอยู่ในโฟลเดอร์ข้อมูลผู้ใช้ตามที่กำหนดใน `paths.py`

## Build

ดับเบิลคลิก `build_release.bat` หรือรัน:

```bat
build_release.bat
```

ไฟล์สำหรับแจกจะอยู่ในโฟลเดอร์ `release\`

## ไฟล์ที่ควรแจก

- `Intelligent-Server-Operations-Platform.exe` (ไฟล์เดียว; ทรัพยากรที่แอปต้องใช้ฝังใน EXE)

ไม่ควรนำไฟล์ข้อมูลจากเครื่องผู้พัฒนา เช่น `web_users.json` หรือ `email_config.json` ไปแจก
