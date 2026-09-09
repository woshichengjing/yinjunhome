#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空调自动化引擎 v2 — 以设定点、模式和风速控制为主。

自动启停仅用于连续节能待机，以及有人且不偏冷时恢复一台制冷设备。
自动模式只管理制冷/除湿；手动制热不接管。除湿机/新风控制不变。
"""
import json, os, sys, time, math, urllib.request
from datetime import datetime

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

STATE_DIR = "/tmp/hermes_states"
CONFIG_FILE = os.path.expanduser("~/.hermes/scripts/data/climate_config.json")
SNAP_FILE = os.path.expanduser("~/.hermes/scripts/data/decision_snapshots.jsonl")
SNAP_MAX_LINES = 3000
SNAP_MAX_BYTES = 3_000_000

HASS_URL = "http://10.90.1.19:8123"
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
if not HASS_TOKEN:
    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.isfile(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("HASS_TOKEN="):
                    HASS_TOKEN = line.strip().split("=", 1)[1].strip('"').strip("'")
                    break

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from state_machines.device_protection import submit_action, DEVICES

# ── 设备映射 ──（客餐厅已解耦，每台独立控制）
AC = {"br": ["br_ac"], "st": ["st_ac"], "lr": ["lr_ac"], "dr": ["dr_ac"], "nb": ["nb_ac"], "sb": ["sb_ac"]}
DEHUM_DEV = "br_dehum"
FRESH_DEV = "fresh_air"
DEHUM_ENTITY = "humidifier.aden_cn_965757218_derh40"
FRESH_SWITCH = "switch.giot_cn_2002953222_v82ksm_channel_4_p_3_1"

DEFAULTS = {
    "dry_run": False, "rooms": [],
    "setpoint_min": 16, "setpoint_max": 32, "step": 1,
    "energy_temp": 30, "energy_hysteresis": 0.5,
    "fresh_observe_min": 30, "dehum_temp_ceiling": 29,
    "co2_high": 1000, "co2_high_humid": 1200,
    "fresh_cool_delta": 2.0, "dry_tcool_buffer": 1.0, "dry_hysteresis": 0.5,
    "suite_bath": {},
}
INPUT_MAX_AGE_SECONDS = 180

_DRY = True


# ══════════════════════════════════════════════
# 辅助（保留原引擎的 helpers）
# ══════════════════════════════════════════════

def _load_json(name: str) -> dict:
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}

def _cfg() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            e = json.load(f).get("engine", {})
    except Exception:
        e = {}
    return {**DEFAULTS, **e}

def get_state(eid: str) -> str:
    if not eid: return ""
    req = urllib.request.Request(f"{HASS_URL}/api/states/{eid}",
                                 headers={"Authorization": f"Bearer {HASS_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return str(json.loads(resp.read()).get("state", ""))
    except Exception:
        return ""

def get_attr(eid: str, attr: str):
    req = urllib.request.Request(f"{HASS_URL}/api/states/{eid}",
                                 headers={"Authorization": f"Bearer {HASS_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("attributes", {}).get(attr, "")
    except Exception:
        return ""

def get_state_full(eid: str) -> dict:
    if not eid: return {}
    req = urllib.request.Request(f"{HASS_URL}/api/states/{eid}",
                                 headers={"Authorization": f"Bearer {HASS_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def _ah(t: float, rh: float) -> float:
    es = 6.112 * math.exp(17.67 * t / (t + 243.5))
    return round(2.1674 * (es * rh / 100) / (273.15 + t), 3)

def _act(dev_id: str, action: str, temp=None, mode=None, fan=None) -> str:
    """提交指令经 device_protection。DRY 模式只记录。"""
    desc_t = f"@{temp}" if temp is not None else ""
    desc_m = f"/{mode}" if mode else ""
    desc_f = f"·{fan}" if fan else ""
    desc = action + desc_t + desc_m + desc_f
    if _DRY:
        return desc
    sigfile = os.path.join(STATE_DIR, f"{dev_id}_engine_last")
    sig = json.dumps({"a": action, "t": temp, "m": mode, "f": fan}, sort_keys=True)
    try:
        if action != "off" and os.path.isfile(sigfile) and open(sigfile).read() == sig:
            return desc + "(dup)"
    except OSError:
        pass
    submit_action(dev_id, action, temp=temp, mode=mode, fan=fan, source="engine")
    # 记录引擎最后一次动作时间戳（区分手动/自动）
    try:
        with open(os.path.join(STATE_DIR, f"engine_{dev_id}_last_action"), "w") as f:
            f.write(str(int(time.time())))
    except OSError: pass
    try:
        with open(sigfile, "w") as f: f.write(sig)
    except OSError: pass
    return desc


def _is_fresh(payload: dict, max_age: int = INPUT_MAX_AGE_SECONDS) -> bool:
    """核心输入必须带生成时间，避免上游故障后继续使用旧决策。"""
    try:
        generated_at = payload.get("generated_at")
        if generated_at is None and payload.get("ts"):
            generated_at = datetime.strptime(payload["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
        age = int(time.time()) - int(generated_at or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    return 0 <= age <= max_age


def _refresh_ac_states(dev_ids: list, devs: dict) -> bool:
    """从 HA 实时校准 AC 开关和 last_changed；任一设备未知则返回 False。"""
    all_known = True
    for dev_id in dev_ids:
        entity = DEVICES.get(dev_id, {}).get("climate", "")
        full = get_state_full(entity) if entity else {}
        state = full.get("state", "") if full else ""
        if state in ("", "unknown", "unavailable"):
            devs.setdefault(dev_id, {})["switch"] = "unknown"
            all_known = False
            continue
        devs.setdefault(dev_id, {})["switch"] = "off" if state == "off" else "on"
        last_changed = full.get("last_changed", "")
        if last_changed:
            devs[dev_id]["last_changed"] = last_changed
    return all_known


def _handle_disabled_room(room: str, dev_ids: list, devs: dict) -> str:
    """禁用只关机一次，但必须等 HA 实时确认所有设备已关后才记完成。"""
    done_file = os.path.join(STATE_DIR, f"engine_{room}_disabled_done")
    if os.path.isfile(done_file):
        return "已禁用"
    if any(devs.get(dev_id, {}).get("switch") not in ("on", "off") for dev_id in dev_ids):
        return "禁用状态未知→待确认"
    on_devices = [dev_id for dev_id in dev_ids if devs[dev_id].get("switch") == "on"]
    if on_devices:
        for dev_id in on_devices:
            _act(dev_id, "off")
        return "禁用→关机待确认"
    try:
        with open(done_file, "w") as f:
            f.write("1")
    except OSError:
        return "禁用已关机→标记失败"
    return "已禁用"

def _fan_for(activity: str, cond: list, mode: str) -> str:
    if "过热" in cond: return "高风"
    return "低风"

def _write_snapshot(snap: dict):
    try:
        os.makedirs(os.path.dirname(SNAP_FILE), exist_ok=True)
        with open(SNAP_FILE, "a") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        if os.path.getsize(SNAP_FILE) > SNAP_MAX_BYTES:
            with open(SNAP_FILE) as f:
                lines = f.readlines()
            if len(lines) > SNAP_MAX_LINES:
                with open(SNAP_FILE, "w") as f:
                    f.writelines(lines[-SNAP_MAX_LINES:])
    except OSError:
        pass



def _compute_setpoint(comfort, at_comfort, energy_save, is_cool,
                      ac_cur, sense_temp, ref_sp, fresh_on,
                      setpoint_min, setpoint_max):
    """Calculate target setpoint. Pure function. Returns (new_sp, reason)."""
    new_sp = ref_sp
    reason = "维持"
    if fresh_on:
        new_sp = round(comfort)
        reason = "开机置舒适"
    elif ac_cur is None or sense_temp is None:
        reason = "缺温度→维持"
    elif sense_temp is not None and (at_comfort - 0.5) < sense_temp <= at_comfort:
        if ac_cur is not None and ac_cur > ref_sp:
            import math
            new_sp = int(math.ceil(ac_cur))
            reason = "停机卸载"
        else:
            reason = "节能保持" if energy_save else "已舒适"
    else:
        ideal = round(comfort + ac_cur - sense_temp)
        if is_cool:
            busy = (ac_cur > ref_sp) and (sense_temp > at_comfort + 0.3)
        else:
            busy = (ac_cur < ref_sp) and (sense_temp < at_comfort - 0.3)
        if busy:
            reason = "运行中等到位"
        elif abs(ideal - ref_sp) >= 1:
            if sense_temp is not None and sense_temp <= (at_comfort - 0.5) and ideal < ref_sp:
                new_sp = round(at_comfort - 0.5)
                reason = "校准回升"
            else:
                new_sp = ideal
                reason = "校准回升" if ideal > ref_sp else "校准压低"
        elif sense_temp is not None and sense_temp <= (at_comfort - 0.5) and new_sp <= ref_sp:
            new_sp = round(at_comfort - 0.5)
            reason = "校准回升"
    new_sp = max(setpoint_min, min(setpoint_max, new_sp))
    if new_sp == setpoint_min and new_sp < ref_sp:
        reason = "已达下限"
    # 舒适兜底：体感超标但公式未压低 → 强制压低 1°C
    if sense_temp is not None and sense_temp > at_comfort and new_sp >= ref_sp:
        new_sp = max(setpoint_min, ref_sp - 1)
        reason = "舒适强制压低"
    return new_sp, reason


def _determine_mode(c, cur_mode, rt, room_at, tmax, out_temp):
    """Determine target HVAC mode. Pure function. Returns zone_mode string."""
    target_mode = None
    _wet = ("过湿" in c)  # 仅过湿(>80%)触发除湿，偏湿(65-80%)不除湿
    _hot = ("偏热" in c or "过热" in c)
    if "过热" in c:
        target_mode = "cool"
    elif _hot and _wet:
        temp_over = max(0, rt - tmax) if rt is not None and tmax else 0
        hum_over  = max(0, room_at - rt) if room_at is not None and rt is not None else 0
        target_mode = "dry" if hum_over >= temp_over else "cool"
    elif _hot:
        target_mode = "cool"
    elif _wet:
        target_mode = "dry"
    if target_mode == "dry" and out_temp is not None and out_temp > 30:
        target_mode = "cool"
    if target_mode == "dry" and "舒适" in c:
        target_mode = "cool"
    zone_mode = target_mode or cur_mode
    if zone_mode == "dry" and "舒适" in c:
        zone_mode = "cool"
    return zone_mode



def _energy_save_for(r, act, c, activity_fn, cond_fn):
    """Determine if room should be in energy-saving mode.
    LR/DR use shared-space logic (5-area merge). Returns bool."""
    ACTIVE_STATES = {"occupied", "sleeping", "chaxi", "resting", "napping",
                     "using_computer", "watching_movie", "watching_tv"}
    if r in ("lr", "dr"):
        other_r = "dr" if r == "lr" else "lr"
        any_active = (act in ACTIVE_STATES or
                      activity_fn(other_r) in ACTIVE_STATES or
                      activity_fn("en") in ACTIVE_STATES or
                      activity_fn("cr") in ACTIVE_STATES or
                      activity_fn("kt") in ACTIVE_STATES)
        any_runaway = ("跑温" in c or "跑温" in cond_fn(other_r) or
                       "跑温" in cond_fn("en") or "跑温" in cond_fn("cr") or
                       "跑温" in cond_fn("kt"))
        return any_runaway or not any_active
    return ("跑温" in c) or (act not in ACTIVE_STATES)


def _energy_save_from_intent(intent: dict, fallback: bool) -> bool:
    """Use the intent layer as the primary comfort/energy decision source."""
    purpose = intent.get("purpose") if isinstance(intent, dict) else None
    if purpose == "energy":
        return True
    if purpose in ("comfort", "sleeping", "guest_mode"):
        return False
    return fallback


def _automatic_cooling_allowed(conditions: list) -> bool:
    """Never auto-start cooling while the room is already cold."""
    return "偏冷" not in conditions and "过冷" not in conditions



def _log_comfort_session(r, now_ts, energy_save, on_units, standby, is_comfortable):
    """Log comfort session to precool_log. Called only when AC is on and comfortable."""
    comfort_file = os.path.join(STATE_DIR, f"engine_{r}_comfort_since")
    if is_comfortable and not energy_save and on_units and not standby:
        if not os.path.isfile(comfort_file):
            with open(comfort_file, "w") as f: f.write(str(now_ts))
        else:
            try:
                since = int(open(comfort_file).read().strip())
                if (now_ts - since) >= 1800:
                    LOG_FILE = os.path.expanduser("~/.hermes/scripts/data/precool_log.jsonl")
                    with open(LOG_FILE, "a") as f:
                        f.write(json.dumps({"room": r, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
            except (ValueError, OSError):
                pass
            with open(comfort_file, "w") as f: f.write(str(now_ts))
    else:
        if os.path.isfile(comfort_file):
            os.remove(comfort_file)



def _dispatch_ac(r, on_units, devs, new_sp, zone_mode, reason,
                 standby, energy_save, act, c, comfort, now_ts, decisions, room_modes):
    """Dispatch AC commands and set room mode."""
    ts_f = os.path.join(STATE_DIR, f"engine_{r}_sp_ts")
    fan = _fan_for(act, c, zone_mode)
    acted = 0
    mode_acted = 0
    for d in on_units:
        d_mode = devs[d].get("climate_state", "")
        try: d_sp = int(float(devs[d].get("ac_set_temp", "")))
        except (ValueError, TypeError): d_sp = None
        need_m = (d_mode != zone_mode)
        need_t = (d_sp is not None and d_sp != new_sp)
        if need_m or need_t:
            _act(d, "on", temp=new_sp if need_t else None,
                 mode=zone_mode, fan=fan)
            acted += 1
            if need_m: mode_acted += 1
    if acted:
        try: open(ts_f, "w").write(str(now_ts))
        except OSError: pass
        parts = []
        if mode_acted: parts.append(f"→{zone_mode}模式(×{mode_acted})")
        if acted > mode_acted or mode_acted == 0:
            parts.append(f"→{new_sp}°C({reason})")
        decisions[r] = " + ".join(parts)
    else:
        decisions[r] = f"保持{new_sp}({reason})"
    if r == "br":
        try:
            with open("/tmp/br_standby.log", "w") as f:
                f.write(f"standby={standby} energy_save={energy_save} act={act} on_units={on_units} conf={comfort:.1f}")
        except: pass
    if standby: room_modes[r] = "待机"
    elif energy_save: room_modes[r] = "节能"
    else: room_modes[r] = "舒适"


# ══════════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════════

def run() -> dict:
    global _DRY
    cfg = _cfg()
    _DRY = bool(cfg["dry_run"])
    rooms = cfg["rooms"]

    # 加载待机调度表
    standby_delays = {}
    try:
        with open(os.path.join(STATE_DIR, "standby_schedule.json")) as f:
            sched = json.load(f)
        wd_today = sched.get("wd_today", None)
        for room, entries in sched.get("schedule", {}).items():
            standby_delays[room] = {}
            for e in entries:
                if e.get("is_workday") == wd_today:
                    standby_delays[room][e["hour"]] = e["delay_min"]
    except (IOError, json.JSONDecodeError):
        pass

    room_state_data = _load_json("room_state.json")
    env_data = _load_json("env_quality.json")
    room_state = room_state_data.get("rooms", {})
    env = env_data.get("rooms", {})
    stale_inputs = []
    if not _is_fresh(room_state_data):
        stale_inputs.append("room_state")
    if not _is_fresh(env_data):
        stale_inputs.append("env_quality")
    inputs_fresh = not stale_inputs
    intent_data = _load_json("climate_intent.json")
    try:
        intent_age = int(time.time()) - int(intent_data.get("generated_at", 0))
    except (TypeError, ValueError):
        intent_age = 10**9
    intents = intent_data.get("intents", {}) if 0 <= intent_age <= 180 else {}
    ext_data = _load_json("external_env.json")
    try:
        external_age = int(time.time()) - int(ext_data.get("generated_at", 0))
    except (TypeError, ValueError):
        external_age = 10**9
    ext = ext_data if 0 <= external_age <= 180 else {}
    devp = _load_json("device_protection.json").get("devices", {})
    soft_off = _load_json("device_soft_off.json") if os.path.isfile(os.path.join(STATE_DIR, "device_soft_off.json")) else {}

    out = ext.get("current", {})
    out_temp = out.get("temp")
    out_ah = out.get("abs_humidity")

    season_label = ext.get("season", "summer")

    def activity(r):
        a = room_state.get(r, {}).get("activity", "unknown")
        return (a[0] if a else "unknown") if isinstance(a, list) else a

    def cond(r):
        return env.get(r, {}).get("condition", [])

    def readings(r):
        return env.get(r, {}).get("readings", {})

    def room_temp(r):
        return readings(r).get("temp")

    def room_ah(r):
        rd = readings(r); t, h = rd.get("temp"), rd.get("hum")
        return _ah(t, h) if (t is not None and h is not None) else None

    # ═══ 新风（全屋，按 br/st 需求）═══
    fresh_state = get_state(FRESH_SWITCH)
    fresh_since_f = os.path.join(STATE_DIR, "engine_fresh_since")
    cool_rooms = []
    dehum_need_rooms = []
    co2_high = False
    fresh_environment_ok = False
    fresh_cool_ok = False
    fresh_dehum_ok = False
    hold_eligible = set()
    fresh_holding = set()
    fresh_target = None
    fresh_desc = "无"
    if not inputs_fresh:
        fresh_desc = "输入数据过期→不控"
    else:
        cool_rooms = [r for r in rooms if "偏热" in cond(r) or "过热" in cond(r)]
        dehum_need_rooms = [r for r in rooms if "偏湿" in cond(r) or "过湿" in cond(r)]
        indoor_ahs = [room_ah(r) for r in rooms if room_ah(r) is not None]
        outdoor_humid = (out_ah is not None and indoor_ahs and out_ah >= min(indoor_ahs))
        co2_thresh = cfg["co2_high_humid"] if outdoor_humid else cfg["co2_high"]
        co2_high = any((readings(r).get("co2") or 0) > co2_thresh for r in rooms)
        fresh_environment_ok = ext.get("fresh_eligible") is True
        fresh_cool_ok = fresh_environment_ok and bool(cool_rooms) and all(
            (room_temp(r) is not None and out_temp < room_temp(r) - cfg["fresh_cool_delta"]) for r in cool_rooms)
        fresh_dehum_ok = fresh_environment_ok and bool(dehum_need_rooms) and out_ah is not None and all(
            (room_ah(r) is not None and out_ah < room_ah(r)) for r in dehum_need_rooms)
        if fresh_cool_ok:
            hold_eligible |= set(cool_rooms)
        if fresh_dehum_ok:
            hold_eligible |= set(dehum_need_rooms)
        if co2_high or fresh_cool_ok or fresh_dehum_ok:
            fresh_target = "on"
            now = int(time.time())
            if fresh_state != "on":
                with open(fresh_since_f, "w") as f:
                    f.write(str(now))
                fresh_holding = hold_eligible
            else:
                try:
                    since = int(open(fresh_since_f).read().strip())
                except Exception:
                    since = 0
                if since and (now - since) < cfg["fresh_observe_min"] * 60:
                    fresh_holding = hold_eligible
        else:
            if fresh_state == "on":
                fresh_target = "off"
            try:
                os.remove(fresh_since_f)
            except OSError:
                pass

        soft_off = _load_json("device_soft_off.json") if os.path.isfile(os.path.join(STATE_DIR, "device_soft_off.json")) else {}
        if soft_off.get(FRESH_DEV, False):
            if fresh_state == "on":
                _act(FRESH_DEV, "off")
            fresh_desc = "软关跳过"
        elif fresh_target == "on":
            fresh_desc = _act(FRESH_DEV, "on")
        elif fresh_target == "off":
            fresh_desc = _act(FRESH_DEV, "off")

    # ═══ 每区空调：绝不开关机，只调模式+设定点+风速 ═══
    # 一区可含多台(客餐厅=客厅+餐厅)：同模式/设定/风速同步；只控开着的机，绝不自动开另一台(B版)
    decisions = {}
    room_modes = {}
    for r in rooms:
        # 每轮重新加载软关状态（面板可能刚操作）
        soft_off = _load_json("device_soft_off.json") if os.path.isfile(os.path.join(STATE_DIR, "device_soft_off.json")) else {}
        dev_ids = AC[r]
        devs = {dev_id: devp.get(dev_id, {}) for dev_id in dev_ids}
        live_devices_known = _refresh_ac_states(dev_ids, devs)

        # ═══ AC 禁用检查 ═══
        ac_disabled = _load_json("ac_disabled.json") if os.path.isfile(os.path.join(STATE_DIR, "ac_disabled.json")) else {}
        _disabled_done_f = os.path.join(STATE_DIR, f"engine_{r}_disabled_done")
        if ac_disabled.get(r, False):
            # 物理关机只执行一次；实时确认全关后才落完成标记，之后允许用户手动开启。
            decisions[r] = _handle_disabled_room(r, dev_ids, devs)
            room_modes[r] = "禁用"
            try: os.remove(os.path.join(STATE_DIR, f"engine_{r}_eco_since"))
            except OSError: pass
            continue
        else:
            # 解除禁用时清理标记
            try: os.remove(_disabled_done_f)
            except OSError: pass
        if not inputs_fresh:
            decisions[r] = "输入数据过期→不控"
            room_modes[r] = "未知"
            continue
        act = activity(r)
        intent = intents.get(r, {})

        c = cond(r)
        rt = room_temp(r)
        rd = readings(r)
        room_at = rd.get("at")      # 体感温度（环境质量已计算）
        th = env.get(r, {}).get("thresholds", {})
        at_max = th.get("at_max", 29)  # 体感舒适上限
        tmax = th.get("temp_max", 27)  # 裸温上限（降级用）

        # 套间卫生间有人 → 本房视为有人
        bath = cfg.get("suite_bath", {}).get(r)
        if bath and act in ("empty", "unknown") and activity(bath) in ("occupied", "entering"):
            act = "occupied"

        # 实时状态未知时 fail-safe，不根据上一轮设备快照下发动作。
        if not live_devices_known:
            decisions[r] = "设备状态未知→不控"
            room_modes[r] = "未知"
            continue
        # 收集开着的机；全关时根据需求决定是否开机
        on_units = [dev_id for dev_id in dev_ids if devs[dev_id].get("switch") == "on"]
        # 设备软关：排除出温控列表，不对它做任何操作
        _all_soft = all(soft_off.get(dev_id, False) for dev_id in dev_ids)
        _filtered = []
        for _did in on_units:
            if soft_off.get(_did, False):
                pass  # 软关设备：不控（用户可能手动在用）
            else:
                _filtered.append(_did)
        on_units = _filtered
        # 待客保护：夜间22-07客餐厅 AC 开着但状态机判无人 → 视为手动开
        if r in ("lr", "dr") and on_units and act in ("empty", "unknown"):
            _hour = datetime.now().hour
            if _hour >= 22 or _hour < 7:
                _guest_file = os.path.join(STATE_DIR, "guest_mode_today")
                try:
                    with open(_guest_file, "w") as f: f.write("1")
                except OSError: pass

        # 所有设备都已软关 → 跳过，不标任何房模
        if _all_soft:
            room_modes[r] = "软关"
            decisions[r] = "已软关→不控"
            continue
        if not on_units:
            # 判断是否需要开机            # 判断是否需要开机
            _e_save = _energy_save_from_intent(
                intent, _energy_save_for(r, act, c, activity, cond))
            need_on = not _e_save and _automatic_cooling_allowed(c)
            # 检测待机状态（即使 AC 关了也要维护 room_modes）
            _is_standby = False
            if _e_save:
                _sb_file = os.path.join(STATE_DIR, f"engine_{r}_eco_since")
                if os.path.isfile(_sb_file):
                    hour_now = datetime.now().hour
                    _delay = standby_delays.get(r, {}).get(hour_now, 30)
                    # 客餐厅夜间快速待机
                    _is_night2 = (hour_now >= 22 or hour_now < 6)
                    if r in ("lr", "dr") and _is_night2 and act in ("empty", "unknown"):
                        if not any(get_state(lid) == "on" for lid in [
                            "light.mijia_cn_group_1692857580902813696_group3_s_2_light",
                            "light.mijia_cn_group_1682436447321858048_group3_s_2_light",
                            "light.mijia_cn_group_1692854453868838912_group3_s_2_light",
                        ]):
                            _delay = 5
                    try:
                        _sb_ts = int(open(_sb_file).read().strip())
                        if (int(time.time()) - _sb_ts) >= _delay * 60:
                            _is_standby = True
                    except: pass
            if need_on:
                # 距离上次关机 < 30 分钟 → 不开机（防频繁开关 + 尊重手动关）
                _just_off = False
                try:
                    _lc = devs.get(dev_ids[0], {}).get("last_changed", "")
                    if _lc:
                        _lc_ts = datetime.fromisoformat(_lc).timestamp()
                        _just_off = (int(time.time()) - _lc_ts) < 30 * 60
                except Exception:
                    _just_off = False
                if _just_off:
                    decisions[r] = "关机未满30分钟→不控"
                    room_modes[r] = "待机"
                    continue
                # 开机：以 comfort 温度 + cool 模式启动
                on_unit = dev_ids[0]
                _act(on_unit, "on", temp=28, mode="cool")
                on_units = [on_unit]
                decisions[r] = "开机置舒适"
                # 补充 ref 缺失字段防止后续 try/except 跳过
                if not devs.get(on_unit, {}).get("ac_set_temp"):
                    devs[on_unit] = dict(devs.get(on_unit, {}), ac_set_temp="28", ac_cur_temp="28", climate_state="cool")
            else:
                decisions[r] = "待机→已关机" if _is_standby else "空调关→不控"
                room_modes[r] = "待机" if _is_standby else ("节能" if _e_save else "舒适")
                continue

        ref = devs[on_units[0]]              # 参考机(第一台在开的)
        cur_mode = ref.get("climate_state", "")
        if cur_mode == "heat":
            decisions[r] = "制热为手动模式→不控"
            room_modes[r] = "手动"
            continue

        energy_save = _energy_save_from_intent(
            intent, _energy_save_for(r, act, c, activity, cond))

        # ═══ 目标设定点计算（先算，模式+温度一轮下发）═══
        try:
            ref_sp = int(float(ref.get("ac_set_temp", "")))
        except (ValueError, TypeError):
            decisions[r] = "无设定点"
            continue
        try:
            ac_cur = float(ref.get("ac_cur_temp", ""))
        except (ValueError, TypeError):
            ac_cur = None

        # 舒适目标优先来自意图层；缺失时保留安全兜底。
        intent_target = intent.get("comfort_target") if isinstance(intent, dict) else None
        if isinstance(intent_target, (int, float)) and not isinstance(intent_target, bool):
            at_comfort = intent_target
        elif act == "sleeping" and not energy_save:
            _sleep_hour = datetime.now().hour
            at_comfort = 28.5 if 3 <= _sleep_hour < 10 else 27.5
        elif energy_save:
            at_comfort = cfg.get("energy_temp", 30)
        else:
            at_comfort = 27.5
        # 节能模式以裸温为基准，正常模式以体感为基准
        sense_temp = rt if energy_save else room_at
        comfort = max(20, at_comfort)  # 下限20°C
        now_ts = int(time.time())

        # 节能待机：动态延迟（基于历史习惯，无数据时默认30分钟）
        hour_now = datetime.now().hour
        standby_file = os.path.join(STATE_DIR, f"engine_{r}_eco_since")
        standby = False
        default_min = standby_delays.get(r, {}).get(hour_now, 30)
        # 客餐厅夜间快速待机：晚上灯全关 + 无人 → 5分钟直接待机
        _is_night = (hour_now >= 22 or hour_now < 6)
        if r in ("lr", "dr") and _is_night and act in ("empty", "unknown"):
            _lr_lights = [
                "light.mijia_cn_group_1692857580902813696_group3_s_2_light",  # 客厅全灯
                "light.mijia_cn_group_1682436447321858048_group3_s_2_light",  # 餐厅全灯
                "light.mijia_cn_group_1692854453868838912_group3_s_2_light",  # 客厅日常灯组
            ]
            _any_light_on = any(get_state(lid) == "on" for lid in _lr_lights)
            if not _any_light_on:
                default_min = 5
        # 预冷超时收尾：预冷启动后 45 分钟仍无人 → 待机关机
        _pc_file = os.path.join(STATE_DIR, f"engine_{r}_precool_start")
        _pc_active = os.path.isfile(_pc_file)
        if _pc_active and act in ("empty", "unknown"):
            try:
                _pc_ts = int(open(_pc_file).read().strip())
                _pc_elapsed = (now_ts - _pc_ts) // 60
                _pc_sched = _load_json("precool_schedule.json").get("schedule", {}).get(r, [])
                _pc_timeout = 45
                for _entry in _pc_sched:
                    if _entry.get("idle_timeout_min"):
                        _pc_timeout = _entry["idle_timeout_min"]
                        break
                if _pc_elapsed >= _pc_timeout:
                    standby = True
                    decisions[r] = f"预冷{_pc_elapsed}分钟未归→收尾待机"
                    try: os.remove(_pc_file)
                    except OSError: pass
            except: pass
        if not _pc_active:
            # 清除旧预冷标记
            pass
        # 有人回家：清理预冷标记，防止幽灵超时
        if _pc_active and act not in ("empty", "unknown"):
            try: os.remove(_pc_file)
            except OSError: pass
        if energy_save:
            if not os.path.isfile(standby_file):
                with open(standby_file, "w") as f: f.write(str(now_ts))
            elif (now_ts - int(open(standby_file).read().strip())) >= default_min * 60:
                standby = True
        else:
            try: os.remove(standby_file)
            except OSError: pass

        # 预冷：待机中命中规律窗口 → 退出待机
        if r == "br":
            try:
                with open("/tmp/br_standby.log", "w") as f:
                    f.write(f"standby={standby} energy_save={energy_save} act={act} on_units={on_units} conf={comfort:.1f}")
            except: pass
        if standby:
            try:
                with open(os.path.join(STATE_DIR, "precool_schedule.json")) as f:
                    sched = json.load(f)
                    precool = sched.get("schedule", {}).get(r, [])
                    wd_today = sched.get("wd_today", datetime.now().weekday() < 5)
                hour = datetime.now().hour
                minute = datetime.now().minute
                wd = wd_today
                for entry in precool:
                    if entry.get("is_workday") == wd and entry["hour"] == hour:
                        sm = entry.get("start_min", 0)
                        if sm <= minute < sm + 60:
                            standby = False
                            # AC 被待机关掉了 → 预冷需重开
                            if not on_units:
                                on_unit = dev_ids[0]
                                _act(on_unit, "on", temp=round(comfort), mode="cool")
                                on_units = [on_unit]
                            # 记录预冷开始时间（收尾计时用）
                            _pc_file = os.path.join(STATE_DIR, f"engine_{r}_precool_start")
                            with open(_pc_file, "w") as f: f.write(str(now_ts))
                            # 重置 eco_since 防止立刻再次待机
                            with open(standby_file, "w") as f: f.write(str(now_ts))
                            break
            except (IOError, json.JSONDecodeError):
                pass
        ts_f = os.path.join(STATE_DIR, f"engine_{r}_sp_ts")

        # 开机检测：AC 最近 2 分钟内才 on 才算刚开机（用 last_changed）
        fresh_on = False
        try:
            _lc = ref.get("last_changed", "")
            if _lc:
                _lc_ts = datetime.fromisoformat(_lc).timestamp()
                fresh_on = (int(time.time()) - _lc_ts) < 120
        except Exception:
            fresh_on = False

        # 区目标模式
        zone_mode = _determine_mode(c, cur_mode, rt, room_at, tmax, out_temp)

        is_cool = zone_mode in ("cool", "dry")

        if r == "br":
            try:
                with open("/tmp/br_standby.log", "w") as f:
                    f.write(f"standby={standby} energy_save={energy_save} act={act} on_units={on_units} conf={comfort:.1f}")
            except: pass
        if standby:
            for d in on_units:
                _act(d, "off")
            new_sp = ref_sp
            reason = "待机→关机"
        else:
            new_sp, reason = _compute_setpoint(
                comfort, at_comfort, energy_save, is_cool,
                ac_cur, sense_temp, ref_sp, fresh_on,
                cfg["setpoint_min"], cfg["setpoint_max"])
        # ═══ 同步下发（模式+温度合并一轮）═══
        if r in ("lr", "dr"):
            try:
                with open(f"/tmp/dbg_{r}_compute.log", "a") as f:
                    f.write(f"{datetime.now().strftime('%H:%M:%S')} comfort={comfort:.1f} at_c={at_comfort:.1f} sense={sense_temp} ac={ac_cur} ref={ref_sp} → new={new_sp} [{reason}]\n")
            except: pass
        _dispatch_ac(r, on_units, devs, new_sp, zone_mode, reason,
                     standby, energy_save, act, c, comfort, now_ts,
                     decisions, room_modes)

        # ── 舒适会话记录（预冷分析用）──
        _log_comfort_session(
            r, now_ts, energy_save, on_units, standby,
            env.get(r, {}).get("comfort") == "适宜")

    # ═══ 除湿机（主卫设备，br+bath 触发）═══
    dehum_rooms = ("br", "bath")
    # 主卫裸温≥30 不运行（除湿机物理在主卫，发热恶化环境）
    _dehum_ceiling = {"br": cfg["dehum_temp_ceiling"], "bath": 30}
    group_overheat = inputs_fresh and any("过热" in cond(r) for r in dehum_rooms)
    dehum_active = [r for r in dehum_rooms if inputs_fresh and (
        ("偏湿" in cond(r) or "过湿" in cond(r))
        and (room_temp(r) is not None and room_temp(r) < _dehum_ceiling[r])
        and "跑温" not in cond(r) and "过热" not in cond(r)
        and r not in fresh_holding
    )]
    group_need = len(dehum_active) > 0
    dehum_state = get_state(DEHUM_ENTITY)
    dehum_mode_cur = get_attr(DEHUM_ENTITY, "mode")
    # 设备软关
    # 除湿禁用
    soft_off = _load_json("device_soft_off.json") if os.path.isfile(os.path.join(STATE_DIR, "device_soft_off.json")) else {}
    if not inputs_fresh:
        dehum_desc = "输入数据过期→不控"
        dehum_mode = "未知"
    elif soft_off.get(DEHUM_DEV, False):
        if dehum_state == "on":
            _act(DEHUM_DEV, "off")
        dehum_desc = "软关跳过"
        dehum_mode = "软关"
    else:
        occ = any(activity(r) not in ("empty", "entering", "unknown") for r in dehum_active)
        dehum_mode = "轻音模式" if occ else "智能模式"
        if group_overheat:
            dehum_desc = (_act(DEHUM_DEV, "off") + "(过热)") if dehum_state == "on" else "保持关(过热)"
        elif group_need:
            if dehum_state != "on":          dehum_desc = _act(DEHUM_DEV, "on", mode=dehum_mode)
            elif dehum_mode_cur != dehum_mode: dehum_desc = _act(DEHUM_DEV, "set", mode=dehum_mode) + "(调模式)"
            else:                            dehum_desc = f"保持开/{dehum_mode}"
        else:
            dehum_desc = (_act(DEHUM_DEV, "off") + "(达标)") if dehum_state == "on" else "保持关"

    # ═══ 影子快照 ═══
    snap = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": _DRY, "season": season_label,
        "inputs": {"fresh": inputs_fresh, "stale": stale_inputs},
        "outdoor": {"temp": out_temp, "ah": out_ah},
        "fresh": {"state": fresh_state, "target": fresh_target, "decision": fresh_desc,
                  "environment_ok": fresh_environment_ok, "cool_ok": fresh_cool_ok,
                  "dehum_ok": fresh_dehum_ok, "co2_high": co2_high,
                  "holding": sorted(fresh_holding)},
        "rooms": {r: {
            "activity": activity(r), "cond": cond(r),
            "readings": readings(r),
            "intent": intents.get(r, {}),
            "decision": decisions.get(r, "无"),
        } for r in rooms},
        "dehum": {"state": dehum_state, "mode_cur": dehum_mode_cur, "mode_target": dehum_mode,
                  "group_need": group_need, "group_overheat": group_overheat,
                  "active_rooms": dehum_active, "decision": dehum_desc},
    }
    _write_snapshot(snap)

    # 输出房间模式（前后端统一）
    try:
        with open(os.path.join(STATE_DIR, "room_mode.json"), "w") as f:
            json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "modes": room_modes}, f, ensure_ascii=False, indent=2)
    except OSError: pass

    return snap


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
