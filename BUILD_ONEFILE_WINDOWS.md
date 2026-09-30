# One-File Windows Build

## วิธี Build

ดับเบิลคลิก `build_onefile.bat`

สคริปต์จะตรวจสอบ Python และ Tcl/Tk ก่อน จากนั้น Build และรัน runtime self-test กับ EXE จริง
หาก `.build-venv` เดิมเสียหรือชี้ไปยัง Python path ที่ไม่มีแล้ว สคริปต์จะสร้าง environment นี้ใหม่
โดยใช้ Python Launcher (`py -3`) หรือ Python ใน `PATH` และไม่ลบไฟล์ใน `build/` หรือ `dist/`

Self-test ตรวจ:
- Tcl/Tk สามารถสร้างหน้าต่างได้
- resource ภายใน EXE ครบ
- theme ดำ/เทาโหลดได้ทั้งหมด
- `docker-compose.yml` และ `app/` ถูกฝังและคัดลอกไป runtime ได้
- Flask Login/Setup/Dashboard ทำงานแบบ first-run
- ข้อมูลสามารถเขียนใน `%LOCALAPPDATA%` ได้
- EXE แบบ one-file สามารถเปิดตัวเองซ้ำได้

ถ้าเกิด error สคริปต์จะหยุดและไม่ประกาศว่า Build สำเร็จ

## ผลลัพธ์

แจกเฉพาะ:

`dist\Intelligent-Server-Operations-Platform.exe`

ไม่ต้องแจก `templates/`, `app/`, `docker-compose.yml`, theme หรือ JSON/CSV ข้อมูลผู้ใช้

## Tcl/Tk

`main.spec` จะค้นหา Tcl/Tk จาก Python ที่ใช้ Build และฝังเข้า EXE โดยตรง พร้อม runtime hook ที่ตั้ง `TCL_LIBRARY` และ `TK_LIBRARY` ให้ตรงกับตำแหน่งภายใน one-file bundle
