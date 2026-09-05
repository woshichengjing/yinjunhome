#!/usr/bin/env python3
"""
HA 智能温控控制器 — 编排层
每分钟轮询 HA，顺序调用各状态机模块，输出仪表盘数据。
systemd 管理：Restart=always
"""

import json, os, sys, time, shutil

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

# 确保模块路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE_DIR = "/tmp/hermes_states"
DATA_DIR = "/opt/ha-controller/data"
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# HASS_TOKEN — 从宿主机的 ~/.hermes/.env 读取
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
if not HASS_TOKEN:
    for p in [os.path.expanduser("~/.hermes/.env"), "/home/hermeswebui/.hermes/.env"]:
        if os.path.isfile(p):
            with open(p) as f:
                for line in f:
                    if line.startswith("HASS_TOKEN="):
                        HASS_TOKEN = line.strip().split("=", 1)[1].strip('"').strip("'")
                        break
        if HASS_TOKEN:
            break

if not HASS_TOKEN:
    print("[FATAL] HASS_TOKEN not found", flush=True)
    sys.exit(1)

os.environ["HASS_TOKEN"] = HASS_TOKEN


def write_json(filename: str, data: dict):
    """写 JSON 到 STATE_DIR、DATA_DIR 和仪表盘目录。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    for d in [STATE_DIR, DATA_DIR, "/root/.hermes/static/data", "/app/static/data"]:
        try:
            path = os.path.join(d, filename)
            with open(path, "w") as f:
                f.write(payload)
            os.chmod(path, 0o644)
        except OSError:
            pass


def sync_file(src_name: str, dst_name: str = None):
    """复制文件到 DATA_DIR 和仪表盘。"""
    if dst_name is None:
        dst_name = src_name
    src = os.path.join(STATE_DIR, src_name)
    if os.path.isfile(src):
        for d in [DATA_DIR, "/root/.hermes/static/data", "/app/static/data"]:
            try:
                dst = os.path.join(d, dst_name)
                shutil.copy2(src, dst)
                os.chmod(dst, 0o644)
            except OSError:
                pass


def _standby_record(room_data: dict):
    """每轮采集房间活动状态到日志，供待机模式分析。"""
    if not room_data or not room_data.get("rooms"):
        return
    rooms = room_data["rooms"]
    now = __import__('datetime').datetime.now()
    wd = now.weekday() < 5  # 简化：工作日=周一到周五
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
    log_file = os.path.expanduser("~/.hermes/scripts/data/standby_data.jsonl")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    lines = []
    for rk, room in rooms.items():
        act = room.get("activity", "unknown")
        if isinstance(act, list):
            act = ",".join(act)
        lines.append(json.dumps({"ts": ts_str, "room": rk, "activity": act, "is_workday": wd}, ensure_ascii=False))
    with open(log_file, "a") as f:
        f.write("\n".join(lines) + "\n")
    # 清理14天前数据
    cutoff = now - __import__('datetime').timedelta(days=14)
    if os.path.isfile(log_file):
        with open(log_file) as f:
            all_lines = f.readlines()
        kept = []
        for line in all_lines:
            try:
                row = json.loads(line.strip())
                ts = __import__('datetime').datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    kept.append(line)
            except Exception:
                kept.append(line)
        with open(log_file, "w") as f:
            f.writelines(kept)


def main():
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    errors = []

    # ── 1. 房间状态（由 room-state-machine 服务写入 room_state.json）──
    room_data = {}
    try:
        with open(os.path.join(STATE_DIR, "room_state.json")) as f:
            room_data = json.load(f)
        write_json("state_room_state.json", room_data)
    except Exception as e:
        errors.append(f"room_state: {e}")

    # ── 2. 环境质量 ──
    try:
        from state_machines.env_quality import run as env_quality_run
        env_data = env_quality_run()
        write_json("state_env_quality.json", env_data)
    except Exception as e:
        errors.append(f"env_quality: {e}")

    # ── 2.5. 温控意图层 ──
    try:
        from state_machines.climate_intent import run as intent_run
        intent_data = intent_run()
        write_json("state_climate_intent.json", intent_data)
    except Exception as e:
        errors.append(f"climate_intent: {e}")

    # ── 3. 空调引擎 ──
    try:
        from state_machines.climate_engine import run as engine_run
        engine_run()
    except Exception as e:
        errors.append(f"climate_engine: {e}")

    # ── 3.5. 决策审计日志 ──
    try:
        from state_machines.climate_logger import run as logger_run
        logger_run()
    except Exception as e:
        errors.append(f"climate_logger: {e}")

    # ── 4. 设备保护 ──
    try:
        from state_machines.device_protection import run as device_run
        dev_data = device_run()
        write_json("state_device_protection.json", dev_data)
    except Exception as e:
        errors.append(f"device_protection: {e}")

    # ── 5. 外部环境 ──
    try:
        from state_machines.external_env import run as ext_env_run
        ext_data = ext_env_run()
        write_json("state_external_env.json", ext_data)
    except Exception as e:
        errors.append(f"external_env: {e}")

    # ── 6. 同步其他输出 ──
    sync_file("room_mode.json", "state_room_mode.json")
    sync_file("climate_intent.json", "state_climate_intent.json")
    sync_file("standby_schedule.json", "standby_schedule.json")
    sync_file("precool_schedule.json", "precool_schedule.json")
    sync_file("ac_disabled.json", "ac_disabled.json")

    # ── 7. 传感器快照 ──
    try:
        from snapshot_sensors import main as snap_main
        snap_main()
    except Exception as e:
        errors.append(f"snapshot_sensors: {e}")

    # ── 8. 能耗 ──
    try:
        from energy_tracker import run as energy_run
        energy_run()
    except Exception as e:
        errors.append(f"energy_tracker: {e}")

    # ── 9. 待机数据采集 ──
    try:
        _standby_record(room_data)
    except Exception as e:
        errors.append(f"standby_record: {e}")

    if errors:
        print(f"[{ts}] ⚠ errors: {'; '.join(errors)}", flush=True)
    else:
        print(f"[{ts}] ✓ all modules ok", flush=True)


if __name__ == "__main__":
    print(f"[controller] started, HASS_TOKEN={'✓' if HASS_TOKEN else '✗'}", flush=True)
    import importlib, sys

    while True:
        try:
            # 彻底清除所有状态机相关的模块缓存
            for key in list(sys.modules.keys()):
                if any(x in key for x in ['state_machines', 'room_state', 'env_quality', 'device_protection', 'external_env', 'climate_engine', 'snapshot_sensors', 'energy_tracker']):
                    del sys.modules[key]
            main()
        except Exception as e:
            print(f"[controller] fatal: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(60)
