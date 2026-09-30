# One-file EXE

โปรเจกต์นี้ถูกเตรียมสำหรับแจกเป็นไฟล์เดียว `Intelligent-Server-Operations-Platform.exe`

## สิ่งที่ฝังเข้า EXE
- Flask templates (`templates/`)
- `logo.png`
- UI themes สีดำและสีเทา
- `docker-compose.yml`
- `app/` ที่ Docker Compose ใช้งาน
- Python dependencies และ Tcl/Tk runtime

## สิ่งที่เกิดขึ้นอัตโนมัติตอนใช้งาน
- ข้อมูล Login/settings/metrics/config เก็บที่ `%LOCALAPPDATA%\AI Server Management`
- Docker runtime (`docker-compose.yml` และ `app/`) ถูกคัดลอกไปยัง `%LOCALAPPDATA%\AI Server Management\runtime` อัตโนมัติ เพื่อให้ Docker bind-mount ใช้ path ที่คงที่ แม้ EXE แบบ one-file จะสร้าง `_MEI...` ใหม่ทุกครั้ง
- ผู้ใช้ไม่ต้องเตรียมไฟล์ใดๆ ข้าง `.exe`

## Build
ให้ดับเบิลคลิก `build_onefile.bat`

สคริปต์จะทำ 2 ขั้นตอน:
1. Build ด้วย PyInstaller
2. เปิด EXE ที่เพิ่งสร้างด้วย `--self-test-relaunch` เพื่อตรวจ Tcl/Tk, resources, theme ทั้งหมด, YAML, first-run Login/Setup, การเขียนข้อมูล และการเปิด EXE ซ้ำแบบ one-file

ถ้า self-test ไม่ผ่าน สคริปต์จะถือว่า Build ไม่สำเร็จ

ผลลัพธ์ที่แจก:
`dist\Intelligent-Server-Operations-Platform.exe`

> หมายเหตุ: เครื่องปลายทางยังต้องมี Docker Desktop/Docker Engine หากต้องการฟังก์ชัน Docker และต้องมี network สำหรับดาวน์โหลด Docker images ครั้งแรก
