"""Dashboard router — /api/dashboard aggregated infrastructure status"""
import asyncio
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from config import DAILY_LIMITS, STATE_DIR

logger = logging.getLogger("ai-hub.router.dashboard")
router = APIRouter(prefix="/api", tags=["dashboard"])

TAIPEI_TZ = timezone(timedelta(hours=8))
CONTENT_DIR = Path("/opt/gemgate/content/sustainability100")
LOG_DIR = Path("/var/log/ai-hub")
HEARTBEAT_FILE = Path("/opt/gemgate/state/heartbeat.json")

# Injected from main.py
quota = None
job_queue = None
all_providers = None

# In-memory cache (25 sec TTL)
_cache = {"data": None, "at": 0.0}
CACHE_TTL = 55

# ── Node definitions ──

NODES = {
    "ac-mac":     {"role": "中央監控 / AI Hub", "ip": "100.116.154.40", "local": True, "temp": True,
                   "projects": ["ai100", "s100", "infra", "aeo"]},
    "ac-3090":    {"role": "GPU 工作站",        "ip": "100.108.119.78", "gpu": True,
                   "projects": ["vllm"]},
    "ac-rpi5":    {"role": "Raspberry Pi 5",    "ip": "100.71.216.27",  "temp": True,
                   "projects": ["workshop"]},
    "acmacmini2": {"role": "Mac Mini 2014",     "ip": "100.118.162.26", "temp": True,
                   "projects": ["foreclosure", "insurance", "taipower", "world-monitor"]},
    "ac-2012":    {"role": "Mac Mini 2012",     "ip": "100.108.115.6",  "temp": True,
                   "projects": []},
}

# Services to check per host
HOST_SERVICES = {
    "ac-mac":     ["ai-hub", "ai-hub-funnel", "happy-coder", "tg-monitor-bot"],
    "ac-3090":    [],
    "ac-rpi5":    ["happy-coder"],
    "acmacmini2": ["happy-coder"],
    "ac-2012":    [],
}


def init(_job_queue, _quota, _all_providers):
    global quota, job_queue, all_providers
    quota = _quota
    job_queue = _job_queue
    all_providers = _all_providers


# ── SSH helper ──

async def _ssh_run(host: str, cmd: str, timeout: int = 8) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", host, cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip()
    except Exception:
        return ""


