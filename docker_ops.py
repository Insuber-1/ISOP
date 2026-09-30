"""
docker_ops.py — ฟังก์ชันคุม Docker container แบบ standalone (ไม่พึ่ง Tkinter)
ใช้โดย server.py (Flask) เพื่อให้หน้าเว็บคุมเซิร์ฟเวอร์ได้เหมือนโปรแกรมเดสก์ท็อป
โดยไม่ต้องแตะ widget ของ main.py ข้าม thread เลย (ปลอดภัยกว่า)

ทุกฟังก์ชันเป็น blocking call ธรรมดา — ฝั่ง Flask route เป็นคนละ thread จาก Tkinter
mainloop อยู่แล้ว จึงเรียกตรงๆ ได้โดยไม่ทำให้หน้าต่างโปรแกรมหลักค้าง
"""
import json
import os
import platform
import re
import subprocess
import time

from paths import app_path, atomic_write_json
# ครอบ subprocess.run()/Popen() ทุกจุดด้วย proc_utils แทนเรียกตรงๆ กันหน้าต่าง CMD ผุดขึ้นมา
# บน Windows ทุกครั้งที่เรียก docker/docker-compose (ดูเหตุผลเต็มๆ ใน proc_utils.py)
import proc_utils

# เดิมคำนวณจาก __file__ ของไฟล์นี้เอง ซึ่งพังตอนแพ็คเป็น .exe แบบ onefile (__file__ จะชี้เข้าไปใน
# โฟลเดอร์ temp ที่สุ่มใหม่ทุกครั้งที่เปิดโปรแกรม) — ใช้ app_path() จาก paths.py แทน จะได้ path เดียวกัน
# เป๊ะกับ resource_limits.json ที่ main.py ใช้ (ไฟล์นี้ต้อง "ใช้ร่วมกัน" ระหว่างสองฝั่งจริงๆ)
RESOURCE_LIMITS_FILE = app_path("resource_limits.json")
DEFAULT_LIMITS = {"cpu_limit": 100, "ram_limit_mb": 2048}


# ══════════════════════════════════════════════════════════
# รายชื่อ container
# ══════════════════════════════════════════════════════════
def list_containers() -> list[dict]:
    """คืนรายชื่อ container ทั้งหมด (รวมที่ปิดอยู่) พร้อมสถานะ — อ่านจาก docker ps -a ตรงๆ
       ไม่ต้องพึ่งค่าใน memory ของ main.py เลย เพื่อความปลอดภัยข้าม thread"""
    try:
        r = proc_utils.run_hidden(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.State}}"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 4:
                continue
            name, image, status, state = parts[0], parts[1], parts[2], parts[3]
            status_lower = status.lower()
            health = ("unhealthy" if "unhealthy" in status_lower else
                      "starting" if "health: starting" in status_lower else
                      "healthy" if "healthy" in status_lower else "unknown")
            out.append({
                "name": name,
                "image": image,
                "status": status,
                "health": health,
                "running": state.lower() == "running",
            })
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []



