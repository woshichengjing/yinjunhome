#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备保护状态机 v2 — 压缩机保护 + 运行计时 + 指令队列。

输出每个设备的：
  state:           off / running / protected
  restart_blocked: true/false + 剩余时间
  min_runtime:     运行中 + 已运行时间
  compressor_load: 带载中(制冷) / 已卸载(回风达标) / 已停止(AC关) / 未知
  next_action:     当前排队指令描述
  energy_optimal:  true/false
"""

import json, os, time
from datetime import datetime
import http.client

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
if not HASS_TOKEN:
    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.isfile(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("HASS_TOKEN="):
                    HASS_TOKEN = line.strip().split("=", 1)[1].strip('"').strip("'")
                    break

STATE_DIR = "/tmp/hermes_states"
os.makedirs(STATE_DIR, exist_ok=True)
NOW = int(time.time())

# ── HTTP helper, bypasses any urllib monkey-patching (state_provider) ──
def _ha_req(method: str, path: str, body: dict = None):
    """直连 HA API，不走 urllib（防止 state_provider 缓存旧 entity 数据）。"""
    conn = http.client.HTTPConnection("10.90.1.19", 8123, timeout=10)
    headers = {"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"}
    try:
        conn.request(method, path, body=json.dumps(body).encode() if body else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        return json.loads(data)
    except Exception:
        return {}
    finally:
        conn.close()


def get_state(entity_id: str) -> str:
    data = _ha_req("GET", f"/api/states/{entity_id}")
    return str(data.get("state", "")) if data else ""



def get_attrs(entity_id: str) -> dict:
    data = _ha_req("GET", f"/api/states/{entity_id}")
    return data.get("attributes", {}) if data else {}


def get_last_changed(entity_id: str) -> str:
    data = _ha_req("GET", f"/api/states/{entity_id}")
    return str(data.get("last_changed", "")) if data else ""


def ha_post(service: str, data: dict):
    """调 HA REST API 控制设备。"""
    try:
        return _ha_req("POST", f"/api/services/{service}", body=data)
    except Exception as e:
        print(f"ha_post({service}) failed: {e}", flush=True)
        return None


# ══════════════════════════════════════════════
# 指令队列
# ══════════════════════════════════════════════

def submit_action(dev_id: str, action: str, temp: int = None, mode: str = None, fan: str = None,
                  force: bool = False, source: str = "manual"):
    """外部脚本调用，提交指令到队列。最新指令覆盖旧指令。
    action: "on" | "off" | "set"
    temp:   目标温度（on/set 时使用）
    mode:   hvac 模式 cool/dry（on/set 时使用，默认 cool）
    fan:    风速 自动/低风/中风/高风（on/set 时使用，None=不改）
    force:  强制重发硬上电(无视proxy的on、绕过保护窗)，用于代理失真恢复
    """
    action_file = os.path.join(STATE_DIR, f"{dev_id}_action.json")
    data = {
        "action": action,
        "temp": temp,
        "mode": mode,
        "fan": fan,
        "force": force,
        "ts": int(time.time()),
        "source": source,
    }
    with open(action_file, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_action(dev_id: str) -> dict | None:
    """读取排队指令。"""
    action_file = os.path.join(STATE_DIR, f"{dev_id}_action.json")
    if not os.path.isfile(action_file):
        return None
    try:
        with open(action_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _clear_action(dev_id: str):
    """清除排队指令。"""
    action_file = os.path.join(STATE_DIR, f"{dev_id}_action.json")
    try:
        os.remove(action_file)
    except OSError:
        pass


def _action_desc(action: dict) -> str:
    """把指令翻译成人类可读描述。"""
    act = action.get("action", "?")
    temp = action.get("temp")
    mode = action.get("mode") or "cool"
    fan = action.get("fan")
    fansuf = f"·{fan}" if fan else ""
    if act == "on":
        return (f"开机 {temp}°C/{mode}" if temp else f"开机/{mode}") + fansuf
    elif act == "off":
        return "关机"
    elif act == "set":
        parts = []
        if temp:
            parts.append(f"{temp}°C")
        if action.get("mode"):
            parts.append(action["mode"])
        if fan:
            parts.append(fan)
        return "设置 " + "/".join(parts) if parts else "设置"
    return f"未知({act})"


def _read_dry_run() -> bool:
    """DRY_RUN 由 climate_config.json engine.dry_run 驱动(默认True安全)，随配置热更新、
    存活于 watchdog 重启。兼容: 环境变量 HERMES_DRY_RUN=0 强制关闭。"""
    if os.environ.get("HERMES_DRY_RUN") == "0":
        return False
    try:
        with open(os.path.expanduser("~/.hermes/scripts/data/climate_config.json")) as f:
            return bool(json.load(f).get("engine", {}).get("dry_run", True))
    except Exception:
        return True


DRY_RUN = _read_dry_run()  # 模块级初值；run() 每轮热读刷新


def _execute_action(dev_id: str, dev: dict, action: dict, protect: bool = True):
    """执行排队指令。DRY_RUN 模式下只记录不执行。
    protect=True 才打保护戳(开关机/换模式)；纯调温/风速 protect=False 不占保护窗。"""
    act = action["action"]
    climate = dev["climate"]

    if DRY_RUN:
        print(f"[DRY_RUN] {dev_id}: {_action_desc(action)} → 跳过执行")
    elif act == "on":
        temp = action.get("temp")
        mode = action.get("mode") or "cool"
        fan = action.get("fan")
        # scdvb VRV: set_hvac_mode 触发开机，set_temperature 设温
        ha_post("climate/set_hvac_mode", {"entity_id": climate, "hvac_mode": mode})
        time.sleep(0.5)
        if temp is not None:
            ha_post("climate/set_temperature", {"entity_id": climate, "temperature": temp})
        if fan:
            ha_post("climate/set_fan_mode", {"entity_id": climate, "fan_mode": fan})
        if protect:
            _stamp_exec(dev_id)
        _clear_action(dev_id)

    elif act == "off":
        if not DRY_RUN:
            ha_post("climate/turn_off", {"entity_id": climate})
        _record_off(dev_id)
        if protect:
            _stamp_exec(dev_id)
        _clear_action(dev_id)

    elif act == "set":
        temp = action.get("temp")
        mode = action.get("mode")
        fan = action.get("fan")
        if temp:
            ha_post("climate/set_temperature", {"entity_id": climate, "temperature": temp})
        if mode:
            ha_post("climate/set_hvac_mode", {"entity_id": climate, "hvac_mode": mode})
        if fan:
            if mode:
                time.sleep(1)
            ha_post("climate/set_fan_mode", {"entity_id": climate, "fan_mode": fan})
        if protect:
            _stamp_exec(dev_id)
        _clear_action(dev_id)


def _record_on(dev_id: str):
    tag = dev_id.split("_")[0]
    with open(os.path.join(STATE_DIR, f"{tag}_on_time"), "w") as f:
        f.write(str(int(time.time())))


def _record_off(dev_id: str):
    tag = dev_id.split("_")[0]
    with open(os.path.join(STATE_DIR, f"{tag}_lastoff"), "w") as f:
        f.write(str(int(time.time())))
    try:
        os.remove(os.path.join(STATE_DIR, f"{tag}_on_time"))
    except OSError:
        pass


def _stamp_exec(dev_id: str):
    """指令实际执行后打时间戳 → 开启统一保护窗(每次执行都重置)。"""
    with open(os.path.join(STATE_DIR, f"{dev_id}_lastexec"), "w") as f:
        f.write(str(int(time.time())))


def _get_last_updated(entity_id: str) -> str:
    """获取 entity 的 last_updated 字段，用于验证命令是否送达。"""
    data = _ha_req("GET", f"/api/states/{entity_id}")
    return str(data.get("last_updated", "")) if data else ""



def _protection_min() -> int:
    try:
        with open(os.path.expanduser("~/.hermes/scripts/data/climate_config.json")) as f:
            return int(json.load(f).get("general", {}).get("protection_min", 15))
    except Exception:
        return 15


def _protection(dev_id: str):
    """统一保护：距上次指令执行 < protection_min(默认15)min 则保护中。
    返回 (blocked, remaining_min, ends_at_epoch)。"""
    pmin = _protection_min()
    f = os.path.join(STATE_DIR, f"{dev_id}_lastexec")
    if os.path.isfile(f):
        try:
            last = int(open(f).read().strip())
            elapsed = (int(time.time()) - last) // 60   # 用实时时间，不用冻结的模块级 NOW
            if elapsed < pmin:
                return True, pmin - elapsed, last + pmin * 60
        except (ValueError, OSError):
            pass
    return False, 0, 0


# ══════════════════════════════════════════════
# 设备定义
# ══════════════════════════════════════════════

DEVICES = {
    "br_ac": {"type": "ac", "climate": "climate.scdvb_cn_2002962758_t001"},
    "lr_ac": {"type": "ac", "climate": "climate.scdvb_cn_2002193115_t001"},
    "dr_ac": {"type": "ac", "climate": "climate.scdvb_cn_2002484870_t001"},
    "st_ac": {"type": "ac", "climate": "climate.scdvb_cn_2002146955_t001"},
    "nb_ac": {"type": "ac", "climate": "climate.scdvb_cn_2002118217_t001"},
    "sb_ac": {"type": "ac", "climate": "climate.scdvb_cn_2002127023_t001"},
    "br_dehum":    {"type": "humidifier", "entity": "humidifier.aden_cn_965757218_derh40"},
    "laundry_dehum": {"type": "humidifier", "entity": "humidifier.xiaomi_cn_742311152_13l"},
    "gb_dehum":    {"type": "humidifier", "entity": "humidifier.aden_cn_747834793_derh16"},
    "fresh_air":   {"type": "fresh", "switch": "switch.giot_cn_2002953222_v82ksm_channel_4_p_3_1"},
    "kitchen_exhaust": {"type": "fresh", "switch": "switch.giot_cn_1126328471_v8icm_on_p_2_1"},
    "gb_exhaust":      {"type": "fresh", "switch": "climate.yeelink_cn_647044298_v6"},
}

PEAK_HOURS = (8, 22)


def load_config():
    try:
        with open(os.path.expanduser("~/.hermes/scripts/data/climate_config.json")) as f:
            return json.load(f).get("general", {})
    except Exception:
        return {}


# ══════════════════════════════════════════════
# 评估逻辑
# ══════════════════════════════════════════════

def evaluate_device(dev_id: str, dev: dict) -> dict:
    dev_type = dev.get("type", "ac")

    # 除湿机
    if dev_type == "humidifier":
        entity = dev["entity"]
        state = get_state(entity)
        attrs = get_attrs(entity)
        result = {
            "device": dev_id, "type": "humidifier",
            "switch": state, "compressor_load": "n/a",
            "dehum_set_humidity": str(attrs.get("humidity", "")),
            "dehum_cur_humidity": str(attrs.get("current_humidity", "")),
            "restart_blocked": False, "restart_remaining_min": 0,
            "min_runtime_active": False, "runtime_min": 0, "runtime_remaining_min": 0,
            "state": "running" if state == "on" else "off",
            "energy_optimal": True, "energy_note": "运行中" if state == "on" else "待机",
            "next_action": "无", "protection_ends_at": 0,
        }
        result["last_changed"] = get_last_changed(entity)
        blocked, remaining, ends_at = _protection(dev_id)
        result["restart_blocked"] = blocked
        result["restart_remaining_min"] = remaining
        if blocked:
            result["protection_ends_at"] = ends_at
        # 除湿机指令队列（on/off + 模式；统一15min保护，执行即重置）
        action = _load_action(dev_id)
        if action:
            action_desc = _action_desc(action)
            if blocked:
                result["next_action"] = f"等待执行: {action_desc}"
            elif DRY_RUN:
                _clear_action(dev_id)
                result["next_action"] = f"[DRY_RUN] 已跳过: {action_desc}"
            else:
                act = action["action"]
                mode = action.get("mode")
                if act == "on":
                    if state == "off":
                        ha_post("humidifier/turn_on", {"entity_id": entity})
                    if mode:
                        ha_post("humidifier/set_mode", {"entity_id": entity, "mode": mode})
                elif act == "off" and state == "on":
                    ha_post("humidifier/turn_off", {"entity_id": entity})
                elif act == "set" and mode:
                    ha_post("humidifier/set_mode", {"entity_id": entity, "mode": mode})
                _stamp_exec(dev_id)   # 执行后开保护窗
                _clear_action(dev_id)
                b2, r2, e2 = _protection(dev_id)
                result["restart_blocked"], result["restart_remaining_min"], result["protection_ends_at"] = b2, r2, e2
                result["next_action"] = f"已执行: {action_desc}"
        elif blocked:
            result["next_action"] = "保护中"
        return result

    # 新风 — 无压缩机保护，简单 on/off 队列（DRY_RUN 保护）
    if dev_type == "fresh":
        sw = get_state(dev["switch"])
        result = {
            "device": dev_id, "type": "fresh",
            "switch": sw, "compressor_load": "n/a",
            "restart_blocked": False, "restart_remaining_min": 0,
            "min_runtime_active": False, "runtime_min": 0, "runtime_remaining_min": 0,
            "state": "running" if sw == "on" else "off",
            "energy_optimal": True, "energy_note": "运行中" if sw == "on" else "待机",
            "next_action": "无", "protection_ends_at": 0,
            "last_changed": get_last_changed(dev["switch"]),
        }
        action = _load_action(dev_id)
        if action:
            action_desc = _action_desc(action)
            if DRY_RUN:
                _clear_action(dev_id)
                result["next_action"] = f"[DRY_RUN] 已跳过: {action_desc}"
            else:
                if action["action"] == "on" and sw == "off":
                    ha_post("switch/turn_on", {"entity_id": dev["switch"]})
                elif action["action"] == "off" and sw == "on":
                    ha_post("switch/turn_off", {"entity_id": dev["switch"]})
                _clear_action(dev_id)
                result["next_action"] = f"已执行: {action_desc}"
        return result

    # 空调 — 完整保护 + 指令队列
    general = load_config()
    tag = dev_id.split("_")[0]

    climate_state = get_state(dev["climate"])
    sw = "on" if climate_state != "off" else "off"
    attrs = get_attrs(dev["climate"])
    ac_cur = str(attrs.get("current_temperature", ""))
    ac_set = str(attrs.get("temperature", ""))

    result = {
        "device": dev_id,
        "switch": sw,
        "climate_state": climate_state,
        "ac_cur_temp": ac_cur,
        "ac_set_temp": ac_set,
    }

    # 压缩机负载
    if sw != "on":
        result["compressor_load"] = "已停止"
    else:
        try:
            if ac_cur and ac_set and ac_cur not in ("unknown", "unavailable"):
                result["compressor_load"] = "带载中" if float(ac_cur) > float(ac_set) else "已卸载"
            else:
                result["compressor_load"] = "未知"
        except (ValueError, TypeError):
            result["compressor_load"] = "未知"

    # 统一保护：距上次指令执行 < protection_min(默认15)min 则保护中，执行即重置
    blocked, remaining, ends_at = _protection(dev_id)
    result["restart_blocked"] = blocked
    result["restart_remaining_min"] = remaining
    result["min_runtime_active"] = False
    result["runtime_min"] = 0
    result["runtime_remaining_min"] = 0

    # 运行状态
    if sw == "on":
        result["state"] = "running"
    elif blocked:
        result["state"] = "protected"
    else:
        result["state"] = "off"

    # 能耗
    hour = datetime.now().hour
    is_peak = PEAK_HOURS[0] <= hour < PEAK_HOURS[1]
    if sw == "on" and is_peak:
        result["energy_optimal"] = False
        result["energy_note"] = "峰电运行"
    elif sw == "on":
        result["energy_optimal"] = True
        result["energy_note"] = "谷电运行"
    else:
        result["energy_optimal"] = True
        result["energy_note"] = "未运行"

    # ── 指令队列 ──
    # 保护仅约束「开关机 / 换模式」(压缩机启停)；纯调温/调风速自由执行，不受保护窗、不重置窗
    protection_ends_at = ends_at if blocked else 0
    next_action = "无"

    action = _load_action(dev_id)
    if action:
        action_desc = _action_desc(action)
        act = action.get("action")
        req_mode = action.get("mode")
        force = bool(action.get("force"))
        cur_mode = result.get("climate_state", "")
        if act == "off":
            protect_op = True                                                      # 关机 → 走保护
        elif act == "on":
            protect_op = (sw != "on") or (bool(req_mode) and req_mode != cur_mode)  # 真开机 或 换模式
        else:  # set
            protect_op = bool(req_mode) and req_mode != cur_mode                    # 仅含换模式才走保护

        if protect_op and blocked and not force:                                   # force(失真恢复)绕过保护窗
            next_action = f"等待执行: {action_desc}"
            protection_ends_at = ends_at
        else:
            _execute_action(dev_id, dev, action, protect=(protect_op and not force))  # force不打戳(恢复动作)
            if DRY_RUN:
                next_action = "[DRY_RUN] 已跳过: " + action_desc
            elif force:
                next_action = "⚠️强制重发(代理失真恢复): " + action_desc
            elif protect_op:
                next_action = "已执行: " + action_desc
                b2, r2, e2 = _protection(dev_id)
                result["restart_blocked"], result["restart_remaining_min"] = b2, r2
                protection_ends_at = e2
            else:
                next_action = "已调温: " + action_desc   # 自由调温/风速，不占保护窗
    elif blocked:
        next_action = "保护中"
        protection_ends_at = ends_at

    result["next_action"] = next_action
    result["protection_ends_at"] = protection_ends_at
    result["last_changed"] = get_last_changed(dev["climate"])
    return result


def run() -> dict:
    global DRY_RUN, NOW
    NOW = int(time.time())      # 每轮刷新，daemon 长驻不冻结
    DRY_RUN = _read_dry_run()   # 每轮热读，随 climate_config.json 热更新、存活于 watchdog 重启
    result = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "devices": {dev_id: evaluate_device(dev_id, dev) for dev_id, dev in DEVICES.items()}
    }
    with open(os.path.join(STATE_DIR, "device_protection.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
