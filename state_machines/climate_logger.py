#!/usr/bin/env python3
"""climate_logger.py — 决策审计日志 v2.4
仅在 HVAC 动作、房间状态、comfort、模式变化或保护拦截时写入 JSONL。
不记录普通 60s 循环（无变化则静默）。
"""
import json, os
from datetime import datetime

STATE_DIR = "/tmp/hermes_states"
LOG_FILE = os.path.join(STATE_DIR, "climate_decision.jsonl")
SNAPSHOT_FILE = os.path.join(STATE_DIR, "logger_snapshot.json")

os.environ["TZ"] = "Asia/Shanghai"
try: __import__("time").tzset()
except: pass


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _intent_summary(intent: dict) -> dict:
    """Extract key fields for logging."""
    return {
        "purpose": intent.get("purpose"),
        "comfort_target": intent.get("comfort_target"),
        "hvac_preference": intent.get("hvac_preference"),
        "power_request": intent.get("power_request"),
        "reason": intent.get("reason"),
    }


def _hashable(d: dict) -> str:
    """Stable string representation for comparison."""
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def run():
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    
    intent_data = _load(os.path.join(STATE_DIR, "climate_intent.json"))
    room_mode_data = _load(os.path.join(STATE_DIR, "room_mode.json"))
    device_data = _load(os.path.join(STATE_DIR, "device_protection.json"))
    env_data = _load(os.path.join(STATE_DIR, "env_quality.json"))
    room_state = _load(os.path.join(STATE_DIR, "room_state.json"))

    intents = intent_data.get("intents", {})
    modes = room_mode_data.get("modes", {})
    devices = device_data.get("devices", {})
    env_rooms = env_data.get("rooms", {})
    state_rooms = room_state.get("rooms", {})

    prev = _load(SNAPSHOT_FILE)
    prev_intents = prev.get("intents", {})
    prev_modes = prev.get("modes", {})

    logs = []

    for room in intents:
        cur_i = intents[room]
        prev_i = prev_intents.get(room, {})
        cur_m = modes.get(room, "")
        prev_m = prev_modes.get(room, "")
        es = env_rooms.get(room, {})
        rs = state_rooms.get(room, {})
        readings = es.get("readings", {})

        # Determine if anything changed
        intent_changed = _hashable(_intent_summary(cur_i)) != _hashable(_intent_summary(prev_i))
        mode_changed = cur_m != prev_m
        ac_id = {"br": "br_ac", "st": "st_ac", "lr": "lr_ac",
                  "dr": "dr_ac", "nb": "nb_ac", "sb": "sb_ac"}.get(room, f"{room}_ac")
        dev = devices.get(ac_id, {})

        if intent_changed or mode_changed:
            entry = {
                "time": now,
                "room": room,
                "state": {
                    "occupancy": rs.get("activity", "?"),
                },
                "environment": {
                    "temperature": readings.get("temp"),
                    "humidity": readings.get("hum"),
                    "apparent_temperature": readings.get("at"),
                },
                "intent": _intent_summary(cur_i),
                "mode": cur_m,
                "previous": {
                    "intent": _intent_summary(prev_i) if prev_i else None,
                    "mode": prev_m if prev_m else None,
                },
            }
            logs.append(entry)

    if logs:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            for entry in logs:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Save current snapshot for next comparison
    snap = {
        "ts": now,
        "intents": {r: _intent_summary(intents[r]) for r in intents},
        "modes": modes,
    }
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)

    return len(logs)


if __name__ == "__main__":
    n = run()
    print(f"Logged {n} decisions")
