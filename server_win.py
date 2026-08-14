#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总控台 Windows 版后端 —— 复用原 static/ 前端，API 契约与原 macOS 版一致。
仅 Python 3 标准库。进程发现用 netstat + PowerShell (Get-Process/WMI) 替代 lsof/ps。
"""
import argparse, base64, ctypes, hashlib, json, logging, mimetypes, os, re, secrets, shutil
import socket, subprocess, sys, threading, time, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
VERSION_PATH = BASE_DIR / "VERSION"
VERSION = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "1.0.0"
SCHEMA_VERSION = 1

def _appdata_dir():
    root = os.environ.get("APPDATA") or str(BASE_DIR / "data")
    return Path(root) / "总控台"
def _logs_dir():
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(BASE_DIR / "logs")
    return Path(root) / "总控台" / "logs"

DATA_DIR = Path(os.environ.get("CONSOLE_DATA_DIR") or _appdata_dir())
LOGS_DIR = Path(os.environ.get("CONSOLE_LOG_DIR") or _logs_dir())
ICONS_DIR = DATA_DIR / "icons"
CONFIG_PATH = DATA_DIR / "config.json"
CONFIG_BAK = DATA_DIR / "config.json.bak"
for d in (DATA_DIR, LOGS_DIR, ICONS_DIR):
    d.mkdir(parents=True, exist_ok=True)

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 开发关键词（只匹配进程名，不匹配 args）
DEV_KEYWORDS = ["python", "python3", "node", "pnpm", "npm", "yarn", "bun", "deno", "go",
               "rust", "cargo", "ollama", "docker", "java", "php", "ruby", "gunicorn",
               "uvicorn", "vite", "astro", "hugo", "hexo", "converter"]

# 进程溯源：父进程名 -> (label, icon)
ORIGIN_MAP = {
    "code.exe": ("VS Code", "code"),
    "cursor.exe": ("Cursor", "code"),
    "windsurf.exe": ("Windsurf", "code"),
    "trae.exe": ("Trae", "code"),
    "claude.exe": ("Claude", "bot"),
    "codex.exe": ("Codex", "bot"),
    "opencode.exe": ("OpenCode", "bot"),
    "windowsterminal.exe": ("Terminal", "terminal"),
    "windows terminal.exe": ("Terminal", "terminal"),
    "wt.exe": ("Terminal", "terminal"),
    "powershell.exe": ("PowerShell", "terminal"),
    "pwsh.exe": ("PowerShell", "terminal"),
    "cmd.exe": ("命令提示符", "terminal"),
}

cfg_lock = threading.Lock()

def default_config():
    # 与 macOS 版 Config.DEFAULT 一致：默认无应用，用户通过界面自行添加。
    return {"schemaVersion": SCHEMA_VERSION, "apps": [], "hidden": [], "pinned": [],
            "promoted": [], "watchedKeywords": [], "uiTheme": "ops"}

def load_config():
    with cfg_lock:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.setdefault("schemaVersion", SCHEMA_VERSION)
            cfg.setdefault("apps", [])
            cfg.setdefault("hidden", [])
            cfg.setdefault("pinned", [])
            cfg.setdefault("promoted", [])
            cfg.setdefault("watchedKeywords", [])
            cfg.setdefault("uiTheme", "ops")
            for a in cfg["apps"]:
                a.setdefault("kind", "service")
            return cfg
        except Exception:
            if CONFIG_BAK.exists():
                try:
                    return json.loads(CONFIG_BAK.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return None

def save_config(cfg):
    with cfg_lock:
        if CONFIG_PATH.exists():
            shutil.copy2(CONFIG_PATH, CONFIG_BAK)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)

CONFIG = load_config()
if CONFIG is None:
    CONFIG = default_config()
    save_config(CONFIG)

RUN_STATE = {}
RUN_LOCK = threading.Lock()

# ============ 进程发现（Windows） ============

def list_listening():
    """netstat -ano -> {port: {pid, host}}"""
    result = {}
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=6,
                             creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", errors="replace")
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            addr = parts[1]
            try:
                port = int(addr.rsplit(":", 1)[-1])
                pid = int(parts[-1])
            except ValueError:
                continue
            host = "127.0.0.1" if addr.startswith("127.0.0.1") else "0.0.0.0"
            cur = result.get(port)
            if cur is None or host == "127.0.0.1":
                result[port] = {"pid": pid, "host": host}
    except Exception:
        pass
    return result

_ps_cache = {"ts": 0, "data": {}}
_ps_lock = threading.Lock()
def process_snapshot():
    global _ps_cache
    now = time.time()
    if now - _ps_cache["ts"] < 4:
        return _ps_cache["data"]
    if not _ps_lock.acquire(blocking=False):
        return _ps_cache["data"]
    try:
        if time.time() - _ps_cache["ts"] < 4:
            return _ps_cache["data"]
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$wmis=@{};"
            "Get-CimInstance Win32_Process | ForEach-Object { $wmis[[int]$_.ProcessId] = $_ };"
            "Get-Process | ForEach-Object { "
            "  $w = $wmis[[int]$_.Id]; "
            "  [PSCustomObject]@{ pid=[int]$_.Id; name=[string]$_.ProcessName; cpu=[double]$_.CPU; mem=[long]$_.WorkingSet64; start=$(if ($_.StartTime) { $_.StartTime.ToFileTimeUtc() } else { 0 }); exe=$(if ($_.Path) { $_.Path } else { '' }); ppid=$(if ($w) { [int]$w.ParentProcessId } else { 0 }); cmd=$(if ($w) { $w.CommandLine } else { '' }) } "
            "} | ConvertTo-Json -Compress;"
        )
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        txt = raw.stdout.decode("utf-8", errors="replace").strip()
        data = {}
        if txt:
            items = json.loads(txt)
            if isinstance(items, dict):
                items = [items]
            for it in items:
                try:
                    pid = int(it["pid"])
                    ft = it.get("start") or 0
                    start_ts = (int(ft) - 116444736000000000) / 1e7 if ft else 0
                    data[pid] = {
                        "name": it.get("name") or "",
                        "cpu_s": float(it.get("cpu") or 0),
                        "mem_bytes": int(it.get("mem") or 0),
                        "start_ts": start_ts,
                        "exe": it.get("exe") or "",
                        "ppid": int(it.get("ppid") or 0),
                        "cmd": it.get("cmd") or "",
                    }
                except Exception:
                    continue
        _ps_cache = {"ts": time.time(), "data": data}
        return data
    except Exception:
        return _ps_cache["data"]
    finally:
        _ps_lock.release()

_cpu_prev = {}  # pid -> (cpu_s, timestamp)

def cpu_pct(pid, cpu_s):
    global _cpu_prev
    now = time.time()
    prev = _cpu_prev.get(pid)
    pct = 0.0
    if prev is not None:
        dt = now - prev[1]
        if dt > 0:
            delta = max(0.0, cpu_s - prev[0])
            pct = delta / dt * 100.0
    _cpu_prev[pid] = (cpu_s, now)
    if len(_cpu_prev) > 300:
        _cpu_prev = {k: v for k, v in _cpu_prev.items() if now - v[1] < 60}
    return round(min(999.0, pct), 1)

def pid_alive(pid):
    if not pid:
        return False
    return pid in process_snapshot()

def kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False

def kill_by_port(port):
    info = list_listening().get(port)
    if info and info["pid"] and info["pid"] != os.getpid():
        return kill_tree(info["pid"])
    return False

def is_related(pid_a, pid_b, snap):
    """pid_a 是否为 pid_b 本身或其祖先/后代（沿 PPID 链）"""
    if not pid_a or not pid_b:
        return False
    if pid_a == pid_b:
        return True
    for start, target in ((pid_a, pid_b), (pid_b, pid_a)):
        cur = start
        for _ in range(12):
            info = snap.get(cur)
            if not info:
                break
            ppid = info.get("ppid")
            if ppid == target:
                return True
            if not ppid or ppid == cur:
                break
            cur = ppid
    return False

def origin_of(pid, snap, depth=0):
    if depth > 12 or not pid:
        return None
    info = snap.get(pid)
    if not info:
        return None
    name = (info.get("name") or "").lower()
    if name in ORIGIN_MAP:
        label, icon = ORIGIN_MAP[name]
        return {"label": label, "icon": icon}
    if name in ("cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "conhost.exe",
                "explorer.exe", "svchost.exe", "services.exe", "wininit.exe", "pnpm.cmd",
                "npm.cmd", "node.exe", "python.exe", "python3.exe"):
        return origin_of(info.get("ppid"), snap, depth + 1)
    if name:
        return {"label": name.replace(".exe", "").title(), "icon": "terminal"}
    return None

def classify_group(name, promoted):
    if promoted:
        return "mine"
    nl = (name or "").lower()
    for kw in DEV_KEYWORDS:
        if kw in nl:
            return "mine"
    return "background"

def uptime_sec(start_ts):
    if not start_ts:
        return 0
    return max(0, int(time.time() - start_ts))

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

_TOTAL_MEM = 0

def total_memory_bytes():
    global _TOTAL_MEM
    if _TOTAL_MEM:
        return _TOTAL_MEM
    try:
        import ctypes
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        _TOTAL_MEM = int(st.ullTotalPhys)
    except Exception:
        _TOTAL_MEM = 16 * 1024 * 1024 * 1024
    return _TOTAL_MEM

# ============ 应用管理 ============

def find_app(app_id):
    for a in CONFIG["apps"]:
        if a["id"] == app_id:
            return a
    return None

def app_log_path(app_id):
    return LOGS_DIR / (app_id + ".log")

def rotate_log(path):
    try:
        if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
            for i in range(2, 0, -1):
                old = path.with_name(path.name + "." + str(i))
                new = path.with_name(path.name + "." + str(i + 1))
                if old.exists():
                    if new.exists():
                        new.unlink()
                    old.rename(new)
            path.write_bytes(b"")
    except Exception:
        pass

def write_log(app_id, data):
    path = app_log_path(app_id)
    rotate_log(path)
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(data)

def tail_log(app_id, tail=300):
    path = app_log_path(app_id)
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        buf = b""
        pos = size
        lines = []
        while pos > 0 and len(lines) <= tail:
            read = min(4096, pos)
            pos -= read
            f.seek(pos)
            buf = f.read(read) + buf
            lines = buf.split(b"\n")
        if len(lines) > tail:
            lines = lines[-tail:]
        return b"\n".join(lines).decode("utf-8", errors="replace")

def inspect_app_health(app):
    issues = []
    cmd = (app.get("command") or "").strip()
    cwd = app.get("cwd")
    if not cmd:
        issues.append({"kind": "missing-command", "severity": "error", "title": "缺少启动命令",
                       "detail": "尚未填写启动命令", "fix": "在编辑面板填写命令"})
    if cwd and not os.path.isdir(cwd):
        issues.append({"kind": "missing-cwd", "severity": "error", "title": "工作目录不存在",
                       "detail": cwd, "fix": "选择正确的工作目录"})
    if cmd:
        first = cmd.split()[0]
        known = {"pnpm", "npm", "yarn", "node", "python", "python3", "cmd", "powershell",
                 "bash", "go", "cargo", "hugo", "hexo"}
        if first.lower() in known:
            pass
        elif os.path.sep in first or first.lower().endswith((".bat", ".exe", ".cmd")):
            p = Path(first)
            if not p.is_absolute():
                base = Path(cwd) if cwd else Path.home()
                p = base / first
            if not p.exists():
                issues.append({"kind": "missing-runtime", "severity": "error", "title": "启动入口不存在",
                               "detail": str(p), "fix": "检查命令路径"})
    blocking = any(i["severity"] == "error" for i in issues)
    status = "error" if blocking else ("unknown" if cmd else "ok")
    return {"status": status, "blocking": blocking, "issues": issues}

def app_status(app):
    port = app.get("port")
    listening = list_listening()
    snap = process_snapshot()
    pid = app.get("lastPid")
    running = False
    cur_pid = None
    legacy = False
    if pid and pid in snap:
        running = True
        cur_pid = pid
    elif port and port in listening:
        lp = listening[port]["pid"]
        if lp and lp != os.getpid():
            running = True
            cur_pid = lp
            legacy = True
    uptime = 0
    if cur_pid:
        info = snap.get(cur_pid)
        if info:
            uptime = uptime_sec(info.get("start_ts"))
    is_listening = False
    occupied = False
    occupied_pid = None
    if port and port in listening:
        lp = listening[port]["pid"]
        if cur_pid and (lp == cur_pid or is_related(lp, cur_pid, snap)):
            is_listening = True
        elif lp != os.getpid():
            occupied = True
            occupied_pid = lp
    return {"running": running, "pid": cur_pid, "uptimeSec": uptime,
            "listening": is_listening, "portOccupied": occupied, "portOccupiedPid": occupied_pid,
            "legacyManaged": legacy}

def start_app(app):
    health = inspect_app_health(app)
    if health["blocking"]:
        return {"ok": False, "error": "配置不完整", "health": health}
    st = app_status(app)
    if st["running"]:
        return {"ok": False, "error": "已在运行"}
    port = app.get("port")
    if port:
        info = list_listening().get(port)
        if info and info["pid"] and info["pid"] != os.getpid():
            snap = process_snapshot().get(info["pid"])
            nm = (snap or {}).get("name", "").lower()
            if any(k in nm for k in DEV_KEYWORDS):
                kill_tree(info["pid"])
                time.sleep(0.5)
    token = secrets.token_hex(8)
    env = os.environ.copy()
    env["CONSOLE_RUN_TOKEN"] = token
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    logpath = app_log_path(app["id"])
    rotate_log(logpath)
    try:
        f = open(logpath, "a", encoding="utf-8", errors="replace")
        p = subprocess.Popen(app["command"], cwd=app.get("cwd"), shell=True,
                             env=env, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                             stdout=f, stderr=subprocess.STDOUT)
    except Exception as e:
        return {"ok": False, "error": "启动失败: " + str(e)}
    app["lastPid"] = p.pid
    app["runToken"] = token
    app["lastPgid"] = p.pid
    if app.get("kind") == "task":
        app["lastExit"] = None
    save_config(CONFIG)
    with RUN_LOCK:
        RUN_STATE[app["id"]] = {"pid": p.pid, "token": token, "startedAt": time.time()}
    if app.get("kind") == "task":
        threading.Thread(target=_monitor_task, args=(app["id"], p), daemon=True).start()
    return {"ok": True, "pid": p.pid}

def _monitor_task(app_id, p):
    code = None
    try:
        code = p.wait()
    except Exception:
        code = None
    now = time.time()
    started = RUN_STATE.get(app_id, {}).get("startedAt") or now
    duration = round(now - started, 2)
    with RUN_LOCK:
        RUN_STATE.pop(app_id, None)
    app = find_app(app_id)
    if app:
        if code == 0:
            status = "succeeded"
        elif code == 130:
            status = "canceled"
        else:
            status = "failed"
        app["lastExit"] = {"status": status, "code": code, "at": int(now), "startedAt": int(started * 1000), "durationSec": duration}
        app["lastPid"] = None
        app["runToken"] = None
        save_config(CONFIG)
        write_log(app_id, "\n[总控台] 任务结束 status=" + status + " code=" + str(code) + " 耗时 " + str(duration) + "s\n")

def stop_app(app):
    st = app_status(app)
    pid = st.get("pid")
    if pid:
        kill_tree(pid)
    port = app.get("port")
    if port:
        info = list_listening().get(port)
        if info and info["pid"] and info["pid"] != os.getpid() and info["pid"] != pid:
            kill_tree(info["pid"])
    app["lastPid"] = None
    app["runToken"] = None
    app["lastPgid"] = None
    if app.get("kind") == "task":
        app["lastExit"] = {"status": "stopped", "code": None, "at": int(time.time()), "startedAt": None, "durationSec": None}
    save_config(CONFIG)
    with RUN_LOCK:
        RUN_STATE.pop(app["id"], None)
    return {"ok": True}

def restart_app(app):
    st = app_status(app)
    if not st["running"]:
        return start_app(app)
    health = inspect_app_health(app)
    if health["blocking"]:
        return {"ok": False, "error": "配置不完整", "health": health}
    stop_app(app)
    time.sleep(1)
    return start_app(app)
# ============ 状态 / 主题 / 项目识别 ============

def load_theme_manifest():
    themes = []
    tdir = STATIC_DIR / "themes"
    if tdir.exists():
        for jf in sorted(tdir.glob("*.json")):
            try:
                themes.append(json.loads(jf.read_text(encoding="utf-8")))
            except Exception:
                pass
    return themes

def enrich_app(app):
    out = dict(app)
    st = app_status(app)
    out.update(st)
    out["health"] = inspect_app_health(app)
    out["ports"] = [app["port"]] if app.get("port") else []
    out["openHost"] = "127.0.0.1"
    out["openHosts"] = {str(app["port"]): "127.0.0.1"} if app.get("port") else {}
    out["portConflict"] = False
    out["portConflictApps"] = []
    out["portOwner"] = None
    return out

def compute_state():
    listening = list_listening()
    snap = process_snapshot()
    apps = CONFIG["apps"]
    hidden = set(CONFIG.get("hidden", []))
    pinned = set(CONFIG.get("pinned", []))
    promoted = set(CONFIG.get("promoted", []))
    apps_by_port = {}
    for a in apps:
        if a.get("port"):
            apps_by_port[a["port"]] = a
    services = []
    seen = set()
    for port, info in listening.items():
        pid = info["pid"]
        if pid == os.getpid():
            continue
        if pid in seen:
            continue
        seen.add(pid)
        p = snap.get(pid)
        name = (p or {}).get("name", "?")
        key = f"{name}:{port}"
        instance_key = f"{pid}:{port}"
        app = apps_by_port.get(port)
        prom = key in promoted
        svc = {"key": key, "instanceKey": instance_key, "pid": pid, "name": name, "port": port,
               "cwd": app["cwd"] if app else None,
               "project": Path(app["cwd"]).name if app and app.get("cwd") else None,
               "cmd": (p or {}).get("cmd", "") if p else "",
               "cpu": cpu_pct(pid, (p or {}).get("cpu_s", 0)) if p else 0,
               "mem": round(((p or {}).get("mem_bytes", 0) / total_memory_bytes() * 100), 1) if p else 0,
               "uptimeSec": uptime_sec((p or {}).get("start_ts")) if p else 0,
               "group": "mine" if prom else classify_group(name, False),
               "pinned": key in pinned, "hidden": key in hidden, "promoted": prom,
               "appId": app["id"] if app else None, "appName": app["name"] if app else None,
               "origin": {"label": "总控台", "icon": "rocket"} if app else origin_of(pid, snap),
               "openHost": info["host"], "openHosts": {str(port): info["host"]}}
        services.append(svc)
    watched = []
    kws = CONFIG.get("watchedKeywords", [])
    if kws:
        for pid, p in snap.items():
            if pid == os.getpid():
                continue
            cmd = (p.get("cmd") or "").lower()
            if any(k.lower() in cmd for k in kws):
                kw = next(k for k in kws if k.lower() in cmd)
                watched.append({"pid": pid, "name": p.get("name", "?"), "cmd": p.get("cmd", ""),
                                "cpu": 0, "mem": round(p.get("mem_bytes", 0) / total_memory_bytes() * 100, 1),
                                "uptimeSec": uptime_sec(p.get("start_ts")), "keyword": kw})
    enriched_apps = [enrich_app(a) for a in apps]
    return {
        "services": services, "watched": watched, "apps": enriched_apps,
        "watchedKeywords": CONFIG.get("watchedKeywords", []),
        "consolePort": CONSOLE_PORT, "consolePid": os.getpid(), "consoleCwd": str(BASE_DIR),
        "version": VERSION, "schemaVersion": SCHEMA_VERSION,
        "degraded": False, "degradedReasons": [],
        "themes": load_theme_manifest(), "uiTheme": CONFIG.get("uiTheme", "ops"),
        "configHealth": {"writable": True, "recoveredFromBackup": False, "migratedFromSchema": None},
    }

FRAMEWORK_PORTS = {
    "astro": 4321, "vite": 5173, "next": 3000, "nuxt": 3000, "gatsby": 8000,
    "react-scripts": 3000, "webpack": 8080, "hugo": 1313, "hexo": 4000,
    "streamlit": 8501, "flask": 5000, "django": 8000, "uvicorn": 8000, "gunicorn": 8000,
}

def guess_port(script):
    m = re.search(r"(?:--port[= ]|-p[= ]|:\s*)(\d{2,5})", script)
    if m:
        return int(m.group(1))
    s = (script or "").lower()
    for fw, port in FRAMEWORK_PORTS.items():
        if fw in s:
            return port
    return None

def project_detect(cwd):
    cwd = os.path.abspath(cwd)
    name = os.path.basename(cwd)
    files = []
    candidates = []
    pkg = Path(cwd) / "package.json"
    if pkg.exists():
        files.append("package.json")
        try:
            d = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = d.get("scripts") or {}
            use_pnpm = (Path(cwd) / "pnpm-lock.yaml").exists() or "pnpm" in str(d.get("packageManager", ""))
            svc_keys = ("d", "s", "dev", "start", "serve", "preview", "p", "run")
            for k, v in scripts.items():
                kind = "service" if k in svc_keys else "task"
                label = ("pnpm " + k) if use_pnpm else ("npm run " + k)
                candidates.append({"command": label, "label": label, "source": "package.json",
                                   "port": guess_port(str(v)), "kind": kind, "detail": str(v)[:80]})
        except Exception:
            pass
    if (Path(cwd) / "_config.yml").exists():
        files.append("_config.yml")
        candidates.append({"command": "hexo s", "label": "hexo s", "source": "_config.yml", "port": 4000, "kind": "service", "detail": "Hexo 本地服务"})
    if (Path(cwd) / "config.toml").exists() or (Path(cwd) / "config.yaml").exists():
        files.append("config.toml")
        candidates.append({"command": "hugo server", "label": "hugo server", "source": "config.toml", "port": 1313, "kind": "service", "detail": "Hugo 本地服务"})
    for f, cmd, port in [("manage.py", "python manage.py runserver", 8000),
                         ("app.py", "python app.py", 5000), ("main.py", "python main.py", 8000)]:
        if (Path(cwd) / f).exists():
            files.append(f)
            candidates.append({"command": cmd, "label": cmd, "source": f, "port": port, "kind": "service", "detail": "Python 应用"})
    return {"ok": True, "cwd": cwd, "name": name, "files": files, "candidates": candidates}

def fetch_favicon(app):
    port = app.get("port")
    if not port:
        return {"ok": False, "error": "无端口"}
    base = "http://127.0.0.1:" + str(port)
    candidates = [base + "/favicon.ico"]
    try:
        html = urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8", errors="replace")
        for m in re.finditer(r'href="([^"]+)"', html):
            href = m.group(1)
            if "icon" in href.lower():
                if href.startswith("http"):
                    candidates.insert(0, href)
                elif href.startswith("/"):
                    candidates.insert(0, base + href)
                else:
                    candidates.insert(0, base + "/" + href)
    except Exception:
        pass
    for u in candidates:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=4).read()
            if len(data) < 20:
                continue
            ext = "ico"
            try:
                ct = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=4).headers.get("Content-Type", "")
                if "png" in ct: ext = "png"
                elif "webp" in ct: ext = "webp"
                elif "svg" in ct: ext = "svg"
                elif "jpg" in ct or "jpeg" in ct: ext = "jpg"
            except Exception:
                pass
            path = ICONS_DIR / ("fav-" + app["id"] + "." + ext)
            path.write_bytes(data)
            app["favicon"] = "/icons/fav-" + app["id"] + "." + ext
            save_config(CONFIG)
            return {"ok": True, "favicon": app["favicon"]}
        except Exception:
            continue
    return {"ok": False, "error": "未找到图标"}
# ============ HTTP ============

CONSOLE_PORT = None
HTTPD = None

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}

def _discard_body(handler):
    try:
        cl = int(handler.headers.get("Content-Length") or 0)
        if cl > 0:
            handler.rfile.read(cl)
    except Exception:
        pass

def _json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass

def _read_json(handler):
    try:
        cl = int(handler.headers.get("Content-Length") or 0)
        if cl <= 0:
            return {}
        raw = handler.rfile.read(cl)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}

def _safe_join(base, rel):
    target = (base / rel.lstrip("/")).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target

def _serve_file(handler, path):
    try:
        data = path.read_bytes()
    except Exception:
        handler.send_response(404)
        handler.end_headers()
        return
    ext = path.suffix.lower()
    ctype = STATIC_TYPES.get(ext, "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg", ".woff2", ".woff"):
        handler.send_header("Cache-Control", "public, max-age=3600")
    else:
        handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except Exception:
        pass

# ---- 动作实现 ----

def _create_app(body):
    name = (body.get("name") or "").strip()
    command = (body.get("command") or "").strip()
    if not name or not command:
        return {"ok": False, "error": "名称和命令必填"}
    kind = body.get("kind") or "service"
    port = body.get("port")
    if kind == "task":
        port = None
    app = {
        "id": secrets.token_hex(4), "name": name, "command": command,
        "cwd": body.get("cwd"), "port": port, "emoji": body.get("emoji"),
        "icon": None, "favicon": None, "kind": kind, "glyph": body.get("glyph"),
        "lastPid": None, "lastPgid": None, "runToken": None,
        "attached": False, "lastExit": None, "createdAt": int(time.time() * 1000),
    }
    attach_pid = body.get("attachPid")
    if attach_pid:
        app["attached"] = True
        app["lastPid"] = attach_pid
    CONFIG["apps"].append(app)
    save_config(CONFIG)
    return enrich_app(app)

def _update_app(app, body):
    stop_before = body.get("stopBeforeUpdate")
    st = app_status(app)
    changed_runtime = any(k in body for k in ("command", "cwd", "port", "kind"))
    if st["running"] and changed_runtime and not stop_before:
        return {"ok": False, "requiresStop": True, "error": "运行中修改需先停止"}
    if stop_before:
        stop_app(app)
    for k in ("name", "command", "cwd", "port", "emoji", "glyph", "kind"):
        if k in body:
            app[k] = body[k]
    if app.get("kind") == "task":
        app["port"] = None
    save_config(CONFIG)
    return enrich_app(app)

def _delete_app(app):
    stop_app(app)
    CONFIG["apps"] = [a for a in CONFIG["apps"] if a["id"] != app["id"]]
    save_config(CONFIG)
    try:
        app_log_path(app["id"]).unlink()
    except Exception:
        pass
    return {"ok": True}

def _attach_app(app, body):
    pid = body.get("pid")
    if app.get("kind") == "task":
        return {"ok": False, "error": "任务不支持认领"}
    port = app.get("port")
    if not port:
        return {"ok": False, "error": "无端口"}
    listening = list_listening()
    if port not in listening or listening[port]["pid"] != pid:
        return {"ok": False, "error": "该进程未监听配置端口"}
    app["attached"] = True
    app["lastPid"] = pid
    app["runToken"] = None
    save_config(CONFIG)
    return {"ok": True, "pid": pid, "cwd": app.get("cwd")}

def _diagnose_app(app):
    health = inspect_app_health(app)
    return {"ok": True, "issues": health["issues"], "summary": ("配置有效" if not health["blocking"] else "存在阻断问题")}

def _kill(body):
    pid = body.get("pid")
    if not pid:
        return {"ok": False, "error": "缺少 pid"}
    ok = kill_tree(pid)
    return {"ok": True} if ok else {"ok": False, "error": "结束进程失败"}

def _flag(body):
    key = body.get("key")
    flag = body.get("flag")
    value = bool(body.get("value"))
    if flag not in ("hidden", "pinned", "promoted"):
        return {"ok": False, "error": "未知标记"}
    lst = list(CONFIG.get(flag, []))
    if value:
        if key not in lst:
            lst.append(key)
    else:
        if key in lst:
            lst.remove(key)
    CONFIG[flag] = lst
    save_config(CONFIG)
    return {"ok": True}

def _watch(body):
    keyword = (body.get("keyword") or "").strip()
    action = body.get("action")
    if not keyword:
        return {"ok": False, "error": "缺少关键字"}
    kws = list(CONFIG.get("watchedKeywords", []))
    if action == "add":
        if keyword not in kws:
            kws.append(keyword)
    elif action == "remove":
        if keyword in kws:
            kws.remove(keyword)
    CONFIG["watchedKeywords"] = kws
    save_config(CONFIG)
    return {"ok": True, "keywords": kws}

def _reorder(body):
    ids = body.get("ids", [])
    apps = CONFIG["apps"]
    by_id = {a["id"]: a for a in apps}
    new_order = []
    for i in ids:
        if i in by_id:
            new_order.append(by_id[i])
    for a in apps:
        if a["id"] not in ids:
            new_order.append(a)
    CONFIG["apps"] = new_order
    save_config(CONFIG)
    return {"ok": True}

def _upload_icon(app, handler):
    cl = int(handler.headers.get("Content-Length") or 0)
    if cl <= 0 or cl > 5 * 1024 * 1024:
        return {"ok": False, "error": "无效图片"}
    data = handler.rfile.read(cl)
    ext = "png"
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    path = ICONS_DIR / (app["id"] + "." + ext)
    path.write_bytes(data)
    app["icon"] = "/icons/" + app["id"] + "." + ext
    save_config(CONFIG)
    return {"ok": True, "icon": app["icon"]}
def _do_restart_console():
    time.sleep(0.8)
    helper = (
        "import time, subprocess, sys\n"
        "time.sleep(1.5)\n"
        "subprocess.Popen([sys.executable, '-X', 'utf8', r'{server}', '--no-browser', '--port', '{port}'], cwd=r'{cwd}', creationflags=0x08000000|0x00000008|0x00000200)\n"
    ).format(server=str(BASE_DIR / "server_win.py"), port=str(CONSOLE_PORT), cwd=str(BASE_DIR))
    try:
        subprocess.Popen([sys.executable, "-c", helper], cwd=str(BASE_DIR),
                         creationflags=DETACHED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    except Exception:
        pass
    os._exit(0)

def _do_stop_console():
    time.sleep(0.5)
    os._exit(0)

class Handler(BaseHTTPRequestHandler):
    server_version = "Console/1.0"
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *args):
        pass
    def do_GET(self):
        self._route("GET")
    def do_POST(self):
        self._route("POST")
    def do_PUT(self):
        self._route("PUT")
    def do_DELETE(self):
        self._route("DELETE")
    def _route(self, method):
        try:
            u = urllib.parse.urlparse(self.path)
            path = u.path
            qs = urllib.parse.parse_qs(u.query)
            if method == "GET" and path == "/api/state":
                return _json(self, compute_state())
            if method == "GET" and path == "/api/health":
                return _json(self, {"status": "ok", "version": VERSION, "schemaVersion": SCHEMA_VERSION,
                                   "degraded": False, "issues": [], "config": {"writable": True}})
            if method == "GET" and path.startswith("/api/console/log"):
                tail = int(qs.get("tail", ["300"])[0])
                logfile = LOGS_DIR / "console.log"
                text = ""
                if logfile.exists():
                    text = logfile.read_text(encoding="utf-8", errors="replace")[-tail * 200:]
                return _json(self, {"text": text})
            if method == "GET" and path.startswith("/api/apps/") and path.endswith("/logs"):
                app_id = path[len("/api/apps/"):-len("/logs")]
                tail = int(qs.get("tail", ["300"])[0])
                return _json(self, {"text": tail_log(app_id, tail)})
            if method == "POST" and path == "/api/console/restart":
                _discard_body(self)
                _json(self, {"ok": True, "pid": os.getpid(), "port": CONSOLE_PORT})
                threading.Thread(target=_do_restart_console, daemon=True).start()
                return
            if method == "POST" and path == "/api/console/stop":
                _discard_body(self)
                _json(self, {"ok": True, "pid": os.getpid(), "port": CONSOLE_PORT})
                threading.Thread(target=_do_stop_console, daemon=True).start()
                return
            if method == "POST" and path == "/api/ui/theme":
                body = _read_json(self)
                theme = body.get("theme")
                ok = any(t.get("id") == theme for t in load_theme_manifest())
                if ok:
                    CONFIG["uiTheme"] = theme
                    save_config(CONFIG)
                    return _json(self, {"ok": True, "theme": theme})
                return _json(self, {"ok": False, "error": "主题不存在"})
            if method == "POST" and path == "/api/pick":
                body = _read_json(self)
                return _json(self, {"ok": True, "canceled": True})
            if method == "POST" and path == "/api/project/detect":
                body = _read_json(self)
                cwd = body.get("cwd") or str(BASE_DIR)
                return _json(self, project_detect(cwd))
            if method == "POST" and path == "/api/kill":
                body = _read_json(self)
                return _json(self, _kill(body))
            if method == "POST" and path == "/api/services/flag":
                body = _read_json(self)
                return _json(self, _flag(body))
            if method == "POST" and path == "/api/watch":
                body = _read_json(self)
                return _json(self, _watch(body))
            if method == "POST" and path == "/api/apps/reorder":
                body = _read_json(self)
                return _json(self, _reorder(body))
            if method == "POST" and path == "/api/apps":
                body = _read_json(self)
                return _json(self, _create_app(body))
            if path.startswith("/api/apps/"):
                rest = path[len("/api/apps/"):]
                parts = rest.rstrip("/").split("/")
                app = find_app(parts[0]) if parts else None
                if app is None:
                    return _json(self, {"ok": False, "error": "应用不存在"}, 404)
                if len(parts) == 2 and parts[1] == "start" and method == "POST":
                    _discard_body(self)
                    return _json(self, start_app(app))
                if len(parts) == 2 and parts[1] == "stop" and method == "POST":
                    _discard_body(self)
                    return _json(self, stop_app(app))
                if len(parts) == 2 and parts[1] == "restart" and method == "POST":
                    _discard_body(self)
                    return _json(self, restart_app(app))
                if len(parts) == 2 and parts[1] == "diagnose" and method == "POST":
                    _discard_body(self)
                    return _json(self, _diagnose_app(app))
                if len(parts) == 2 and parts[1] == "attach" and method == "POST":
                    body = _read_json(self)
                    return _json(self, _attach_app(app, body))
                if len(parts) == 2 and parts[1] == "favicon" and method == "POST":
                    _discard_body(self)
                    return _json(self, fetch_favicon(app))
                if len(parts) == 2 and parts[1] == "icon" and method == "POST":
                    return _json(self, _upload_icon(app, self))
                if len(parts) == 2 and parts[1] == "icon" and method == "DELETE":
                    app["icon"] = None
                    save_config(CONFIG)
                    return _json(self, {"ok": True})
                if len(parts) == 1 and method == "PUT":
                    body = _read_json(self)
                    return _json(self, _update_app(app, body))
                if len(parts) == 1 and method == "DELETE":
                    return _json(self, _delete_app(app))
                return _json(self, {"ok": False, "error": "未知操作"}, 404)
            if method == "GET":
                if path == "/" or path == "/index.html":
                    return _serve_file(self, STATIC_DIR / "index.html")
                if path == "/favicon.ico":
                    return _serve_file(self, STATIC_DIR / "assets" / "favicon.ico")
                if path.startswith("/icons/"):
                    p = _safe_join(ICONS_DIR, path[len("/icons/"):])
                    if p and p.is_file():
                        return _serve_file(self, p)
                p = _safe_join(STATIC_DIR, path.lstrip("/"))
                if p and p.is_file():
                    return _serve_file(self, p)
                return _json(self, {"ok": False, "error": "not found"}, 404)
            return _json(self, {"ok": False, "error": "method not allowed"}, 405)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                return _json(self, {"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

def main():
    global CONSOLE_PORT, HTTPD
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    start_port = args.port or 9600
    httpd = None
    for p in range(start_port, start_port + 10):
        try:
            httpd = ThreadingHTTPServer((args.host, p), Handler)
            CONSOLE_PORT = p
            HTTPD = httpd
            break
        except OSError:
            continue
    if httpd is None:
        print("[总控台] 无法绑定端口", file=sys.stderr)
        sys.exit(1)
    print(f"[总控台] 已启动 http://{args.host}:{CONSOLE_PORT}")
    try:
        logging.basicConfig(filename=str(LOGS_DIR / "console.log"), level=logging.INFO,
                            format="%(asctime)s %(message)s")
        logging.info(f"[总控台] 已启动 http://{args.host}:{CONSOLE_PORT}")
    except Exception:
        pass
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://{args.host}:{CONSOLE_PORT}")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