# ══════════════════════════════════════════════════════════
# Start / Stop / Restart
# ══════════════════════════════════════════════════════════
def docker_action(name: str, action: str) -> tuple[bool, str]:
    if action not in ("start", "stop", "restart"):
        return False, f"ไม่รู้จัก action: {action}"
    try:
        r = proc_utils.run_hidden(["docker", action, name], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, f"{action} '{name}' สำเร็จ"
        return False, (r.stderr or r.stdout or "ไม่ทราบสาเหตุ").strip()
    except FileNotFoundError:
        return False, "ไม่พบคำสั่ง docker บนเครื่องนี้"
    except subprocess.TimeoutExpired:
        return False, f"{action} '{name}' timeout"
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════
# รันคำสั่งข้างใน container (docker exec) — มี timeout กันค้าง
# ══════════════════════════════════════════════════════════
def docker_exec_cmd(name: str, cmd: str, timeout_sec: int = 30) -> tuple[bool, str]:
    if not cmd or not cmd.strip():
        return False, "ไม่ได้พิมพ์คำสั่ง"
    try:
        proc = proc_utils.popen_hidden(
            ["docker", "exec", name, "sh", "-c", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False
        )
        try:
            out, _ = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, f"[Timeout] คำสั่งทำงานนานเกิน {timeout_sec}s ถูกยกเลิกอัตโนมัติ"
        try:
            decoded = out.decode("utf-8")
        except UnicodeDecodeError:
            decoded = out.decode("cp874", errors="replace")
        return True, decoded or "[No output]"
    except FileNotFoundError:
        return False, "ไม่พบคำสั่ง docker บนเครื่องนี้"
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════
# Ping (ICMP ไปที่ IP ของ container ก่อน แล้ว fallback เป็น docker exec ถ้าใช้ไม่ได้)
# ══════════════════════════════════════════════════════════
def _get_container_ip(name: str):
    try:
        r = proc_utils.run_hidden(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
            capture_output=True, text=True, timeout=5
        )
        lines = r.stdout.strip().splitlines()
        ip = lines[0].strip() if lines else ""
        return ip or None
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError):
        return None


def _ping_host(ip: str):
    param = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        r = proc_utils.run_hidden(
            ["ping", param, "1", "-w", "1000", ip],
            capture_output=True, text=True, timeout=4
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out, re.IGNORECASE)
            return True, (float(m.group(1)) if m else None)
        return False, None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, None


def docker_ping(name: str) -> tuple[bool, str]:
    r = proc_utils.run_hidden(["docker", "inspect", "-f", "{{.State.Running}}", name],
                        capture_output=True, text=True, timeout=5)
    if "true" not in r.stdout.lower():
        return False, "container ไม่ได้ทำงาน (offline)"

    ip = _get_container_ip(name)
    if ip:
        ok, latency = _ping_host(ip)
        if ok:
            latency_txt = f"{latency:.1f} ms" if latency is not None else "ตอบกลับ (ไม่ทราบ ms)"
            return True, f"ping สำเร็จ ({ip}): {latency_txt}"

    # ICMP ตรง IP ใช้ไม่ได้ (พบบ่อยบน Docker Desktop) → วัดผ่าน docker exec แทน
    try:
        start = time.time()
        r2 = proc_utils.run_hidden(["docker", "exec", name, "echo", "pong"],
                             capture_output=True, text=True, timeout=5)
        elapsed_ms = (time.time() - start) * 1000
        if r2.returncode == 0:
            return True, f"container ทำงานปกติ ตอบสนอง {elapsed_ms:.1f} ms (ผ่าน docker exec)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False, "container online แต่ตรวจสอบ response ไม่ได้"


# ══════════════════════════════════════════════════════════
# Resource limits (RAM/CPU) — ใช้ไฟล์ resource_limits.json ร่วมกับ main.py
# ══════════════════════════════════════════════════════════
def _load_limits() -> dict:
    if os.path.exists(RESOURCE_LIMITS_FILE):
        try:
            with open(RESOURCE_LIMITS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_limits(limits: dict):
    atomic_write_json(RESOURCE_LIMITS_FILE, limits)


def get_resource_limit(name: str) -> dict:
    limits = _load_limits()
    merged = dict(DEFAULT_LIMITS)
    merged.update(limits.get(name, {}))
    return merged


def get_actual_memory_mb(name: str):
    """อ่านค่า memory limit ที่ container ใช้งาน 'จริง' ตอนนี้ (ไม่ใช่แค่ค่าที่บันทึกไว้)
       คืน None ถ้าไม่จำกัด (unlimited)"""
    try:
        r = proc_utils.run_hidden(
            ["docker", "inspect", "-f", "{{.HostConfig.Memory}}", name],
            capture_output=True, text=True, timeout=5
        )
        raw = r.stdout.strip()
        val = int(raw) if raw.lstrip("-").isdigit() else 0
        if val <= 0:
            return None
        return round(val / (1024 * 1024))
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def _cpu_limit_args(cpu_pct: float) -> list[str]:
    """Build Docker update flags; 100% means remove the CPU quota."""
    if cpu_pct >= 100:
        return ["--cpu-quota=-1"]
    return [f"--cpus={cpu_pct / 100:.2f}"]


def set_resource_limit(name: str, cpu_pct: float, ram_mb: int) -> tuple[bool, str]:
    """ตั้ง RAM/CPU limit ทั้งบันทึกลง resource_limits.json (ให้ desktop app เห็นค่าตรงกัน)
       และสั่ง docker update ให้ container จริงทันที แล้ว 'ตรวจสอบผลจริง' ก่อนตอบกลับ
       (ไม่เชื่อแค่ returncode เพราะ docker update บางกรณี exit 0 แต่ค่าจริงไม่เปลี่ยนก็มี)"""
    limits = _load_limits()
    limits[name] = {"cpu_limit": cpu_pct, "ram_limit_mb": ram_mb}
    _save_limits(limits)

    try:
        r = proc_utils.run_hidden(
            ["docker", "update",
             *_cpu_limit_args(cpu_pct),
             f"--memory={ram_mb}m",
             f"--memory-swap={ram_mb}m",
             name],
            capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return False, "บันทึกค่าแล้ว แต่ไม่พบคำสั่ง docker บนเครื่องนี้"
    except subprocess.TimeoutExpired:
        return False, "บันทึกค่าแล้ว แต่ docker update timeout"

    if r.returncode != 0:
        err = (r.stderr or r.stdout or "ไม่ทราบสาเหตุ").strip()
        return False, f"บันทึกค่าแล้ว แต่ docker update ไม่สำเร็จ: {err}"

    actual_mb = get_actual_memory_mb(name)
    if actual_mb != ram_mb:
        shown = f"{actual_mb} MB" if actual_mb else "ไม่จำกัด (unlimited)"
        return False, (f"สั่งไปแล้วแต่ container ยังไม่ได้ใช้ {ram_mb} MB จริง (ตอนนี้เป็น {shown}) — "
                        "ลอง Stop แล้ว Start container ใหม่อีกครั้ง")
    return True, f"ตั้ง RAM {ram_mb} MB / CPU {cpu_pct}% ให้ '{name}' เรียบร้อย และตรวจสอบแล้วว่าใช้ค่านี้จริง"


# ══════════════════════════════════════════════════════════
# Public tunnel URL (cloudflared quick tunnel) — สำหรับเข้า dashboard จากนอกวงแลน
# ══════════════════════════════════════════════════════════
_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def diagnose_tunnel(container_name: str = "dashboard-tunnel") -> dict:
    """เช็คสถานะ container cloudflared แบบละเอียด เพื่อบอกได้ชัดว่าติดตรงไหน แทนที่จะรู้แค่
       'หาลิงก์ไม่เจอ' เฉยๆ — คืน dict {exists, running, url, log_tail, error}
       - exists=False   → ยังไม่มี container นี้เลย (docker-compose ยังไม่ได้สร้าง/สร้างไม่สำเร็จ)
       - running=False  → มี container แต่ไม่ได้ทำงานอยู่ (crash/exit/กำลัง pull image)
       - url=None ทั้งที่ running=True → cloudflared ยังเชื่อมต่อ Cloudflare edge ไม่เสร็จ (รอเพิ่ม)
       """
    result = {"exists": False, "running": False, "url": None, "log_tail": "", "error": None}
    # Compose no longer pins global container_name values. Resolve the tunnel by
    # its Compose service label first, then fall back to the legacy fixed name
    # for containers created by older releases.
    target = container_name
    try:
        lookup = proc_utils.run_hidden(
            ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=ai-server-management",
             "--filter", f"label=com.docker.compose.service={container_name}", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=5
        )
        if lookup.returncode == 0 and lookup.stdout.strip():
            target = lookup.stdout.strip().splitlines()[0].strip()
        r = proc_utils.run_hidden(
            ["docker", "inspect", "-f", "{{.State.Running}}", target],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            result["error"] = (r.stderr or r.stdout or "ไม่พบ container").strip()
            return result
        result["exists"] = True
        result["running"] = "true" in r.stdout.lower()
    except FileNotFoundError:
        result["error"] = "ไม่พบคำสั่ง docker บนเครื่องนี้"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "docker inspect timeout"
        return result

    try:
        r2 = proc_utils.run_hidden(
            ["docker", "logs", "--tail", "300", target],
            capture_output=True, text=True, timeout=8
        )
        text = (r2.stdout or "") + (r2.stderr or "")
        result["log_tail"] = text[-800:]  # เก็บท้าย log ไว้เผื่อ debug
        matches = _TUNNEL_URL_RE.findall(text)
        result["url"] = matches[-1] if matches else None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        result["error"] = str(e)

    return result


def get_tunnel_url(container_name: str = "dashboard-tunnel") -> str | None:
    """อ่าน docker logs ของ container cloudflared แล้วหา URL แบบ https://xxx.trycloudflare.com
       ล่าสุดที่ปรากฏ (cloudflared quick tunnel จะพิมพ์ URL นี้ออกมาตอนเชื่อมต่อสำเร็จ)
       คืน None ถ้ายังไม่เจอ (เช่น container ยังไม่พร้อม หรือยังเชื่อมต่อไม่เสร็จ)
       — ใช้ diagnose_tunnel() แทนถ้าอยากรู้ว่าติดขัดตรงไหนด้วย"""
    return diagnose_tunnel(container_name).get("url")


# ══════════════════════════════════════════════════════════
# Logs
# ══════════════════════════════════════════════════════════
def get_logs(name: str, lines: int = 150) -> str:
    try:
        r = proc_utils.run_hidden(
            ["docker", "logs", "--tail", str(lines), name],
            capture_output=True, text=True, timeout=8
        )
        text = (r.stdout or "") + (r.stderr or "")
        return text or "(ไม่มี log)"
    except FileNotFoundError:
        return "[Error] ไม่พบคำสั่ง docker บนเครื่องนี้"
    except subprocess.TimeoutExpired:
        return "[Error] อ่าน log timeout"
    except Exception as e:
        return f"[Error] {e}"
