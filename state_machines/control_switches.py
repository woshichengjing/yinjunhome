"""自动控制开关：只信任控制器实际使用的文件，未知状态禁止自动开启。"""
import json
import os
import time


def _flag(state_dir, filename, key):
    try:
        with open(os.path.join(state_dir, filename), encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            return None
        value = data.get(key, False)  # 兼容历史稀疏字典；缺键表示未禁用。
        return value if isinstance(value, bool) else None
    except (OSError, ValueError):
        return None


def read_control_switches(state_dir, device):
    room_disabled = _flag(state_dir, "ac_disabled.json", device[:-3]) if device.endswith("_ac") else False
    soft_off = _flag(state_dir, "device_soft_off.json", device)
    reason = ("room_disabled" if room_disabled is True else
              "device_soft_off" if soft_off is True else
              "switch_state_unknown" if room_disabled is None or soft_off is None else "")
    return {"room_disabled": room_disabled, "device_soft_off": soft_off,
            "automation_allowed": not reason, "blocked_reason": reason,
            "observed_at": int(time.time())}


def automatic_action_blocked(state_dir, device, action):
    if action.get("source") != "engine" or action.get("action") == "off":
        return False
    return not read_control_switches(state_dir, device)["automation_allowed"]
