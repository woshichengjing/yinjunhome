#!/usr/bin/env python3
"""climate_intent.py — 温控意图层 v2.5
输入 room_state + env_quality → 输出每个房间的温控意图（要什么，不是怎么做）。
"""
import json, os, time
from datetime import datetime
from state_machines.climate_policy import cooling_allowed, has_runaway, guest_active
from state_machines.control_switches import read_control_switches

STATE_DIR = "/tmp/hermes_states"
CONFIG_FILE = os.path.expanduser("~/.hermes/scripts/data/climate_config.json")
INPUT_MAX_AGE_SECONDS = 180

os.environ["TZ"] = "Asia/Shanghai"
try: __import__("time").tzset()
except: pass


def _load(name: str) -> dict:
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_engine_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get("engine", {})
    except Exception:
        return {}


def _is_fresh(payload: dict, max_age: int = INPUT_MAX_AGE_SECONDS) -> bool:
    """核心输入必须带生成时间，过期或未来时间都按不可用处理。"""
    try:
        generated_at = payload.get("generated_at")
        if generated_at is None and payload.get("ts"):
            generated_at = datetime.strptime(payload["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
        age = int(time.time()) - int(generated_at or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    return 0 <= age <= max_age


def _automatic_power_request(conditions: list) -> str:
    """与执行层一致：冷状态、舒适、偏湿或仅空气问题都不请求自动开机。"""
    return "on" if cooling_allowed(conditions) else "hold"


def run() -> dict:
    """Generate climate intent for all controlled rooms. Returns {ts, intents: {room: {...}}}."""
    room_state_data = _load("room_state.json")
    env_data = _load("env_quality.json")
    room_state = room_state_data.get("rooms", {})
    env = env_data.get("rooms", {})
    stale_inputs = []
    if not _is_fresh(room_state_data):
        stale_inputs.append("room_state")
    if not _is_fresh(env_data):
        stale_inputs.append("env_quality")
    suite_bath = _load_engine_config().get("suite_bath", {})

    hour = datetime.now().hour
    intents = {}

    for room in ("br", "st", "lr", "dr", "nb", "sb"):
        if stale_inputs:
            intents[room] = {
                "occupancy": "unknown",
                "purpose": "input_stale",
                "comfort_target": None,
                "hvac_preference": "hold",
                "power_request": "hold",
                "priority": 0,
                "reason": "stale_inputs:" + ",".join(stale_inputs),
                "source": stale_inputs,
            }
            continue
        rs = room_state.get(room, {})
        es = env.get(room, {})

        activity = rs.get("activity", "unknown")
        bath = suite_bath.get(room)
        bath_activity = room_state.get(bath, {}).get("activity", "unknown") if bath else "unknown"
        if bath and activity in ("empty", "unknown") and bath_activity in ("occupied", "entering"):
            activity = "occupied"
        readings = es.get("readings", {})
        conditions = es.get("condition", [])
        thresholds = es.get("thresholds", {})

        at_val = readings.get("at")
        temp_val = readings.get("temp")
        rh_val = readings.get("hum")

        # ── P0: 设备安全 ──
        switches = read_control_switches(STATE_DIR, room + "_ac")
        # 禁用
        if switches["room_disabled"] is True:
            intents[room] = {
                "occupancy": activity,
                "purpose": "disabled",
                "comfort_target": None,
                "hvac_preference": "off",
                "power_request": "off",
                "priority": 0,
                "reason": "disabled",
                "source": ["room_state"],
            }
            continue

        # 软关
        if switches["device_soft_off"] is True:
            intents[room] = {
                "occupancy": activity,
                "purpose": "soft_off",
                "comfort_target": None,
                "hvac_preference": "off",
                "power_request": "off",
                "priority": 1,
                "reason": "soft_off",
                "source": ["device_soft_off"],
            }
            continue

        if not switches["automation_allowed"]:
            intents[room] = {
                "occupancy": activity, "purpose": "switch_state_unknown",
                "comfort_target": None, "hvac_preference": "hold", "power_request": "hold",
                "priority": 0, "reason": "switch_state_unknown", "source": ["control_switches"],
            }
            continue

        # 跑温是全局否决，必须先于睡眠、待客和共享空间舒适策略。
        if has_runaway(room, conditions, lambda r: env.get(r, {}).get("condition", [])):
            intents[room] = {
                "occupancy": activity, "purpose": "energy", "comfort_target": 30,
                "hvac_preference": "off", "power_request": "hold", "priority": 2,
                "reason": "occupied_runaway" if activity == "occupied" else "runaway",
                "source": ["env_quality"],
            }
            continue

        # ── P2: 房间保护 ──
        # Guest mode (night-time lr/dr manual on)
        if room in ("lr", "dr") and activity in ("empty", "unknown"):
            if guest_active(STATE_DIR, datetime.now()):
                intents[room] = {
                    "occupancy": "occupied",
                    "purpose": "guest_mode",
                    "comfort_target": 27.5,
                    "hvac_preference": "cool",
                    "power_request": "hold",
                    "priority": 2,
                    "reason": "guest_mode",
                    "source": ["climate_engine"],
                }
                continue

        # ── P3: 房间状态 ──
        is_sleeping = (activity == "sleeping")
        is_occupied = activity in ("occupied", "entering", "chaxi", "resting",
                                    "napping", "using_computer", "watching_movie",
                                    "watching_tv")
        is_empty = activity in ("empty", "unknown")

        if is_sleeping:
            # Sleep: 前半夜 27.5, 后半夜 28.5
            sleep_target = 28.5 if 3 <= hour < 10 else 27.5
            reason = "sleeping_after_midnight" if 3 <= hour < 10 else "sleeping_before_midnight"
            intents[room] = {
                "occupancy": "sleeping",
                "purpose": "sleeping",
                "comfort_target": sleep_target,
                "hvac_preference": "cool",
                "power_request": _automatic_power_request(conditions),
                "priority": 3,
                "reason": reason,
                "source": ["room_state"],
            }
            continue

        if is_occupied:
            # ── P4: 温控策略 ──
            # Normal occupied → comfort
            intents[room] = {
                "occupancy": activity,
                "purpose": "comfort",
                "comfort_target": 27.5,
                "hvac_preference": "cool",
                "power_request": _automatic_power_request(conditions),
                "priority": 4,
                "reason": "occupied_comfort",
                "source": ["room_state", "env_quality"],
            }
            continue

        if is_empty:
            # LR/DR 共享空间：客餐厅任一有人 → 全都不节能
            if room in ("lr", "dr"):
                ACTIVE_STATES = {"occupied", "sleeping", "chaxi", "resting", "napping",
                                 "using_computer", "watching_movie", "watching_tv"}
                any_occupied = any(
                    room_state.get(r, {}).get("activity") in ACTIVE_STATES
                    for r in ("lr", "dr", "en", "cr", "kt")
                )
                if any_occupied:
                    intents[room] = {
                        "occupancy": activity,
                        "purpose": "comfort",
                        "comfort_target": 27.5,
                        "hvac_preference": "cool",
                        "power_request": _automatic_power_request(conditions),
                        "priority": 4,
                        "reason": "shared_space_comfort",
                        "source": ["room_state"],
                    }
                    continue
            intents[room] = {
                "occupancy": "empty",
                "purpose": "energy",
                "comfort_target": 30,
                "hvac_preference": "cool",
                "power_request": "hold",
                "priority": 4,
                "reason": "energy_empty",
                "source": ["room_state"],
            }
            continue

        # Fallback
        intents[room] = {
            "occupancy": activity,
            "purpose": "energy",
            "comfort_target": 30,
            "hvac_preference": "cool",
            "power_request": "hold",
            "priority": 5,
            "reason": "fallback",
            "source": ["room_state"],
        }

    result = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": int(time.time()),
        "version": "v2.5-intent",
        "intents": intents,
    }
    # Write to STATE_DIR for logger and other consumers
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "climate_intent.json"), "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError: pass
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
