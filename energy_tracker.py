#!/usr/bin/env python3
"""energy_tracker.py — 按采样间隔累计运行时长，输出能耗估算和 30 天历史快照。"""
import json, os, shutil, time
from datetime import datetime
from zoneinfo import ZoneInfo

STATE_DIR = "/tmp/hermes_states"
DST = "/app/static/data"
HIST_FILE = os.path.join(STATE_DIR, "energy_history.json")
EST_WATT = 1500
DEHUM_WATT = 300
AC_IDLE_WATT = 80    # 待机/风扇基础功耗
MAX_DAYS = 30
ZONE = ZoneInfo("Asia/Shanghai")

def run(dashboard_dir=DST):
    now_ts = int(time.time())
    today = datetime.fromtimestamp(now_ts, ZONE).date().isoformat()
    energy_file = os.path.join(STATE_DIR, "energy_stats.json")

    # ── 当日累计 ──
    stats = {"date": today, "rooms": {}, "_last_ts": now_ts}
    try:
        if os.path.isfile(energy_file):
            with open(energy_file) as f:
                stats = json.load(f)
    except: pass
    if stats.get("date") != today:
        stats = {"date": today, "rooms": {}, "_last_ts": now_ts}

    elapsed = max(0, now_ts - stats.get("_last_ts", now_ts))
    if elapsed > 600:
        elapsed = 300
    stats["_last_ts"] = now_ts

    # 读设备状态
    devp = {}
    try:
        with open(os.path.join(STATE_DIR, "device_protection.json")) as f:
            devp = json.load(f).get("devices", {})
    except: pass

    snapshot_devices = {}
    total_watts = 0

    for dev_id, dev in devp.items():
        watt = 0
        room_key = None
        if dev_id.endswith("_ac") and dev.get("switch") == "on":
            room_key = dev_id.replace("_ac", "")
            if dev.get("compressor_load") == "带载中":
                watt = EST_WATT
            else:
                watt = AC_IDLE_WATT     # 待机/风扇基础功耗
        elif dev_id.endswith("_dehum") and dev.get("switch") == "on":
            room_key = dev_id.replace("_dehum", "") + "除湿"
            watt = DEHUM_WATT

        if room_key:
            if room_key not in stats["rooms"]:
                stats["rooms"][room_key] = {"runtime_sec": 0, "est_kwh": 0}
            stats["rooms"][room_key]["runtime_sec"] += (elapsed if watt > 0 else 0)
            room_stats = stats["rooms"][room_key]
            room_stats["est_kwh"] = round(room_stats["est_kwh"] + watt * elapsed / 3600000, 6)

        snapshot_devices[dev_id] = {"watts": watt, "runtime_sec": elapsed if watt > 0 else 0}
        total_watts += watt

    # 写当日累计
    os.makedirs(os.path.dirname(energy_file), exist_ok=True)
    with open(energy_file, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    # ── 历史快照 ──
    history = []
    try:
        if os.path.isfile(HIST_FILE):
            with open(HIST_FILE) as f:
                history = json.load(f)
    except: pass

    snapshot = {"ts": now_ts, "total_watts": total_watts, "devices": snapshot_devices}
    history.append(snapshot)

    # 滚动保留 MAX_DAYS 天
    cutoff = now_ts - MAX_DAYS * 86400
    history = [p for p in history if p["ts"] >= cutoff]

    # 仅保留最近 30 天的采样点。
    with open(HIST_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))

    # 独立运行时兼容旧的直出路径，控制器通过持久化目录发布。
    if dashboard_dir is not None:
        for src, name in ((energy_file, "state_energy_stats.json"), (HIST_FILE, "state_energy_history.json")):
            dst = os.path.join(dashboard_dir, name)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o644)

    print(f"energy: today={stats['rooms']}, history={len(history)} snapshots")


if __name__ == "__main__":
    run()
