#!/usr/bin/env python3
"""energy_tracker.py — 每5分钟累计压缩机带载时长，输出能耗估算 + 30天历史快照"""
import json, os, time

STATE_DIR = "/tmp/hermes_states"
DST = "/app/static/data"
HIST_FILE = os.path.join(STATE_DIR, "energy_history.json")
EST_WATT = 1500
DEHUM_WATT = 300
AC_IDLE_WATT = 80    # 待机/风扇基础功耗
MAX_DAYS = 30

def run():
    now_ts = int(time.time())
    today = time.strftime("%Y-%m-%d")
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
            stats["rooms"][room_key]["est_kwh"] += round(watt * elapsed / 3600000, 4)
            stats["rooms"][room_key]["est_kwh"] = round(stats["rooms"][room_key]["est_kwh"], 2)

        snapshot_devices[dev_id] = {"watts": watt, "runtime_sec": elapsed if watt > 0 else 0}
        total_watts += watt

    # 写当日累计
    os.makedirs(os.path.dirname(energy_file), exist_ok=True)
    with open(energy_file, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    import shutil
    dst_file = os.path.join(DST, "state_energy_stats.json")
    shutil.copy2(energy_file, dst_file)
    os.chmod(dst_file, 0o644)

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

    # 每天最多 288 条，30 天 ≈ 8640 条 → 保持合理大小
    with open(HIST_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))

    # 同步历史到静态目录
    dst_hist = os.path.join(DST, "state_energy_history.json")
    shutil.copy2(HIST_FILE, dst_hist)
    os.chmod(dst_hist, 0o644)

    print(f"energy: today={stats['rooms']}, history={len(history)} snapshots")


if __name__ == "__main__":
    run()