async def _local_run(cmd: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode(errors="replace").strip()
    except Exception:
        return ""


# ── Node collection ──

def _parse_uptime(raw: str) -> dict:
    result = {"uptime_raw": raw, "uptime_days": 0, "cpu_load_1m": 0.0}
    if not raw:
        return result
    m = re.search(r'load average[s]?:\s*([\d.]+)', raw)
    if m:
        result["cpu_load_1m"] = float(m.group(1))
    m = re.search(r'up\s+(\d+)\s+day', raw)
    if m:
        result["uptime_days"] = int(m.group(1))
    return result


def _parse_free(raw: str) -> dict:
    result = {"mem_used_gb": 0, "mem_total_gb": 0, "mem_pct": 0}
    if not raw:
        return result
    for line in raw.split("\n"):
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 3:
                total = int(parts[1])
                used = int(parts[2])
                result["mem_total_gb"] = round(total / 1073741824, 1)
                result["mem_used_gb"] = round(used / 1073741824, 1)
                if total > 0:
                    result["mem_pct"] = round(used / total * 100)
    return result


def _parse_df(raw: str) -> dict:
    result = {"disk_used_gb": 0, "disk_total_gb": 0, "disk_pct": 0}
    if not raw:
        return result
    lines = raw.strip().split("\n")
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            total = int(parts[1])
            used = int(parts[2])
            result["disk_total_gb"] = round(total / 1073741824)
            result["disk_used_gb"] = round(used / 1073741824)
            pct_str = parts[4].replace("%", "")
            if pct_str.isdigit():
                result["disk_pct"] = int(pct_str)
    return result


def _parse_gpu(raw: str) -> Optional[dict]:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 5:
        return {
            "name": parts[0],
            "temp_c": int(parts[1]) if parts[1].isdigit() else 0,
            "utilization_pct": int(parts[2].replace(" %", "")) if "%" in parts[2] else 0,
            "mem_used_mb": int(parts[3].replace(" MiB", "")) if "MiB" in parts[3] else 0,
            "mem_total_mb": int(parts[4].replace(" MiB", "")) if "MiB" in parts[4] else 0,
        }
    return None


async def _collect_node(host: str, cfg: dict) -> dict:
    is_local = cfg.get("local", False)
    has_gpu = cfg.get("gpu", False)
    has_temp = cfg.get("temp", False)

    run = _local_run if is_local else lambda cmd: _ssh_run(host, cmd)

    tasks = [
        run("uptime"),
        run("free -b"),
        run("df -B1 /"),
    ]
    if has_temp:
        tasks.append(run("cat /sys/class/thermal/thermal_zone0/temp"))
    else:
        tasks.append(asyncio.sleep(0, result=""))
    if has_gpu:
        tasks.append(run("nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits"))
    else:
        tasks.append(asyncio.sleep(0, result=""))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    uptime_raw = results[0] if isinstance(results[0], str) else ""
    free_raw = results[1] if isinstance(results[1], str) else ""
    df_raw = results[2] if isinstance(results[2], str) else ""
    temp_raw = results[3] if isinstance(results[3], str) else ""
    gpu_raw = results[4] if isinstance(results[4], str) else ""

    online = bool(uptime_raw)
    node = {
        "hostname": host,
        "role": cfg["role"],
        "tailscale_ip": cfg["ip"],
        "online": online,
        "projects": cfg.get("projects", []),
        **_parse_uptime(uptime_raw),
        **_parse_free(free_raw),
        **_parse_df(df_raw),
        "temp_c": None,
    }
    if has_temp and temp_raw and temp_raw.isdigit():
        node["temp_c"] = round(int(temp_raw) / 1000, 1)
    if has_gpu:
        node["gpu"] = _parse_gpu(gpu_raw)
    return node


# ── Services ──

async def _collect_services() -> dict:
    services = {}
    tasks = []
    for host, svc_list in HOST_SERVICES.items():
        for svc in svc_list:
            if host == "ac-mac":
                tasks.append((_local_run(f"systemctl is-active {svc}.service"), host, svc))
            else:
                tasks.append((_ssh_run(host, f"systemctl is-active {svc}.service"), host, svc))

    coros = [t[0] for t in tasks]
    results = await asyncio.gather(*coros, return_exceptions=True)

    for i, (_, host, svc) in enumerate(tasks):
        if host not in services:
            services[host] = []
        status = results[i] if isinstance(results[i], str) else "unknown"
        services[host].append({
            "name": svc,
            "active": status.strip() == "active",
            "status": status.strip() if status.strip() else "unknown",
        })
    return services


# ── Timers ──

CUSTOM_TIMERS = {
    "ac-mac": [
        "ai-hub-pipeline", "automation-news-digest", "automation-pipeline-audit",
        "automation-podcast-tracker", "automation-job-tracker",
        "automation-health-report", "ai-hub-watchdog", "ai-hub-idle-check",
    ],
    "ac-3090": ["diagram-cleanup"],
}

CRON_JOBS = {
    "ac-mac": [
        {"schedule": "0 6 * * *",     "name": "daily-report",       "desc": "每日機器狀態報告 → TG"},
        {"schedule": "5 6 * * *",     "name": "health-check",       "desc": "AI Hub 健康檢查"},
        {"schedule": "10 6 * * *",    "name": "backfill-images",    "desc": "缺圖補圖 (ai100+s100)"},
        {"schedule": "5 9,13,18 * * *","name": "ai100-news",        "desc": "AI 100 講新聞 (3 次/日)"},
        {"schedule": "50 5 * * *",    "name": "chrome-hold-check",  "desc": "Chrome 版本鎖定檢查"},
        {"schedule": "30 3 * * 1,3,5","name": "aeo-monitor",        "desc": "AEO 排名監控 (一三五)"},
    ],
    "acmacmini2": [
        {"schedule": "0 10 * * 2",    "name": "foreclosure-map",    "desc": "法拍物件地圖 (週二)"},
        {"schedule": "30 8,20 * * *", "name": "insurance-kb",       "desc": "保險知識庫爬取 (2 次/日)"},
        {"schedule": "*/5 * * * *",   "name": "taipower-collect",   "desc": "台電資料蒐集 (每 5 分)"},
        {"schedule": "5 * * * *",     "name": "taipower-push",      "desc": "台電資料推送 (每小時)"},
        {"schedule": "0 14 * * *",    "name": "taipower-screenshot", "desc": "台電儀表板截圖"},
        {"schedule": "*/30 * * * *",  "name": "warm-cache",         "desc": "World Monitor 快取預熱"},
    ],
}


async def _collect_timers() -> dict:
    hosts_with_timers = list(CUSTOM_TIMERS.keys())
    raw_results = await asyncio.gather(*[
        (_local_run if h == "ac-mac" else lambda cmd, h=h: _ssh_run(h, cmd))(
            "systemctl list-timers --no-pager --plain 2>/dev/null"
        ) for h in hosts_with_timers
    ])

    timers_by_host = {}
    for i, host in enumerate(hosts_with_timers):
        raw = raw_results[i] if isinstance(raw_results[i], str) else ""
        whitelist = set(CUSTOM_TIMERS.get(host, []))
        items = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line or line.startswith("NEXT") or "timers listed" in line:
                continue
            parts = line.split()
            timer_name = None
            for p in parts:
                if p.endswith(".timer"):
                    timer_name = p.replace(".timer", "")
                    break
            if timer_name and timer_name in whitelist:
                next_run = ""
                if len(parts) >= 4:
                    next_run = f"{parts[1]} {parts[2]}"
                items.append({
                    "name": timer_name,
                    "type": "timer",
                    "next_run": next_run,
                    "raw": line,
                })
        if items:
            timers_by_host[host] = items

    for host, crons in CRON_JOBS.items():
        if host not in timers_by_host:
            timers_by_host[host] = []
        for c in crons:
            timers_by_host[host].append({
                "name": c["name"],
                "type": "cron",
                "schedule": c["schedule"],
                "desc": c.get("desc", ""),
            })

    return timers_by_host


# ── Tracked tasks with groups ──

# source: "journal" = journalctl, "log" = log file parsing, "heartbeat" = heartbeat API
TRACKED_TASKS = {
    # — ai100 group —
    "ai100-news":         {"unit": None, "host": "ac-mac", "desc": "AI100 新聞 (3次/日)",
                           "per_day": 3, "group": "ai100",
                           "log": "/var/log/ai-hub/ai100-news.log", "source": "log"},
    "backfill-images":    {"unit": None, "host": "ac-mac", "desc": "缺圖補圖",
                           "per_day": 1, "group": "ai100", "source": "heartbeat"},
    # — s100 group —
    "s100-pipeline":      {"unit": "ai-hub-pipeline", "host": "ac-mac", "desc": "S100 Pipeline",
                           "per_day": 1, "group": "s100", "source": "journal"},
    "s100-news":          {"unit": "automation-news-digest", "host": "ac-mac", "desc": "永續新聞 (3次/日)",
                           "per_day": 3, "group": "s100", "source": "journal"},
    "pipeline-audit":     {"unit": "automation-pipeline-audit", "host": "ac-mac", "desc": "Pipeline 審計",
                           "per_day": 1, "group": "s100", "source": "journal"},
    "podcast-tracker":    {"unit": "automation-podcast-tracker", "host": "ac-mac", "desc": "Podcast 追蹤",
                           "per_day": 96, "group": "s100", "source": "journal"},
    "job-tracker":        {"unit": "automation-job-tracker", "host": "ac-mac", "desc": "Job 追蹤",
                           "per_day": 48, "group": "s100", "source": "journal"},
    # — infra group —
    "ai-hub":             {"unit": "ai-hub", "host": "ac-mac", "desc": "AI Hub 服務",
                           "per_day": 1, "group": "infra", "source": "journal", "health": True},
    "watchdog":           {"unit": "ai-hub-watchdog", "host": "ac-mac", "desc": "AI Hub Watchdog",
                           "per_day": 1, "group": "infra", "source": "journal"},
    "daily-report":       {"unit": None, "host": "ac-mac", "desc": "每日系統報告",
                           "per_day": 1, "group": "infra", "source": "heartbeat"},
    "health-check":       {"unit": None, "host": "ac-mac", "desc": "AI Hub 健康檢查",
                           "per_day": 1, "group": "infra", "source": "heartbeat"},
    "health-report":      {"unit": "automation-health-report", "host": "ac-mac", "desc": "自動健康報告",
                           "per_day": 1, "group": "infra", "source": "journal"},
    "chrome-hold-check":  {"unit": None, "host": "ac-mac", "desc": "Chrome 版本鎖定",
                           "per_day": 1, "group": "infra", "source": "heartbeat"},
    "diagram-cleanup":    {"unit": "diagram-cleanup", "host": "ac-3090", "desc": "圖表清理",
                           "per_day": 1, "group": "infra", "source": "journal"},
    # — aeo group —
    "aeo-monitor":        {"unit": None, "host": "ac-mac", "desc": "AEO 排名監控 (一三五)",
                           "per_day": 0, "group": "aeo", "source": "heartbeat"},
    # — acmacmini2 projects —
    "foreclosure-map":    {"unit": None, "host": "acmacmini2", "desc": "法拍地圖 (週二)",
                           "per_day": 0, "group": "foreclosure", "source": "heartbeat"},
    "insurance-kb":       {"unit": None, "host": "acmacmini2", "desc": "保險知識庫 (2次/日)",
                           "per_day": 2, "group": "insurance", "source": "heartbeat"},
    "taipower-collect":   {"unit": None, "host": "acmacmini2", "desc": "台電資料蒐集",
                           "per_day": 288, "group": "taipower", "source": "heartbeat"},
    "taipower-screenshot":{"unit": None, "host": "acmacmini2", "desc": "台電儀表板截圖",
                           "per_day": 1, "group": "taipower", "source": "heartbeat"},
}

# Group display names
TASK_GROUPS = {
    "ai100":        "AI 100 講",
    "s100":         "永續 100 講",
    "infra":        "基礎設施",
    "aeo":          "AEO 監控",
    "foreclosure":  "法拍地圖",
    "insurance":    "保險知識庫",
    "taipower":     "台電資料",
}


def _read_heartbeat() -> dict:
    """Read heartbeat store."""
    if HEARTBEAT_FILE.exists():
        try:
            return json.loads(HEARTBEAT_FILE.read_text())
        except Exception:
            return {}
    return {}


async def _collect_task_history() -> list:
    """Collect 7-day execution history for all tracked tasks."""
    from datetime import date, timedelta as td
    today = date.today()
    dates = [(today - td(days=i)).isoformat() for i in range(6, -1, -1)]

    # Build journalctl commands per host
    host_units = {}
    for tname, tcfg in TRACKED_TASKS.items():
        unit = tcfg.get("unit")
        host = tcfg["host"]
        if unit and tcfg.get("source") == "journal":
            if host not in host_units:
                host_units[host] = set()
            host_units[host].add(unit)

    # Parallel: one SSH per host for journal
    async def _get_journal(host, units):
        unit_flags = " ".join(f"-u {u}.service" for u in units)
        cmd = f"journalctl {unit_flags} --since '7 days ago' --no-pager -o short 2>/dev/null | grep 'systemd\\[' | grep -E 'Finished|Failed|Started'"
        if host == "ac-mac":
            return await _local_run(cmd)
        return await _ssh_run(host, cmd, timeout=10)

    async def _get_ai100_log():
        cmd = "grep -E '文章產出|圖片生成成功|圖片生成最終失敗' /var/log/ai-hub/ai100-news.log 2>/dev/null | tail -100"
        return await _local_run(cmd)

    tasks_to_run = []
    host_order = []
    for host, units in host_units.items():
        tasks_to_run.append(_get_journal(host, units))
        host_order.append(host)
    tasks_to_run.append(_get_ai100_log())

    results = await asyncio.gather(*tasks_to_run, return_exceptions=True)

    # Parse journal results
    unit_history = {}
    for i, host in enumerate(host_order):
        raw = results[i] if isinstance(results[i], str) else ""
        for line in raw.split("\n"):
            if not line.strip():
                continue
            m = re.match(r"\s*(\d+)月\s+(\d+)\s+\d+:\d+:\d+", line)
            if not m:
                m2 = re.match(r"\s*(\w+)\s+(\d+)\s+\d+:\d+:\d+", line)
                if m2:
                    month_map = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
                                 "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
                    mon = month_map.get(m2.group(1), "01")
                    day = m2.group(2).zfill(2)
                    d_str = f"{today.year}-{mon}-{day}"
                else:
                    continue
            else:
                mon = m.group(1).zfill(2)
                day = m.group(2).zfill(2)
                d_str = f"{today.year}-{mon}-{day}"

            if "Failed" in line:
                status = "failed"
            elif "Finished" in line:
                status = "ok"
            elif "Started" in line or "Starting" in line:
                status = "started"
            else:
                continue

            unit_name = None
            for tname, tcfg in TRACKED_TASKS.items():
                u = tcfg.get("unit")
                if not u:
                    continue
                svc = u + ".service"
                if svc in line:
                    unit_name = u
                    break
            if not unit_name:
                if "Pipeline" in line and "Sustainability 100" in line:
                    unit_name = "ai-hub-pipeline"
                elif "News" in line or "news-digest" in line:
                    unit_name = "automation-news-digest"
                elif "Health" in line:
                    unit_name = "automation-health-report"
                elif "Audit" in line:
                    unit_name = "automation-pipeline-audit"
                elif "Watchdog" in line or "watchdog" in line:
                    unit_name = "ai-hub-watchdog"
                elif "AI Service Hub" in line or "ai-hub.service" in line:
                    unit_name = "ai-hub"
                elif "diagram" in line.lower():
                    unit_name = "diagram-cleanup"
                elif "podcast" in line.lower():
                    unit_name = "automation-podcast-tracker"
                elif "job" in line.lower() and "tracker" in line.lower():
                    unit_name = "automation-job-tracker"

            if unit_name and d_str in dates:
                if unit_name not in unit_history:
                    unit_history[unit_name] = {}
                if d_str not in unit_history[unit_name]:
                    unit_history[unit_name][d_str] = {"ok": 0, "failed": 0, "started": 0}
                if status == "ok":
                    unit_history[unit_name][d_str]["ok"] += 1
                elif status == "failed":
                    unit_history[unit_name][d_str]["failed"] += 1
                elif status == "started":
                    unit_history[unit_name][d_str]["started"] += 1

    # Parse ai100 log
    ai100_raw = results[-1] if isinstance(results[-1], str) else ""
    ai100_articles = {}
    ai100_images = {}
    ai100_img_fail = {}
    for line in ai100_raw.split("\n"):
        if "文章產出" in line:
            m = re.search(r"(\d{4}-\d{2}-\d{2})-[a-f0-9]+\.md", line)
            if m and m.group(1) in dates:
                ai100_articles[m.group(1)] = ai100_articles.get(m.group(1), 0) + 1
        elif "圖片生成成功" in line:
            m = re.search(r"(\d{4}-\d{2}-\d{2})-[a-f0-9]+\.png", line)
            if m and m.group(1) in dates:
                ai100_images[m.group(1)] = ai100_images.get(m.group(1), 0) + 1
        elif "圖片生成最終失敗" in line:
            m = re.search(r"(\d{4}-\d{2}-\d{2})-[a-f0-9]+\.png", line)
            if m and m.group(1) in dates:
                ai100_img_fail[m.group(1)] = ai100_img_fail.get(m.group(1), 0) + 1
    ai100_dates = {}
    for d_str in dates:
        n_art = ai100_articles.get(d_str, 0)
        n_img = ai100_images.get(d_str, 0)
        n_fail = ai100_img_fail.get(d_str, 0)
        if n_art == 0:
            continue
        if n_fail > 0 or n_img < n_art:
            ai100_dates[d_str] = "partial"
        else:
            ai100_dates[d_str] = "ok"

    # Health service special handling
    for tname, tcfg in TRACKED_TASKS.items():
        if not tcfg.get("health"):
            continue
        unit = tcfg["unit"]
        if unit and unit in unit_history:
            for d_str, counts in list(unit_history[unit].items()):
                starts = counts.get("started", 0)
                crashes = counts.get("failed", 0)
                if crashes > 0:
                    unit_history[unit][d_str] = {"ok": max(0, starts - crashes), "failed": crashes, "started": 0}
                elif starts > 0:
                    unit_history[unit][d_str] = {"ok": 1, "failed": 0, "started": 0}

    # Read heartbeat data
    heartbeat = _read_heartbeat()

    # Build final output
    result = []
    for tname, tcfg in TRACKED_TASKS.items():
        unit = tcfg.get("unit")
        is_health = tcfg.get("health", False)
        source = tcfg.get("source", "journal")
        group = tcfg.get("group", "other")
        days = []

        for d_str in dates:
            if source == "log" and tname == "ai100-news":
                st = ai100_dates.get(d_str, "none")
            elif source == "journal" and unit and unit in unit_history:
                counts = unit_history[unit].get(d_str)
                if counts is None:
                    st = "ok" if is_health else "none"
                elif counts["failed"] == 0:
                    st = "ok"
                elif counts["ok"] == 0:
                    st = "failed"
                else:
                    st = "partial" if counts["ok"] >= counts["failed"] else "failed"
            elif source == "heartbeat" and tname in heartbeat:
                # Check heartbeat history for this date
                hb = heartbeat[tname]
                st = "none"
                for entry in hb.get("history", []):
                    ts = entry.get("timestamp", "")
                    if ts.startswith(d_str):
                        st = entry.get("status", "none")
                        break  # use most recent entry for this date
            else:
                st = "none"
            days.append({"date": d_str, "status": st})

        result.append({
            "name": tname,
            "desc": tcfg["desc"],
            "host": tcfg["host"],
            "group": group,
            "days": days,
        })
    return result


# ── Tailscale ──

async def _collect_tailscale() -> dict:
    raw = await _local_run("tailscale status --json 2>/dev/null")
    result = {"nodes": [], "self_online": True}
    if not raw:
        return result
    try:
        data = json.loads(raw)
        peers = data.get("Peer", {})
        myself = data.get("Self", {})
        if myself:
            result["nodes"].append({
                "hostname": myself.get("HostName", "self"),
                "ip": myself.get("TailscaleIPs", [""])[0] if myself.get("TailscaleIPs") else "",
                "online": myself.get("Online", True),
            })
        for _, peer in peers.items():
            result["nodes"].append({
                "hostname": peer.get("HostName", "?"),
                "ip": peer.get("TailscaleIPs", [""])[0] if peer.get("TailscaleIPs") else "",
                "online": peer.get("Online", False),
            })
    except Exception:
        pass
    return result


# ── Pipeline ──

async def _collect_pipeline() -> dict:
    result = {
        "project": "低碳永續 100 講",
        "total": 100,
        "completed": 0,
        "in_progress": 0,
        "pending": 0,
        "episode_grid": [],
    }
    topics_file = CONTENT_DIR / "topics.json"
    if not topics_file.exists():
        return result

    try:
        topics_data = json.loads(topics_file.read_text())
        topics = topics_data.get("topics", [])
        result["total"] = len(topics)

        for topic in topics:
            ep_id = topic["id"]
            title = topic.get("title", "")
            state_file = CONTENT_DIR / ep_id / "state.json"

            if state_file.exists():
                state = json.loads(state_file.read_text())
                steps = state.get("steps_completed", [])
                if 7 in steps:
                    status = "complete"
                    result["completed"] += 1
                elif steps:
                    status = "partial"
                    result["in_progress"] += 1
                else:
                    status = "pending"
                    result["pending"] += 1
            else:
                steps = []
                status = "pending"
                result["pending"] += 1

            result["episode_grid"].append({
                "ep": ep_id,
                "title": title,
                "status": status,
                "steps": sorted(steps),
            })
    except Exception as e:
        logger.warning(f"Pipeline collection error: {e}")

    if result["total"] > 0:
        result["completion_pct"] = round(result["completed"] / result["total"] * 100)
    else:
        result["completion_pct"] = 0
    return result


# ── AI Providers ──

def _collect_providers() -> dict:
    providers = {}
    if not all_providers:
        return providers
    for name, prov in all_providers.items():
        used = quota.get_used(name) if quota else 0
        limit = DAILY_LIMITS.get(name, 999999)
        providers[name] = {
            "category": getattr(prov, "category", "unknown"),
            "busy": job_queue.is_busy(getattr(prov, "chrome_profile", None)) if job_queue else False,
            "healthy": True,
            "today_used": used,
            "daily_limit": limit,
            "remaining": max(0, limit - used),
        }
    return providers


# ── Log tails ──

async def _collect_log_tails() -> dict:
    logs = {}
    log_files = {
        "pipeline": LOG_DIR / "pipeline.log",
        "news": LOG_DIR / "sustainability-news.log",
        "ai100": LOG_DIR / "ai100-news.log",
        "audit": LOG_DIR / "pipeline-audit.log",
    }
    for name, path in log_files.items():
        if path.exists():
            try:
                lines = path.read_text().strip().split("\n")
                logs[name] = lines[-5:] if len(lines) >= 5 else lines
            except Exception:
                logs[name] = []
        else:
            logs[name] = []
    return logs


# ── Alerts & Todos ──

def _build_alerts_todos(providers: dict, services: dict, pipeline: dict) -> tuple:
    alerts = []
    todos = []

    for name, p in providers.items():
        if p["daily_limit"] < 999999 and p["remaining"] == 0:
            alerts.append({
                "level": "warning",
                "source": name,
                "message": f"{name} 今日配額已用盡 ({p['today_used']}/{p['daily_limit']})",
            })

    for host, svc_list in services.items():
        for svc in svc_list:
            if not svc["active"] and svc["status"] != "unknown":
                alerts.append({
                    "level": "error",
                    "source": f"{host}/{svc['name']}",
                    "message": f"{host} 上的 {svc['name']} 服務 {svc['status']}",
                })

    for ep in pipeline.get("episode_grid", []):
        if ep["status"] == "partial":
            missing = [s for s in [1,2,4,7,6] if s not in ep["steps"]]
            if missing:
                step_names = {1: "研究", 2: "Podcast", 4: "圖卡", 6: "通知", 7: "部署"}
                missing_names = [step_names.get(s, str(s)) for s in missing]
                todos.append({
                    "priority": "medium",
                    "text": f"{ep['ep']} 缺少: {', '.join(missing_names)}",
                })

    return alerts, todos


# ── Main endpoint ──

@router.get("/dashboard")
async def dashboard():
    now = time.monotonic()
    if _cache["data"] and (now - _cache["at"]) < CACHE_TTL:
        return _cache["data"]

    node_tasks = {host: _collect_node(host, cfg) for host, cfg in NODES.items()}
    node_results = await asyncio.gather(*node_tasks.values(), return_exceptions=True)
    nodes = {}
    for i, host in enumerate(node_tasks.keys()):
        if isinstance(node_results[i], dict):
            nodes[host] = node_results[i]
        else:
            nodes[host] = {"hostname": host, "online": False, "error": str(node_results[i])}

    services, timers, tailscale, pipeline, log_tails, task_history = await asyncio.gather(
        _collect_services(),
        _collect_timers(),
        _collect_tailscale(),
        _collect_pipeline(),
        _collect_log_tails(),
        _collect_task_history(),
        return_exceptions=True,
    )
    if not isinstance(services, dict): services = {}
    if not isinstance(timers, dict): timers = {}
    if not isinstance(tailscale, dict): tailscale = {"nodes": []}
    if not isinstance(pipeline, dict): pipeline = {}
    if not isinstance(log_tails, dict): log_tails = {}
    if not isinstance(task_history, list): task_history = []

    providers = _collect_providers()
    alerts, todos = _build_alerts_todos(providers, services, pipeline)

    data = {
        "generated_at": datetime.now(tz=TAIPEI_TZ).isoformat(),
        "nodes": nodes,
        "services": services,
        "timers": timers,
        "ai_providers": providers,
        "pipeline": pipeline,
        "network": tailscale,
        "alerts": alerts,
        "todos": todos,
        "log_tails": log_tails,
        "task_history": task_history,
        "task_groups": TASK_GROUPS,
    }

    _cache["data"] = data
    _cache["at"] = now
    return data
