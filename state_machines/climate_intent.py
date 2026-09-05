#!/usr/bin/env python3
"""climate_intent.py — 温控意图层 v2.4
输入 room_state + env_quality → 输出每个房间的温控意图（要什么，不是怎么做）。
"""
import json, os
from datetime import datetime

STATE_DIR = "/tmp/hermes_states"

os.environ["TZ"] = "Asia/Shanghai"
try: __import__("time").tzset()
except: pass


def _load(name: str) -> dict:
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def run() -> dict:
    """Generate climate intent for all controlled rooms. Returns {ts, intents: {room: {...}}}."""
    room_state = _load("room_state.json").get("rooms", {})
    env = _load("env_quality.json").get("rooms", {})
    soft_off = _load("device_soft_off.json")
    ac_disabled = _load("ac_disabled.json")

    hour = datetime.now().hour
    intents = {}

    for room in ("br", "st", "lr", "dr", "nb", "sb"):
        rs = room_state.get(room, {})
        es = env.get(room, {})

        activity = rs.get("activity", "unknown")
        readings = es.get("readings", {})
        conditions = es.get("condition", [])
        thresholds = es.get("thresholds", {})

        at_val = readings.get("at")
        temp_val = readings.get("temp")
        rh_val = readings.get("hum")

        # ── P0: 设备安全 ──
        # 禁用
        if ac_disabled.get(room, False):
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
        ac_ids = {"br": "br_ac", "st": "st_ac", "lr": "lr_ac",
                   "dr": "dr_ac", "nb": "nb_ac", "sb": "sb_ac"}
        ac_id = ac_ids.get(room, f"{room}_ac")
        if soft_off.get(ac_id, False):
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

        # ── P1: 用户手动控制 ──
        manual_off = os.path.isfile(os.path.join(STATE_DIR, f"engine_{room}_manual_off"))
        manual_on = os.path.isfile(os.path.join(STATE_DIR, f"engine_{room}_manual_on"))

        if manual_off:
            intents[room] = {
                "occupancy": activity,
                "purpose": "manual_off",
                "comfort_target": None,
                "hvac_preference": "off",
                "power_request": "off",
                "priority": 1,
                "reason": "manual_off",
                "source": ["climate_engine"],
            }
            continue

        if manual_on:
            intents[room] = {
                "occupancy": "occupied",  # forced
                "purpose": "manual_on",
                "comfort_target": 27.5,
                "hvac_preference": "cool",
                "power_request": "on",
                "priority": 1,
                "reason": "manual_on",
                "source": ["climate_engine"],
            }
            continue

        # ── P2: 房间保护 ──
        # Guest mode (night-time lr/dr manual on)
        if room in ("lr", "dr"):
            guest_file = os.path.join(STATE_DIR, "guest_mode_today")
            if os.path.isfile(guest_file) and (hour >= 22 or hour < 7):
                intents[room] = {
                    "occupancy": "occupied",
                    "purpose": "guest_mode",
                    "comfort_target": 27.5,
                    "hvac_preference": "cool",
                    "power_request": "on",
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
                "power_request": "on",
                "priority": 3,
                "reason": reason,
                "source": ["room_state"],
            }
            continue

        if is_occupied:
            # ── P4: 温控策略 ──
            # Check for runaway (window open)
            has_runaway = any("跑温" in c for c in conditions)
            if has_runaway:
                intents[room] = {
                    "occupancy": activity,
                    "purpose": "energy",
                    "comfort_target": 30,
                    "hvac_preference": "cool",
                    "power_request": "on",
                    "priority": 4,
                    "reason": "occupied_runaway",
                    "source": ["env_quality"],
                }
                continue

            # Normal occupied → comfort
            intents[room] = {
                "occupancy": activity,
                "purpose": "comfort",
                "comfort_target": 27.5,
                "hvac_preference": "cool",
                "power_request": "on",
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
                        "power_request": "on",
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
                "power_request": "on",
                "priority": 4,
                "reason": "energy_empty",
                "source": ["room_state"],
            }
            continue

        # Fallback
        intents[room] = {
            "occupancy": activity,
            "purpose": "comfort",
            "comfort_target": 27.5,
            "hvac_preference": "cool",
            "power_request": "on",
            "priority": 5,
            "reason": "fallback",
            "source": ["room_state"],
        }

    result = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v2.4-intent",
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
