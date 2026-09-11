#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环境质量状态机 v3 — 房间状态驱动，输出单一环境状态。

状态：舒适 / 偏热 / 过热 / 偏冷 / 过冷 / 偏干 / 过干 / 偏湿 / 过湿 / 空气污浊 / 空闲
阈值根据房间活动动态调整（睡觉更严格，观影/洗浴跳过部分检测）。

房间标签：br=主卧 st=书房 lr=客厅 dr=餐厅 sb=南次卧 nb=北次卧 gb=客卫 bath=主卫 en=玄关
"""

import json, os, time, urllib.request
from datetime import datetime

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

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

STATE_DIR = "/tmp/hermes_states"
os.makedirs(STATE_DIR, exist_ok=True)

ALL_STATES = ["舒适", "偏热", "过热", "偏冷", "过冷", "偏干", "过干", "偏湿", "过湿", "空气污浊", "跑温", "不适宜"]


# 门窗传感器（跑温判断，仅主卧(br)/书房(st)）
DOOR_WINDOW = {
    "br": {
        "door":   "binary_sensor.isa_cn_blt_3_1lnimpgbs4c00_dw2hl_contact_state_p_2_2",
        "window": "binary_sensor.zhu_wo_zhu_wo_chuang_chuan_gan_qi_men",
    },
    "st": {
        "door":   "binary_sensor.isa_cn_blt_3_144u9efpg5c01_dw2hl_contact_state_p_2_2",
        "window": "binary_sensor.shu_fang_shu_fang_chuang_chuan_gan_qi_men",
    },
    "lr": {
        # 客餐厅：客厅左/右窗 + 餐厅窗，任一连续开≥10min → 跑温（无门，连通主卧走 DOOR_PATHS 待补）
        "window": [
            "binary_sensor.ke_ting_ke_ting_you_chuang_chuan_gan_qi_men",
            "binary_sensor.ke_ting_ke_ting_zuo_chuang_chuan_gan_qi_men",
            "binary_sensor.can_ting_can_ting_chuang_chuan_gan_qi_men",
        ],
    },
}
LR_TEMP_EID = "sensor.ke_ting_kong_qi_jian_ce_yi_temperature"
OUTDOOR_TEMP_EID = "sensor.lumi_cn_lumi_158d000116b3a8_v1_temperature_p_2_1"
TEMP_LOSS_DELTA = 3.0

# 房间空调开关（跑温门开判断用）
AC_SWITCH = {
    "br": "climate.scdvb_cn_2002962758_t001",  # 主卧
    "st": "climate.scdvb_cn_2002146955_t001",  # 书房
}
# 房门联通通路：每个房间列出所有会导致它跑温的「门→联通房间」通路，任一满足即跑温
# 书房门联通主卧，故主卧要同时看自己的门(→客餐厅)和书房的门(→书房)
# 每条通路：door 门传感器 + room 联通房名 + ac_sw 联通房空调(任一开=有空调) + temp 联通房温度
DOOR_PATHS = {
    "br": [
        # 主卧门 → 客餐厅（客厅00002/餐厅00003任一开=联通房有空调）
        {"door": "binary_sensor.isa_cn_blt_3_1lnimpgbs4c00_dw2hl_contact_state_p_2_2",
         "room": "客餐厅",
         "ac_sw": ["climate.scdvb_cn_2002193115_t001",
                   "climate.scdvb_cn_2002484870_t001"],
         "temp": "sensor.ke_ting_kong_qi_jian_ce_yi_temperature"},
        # 书房门 → 书房（主卧冷气流向书房）：仅当书房门开【且书房窗也开】才算跑温
        # （只开门、窗关着，冷气只是在室内重分布未流失外界，不算跑温）
        {"door": "binary_sensor.isa_cn_blt_3_144u9efpg5c01_dw2hl_contact_state_p_2_2",
         "also_window": "binary_sensor.shu_fang_shu_fang_chuang_chuan_gan_qi_men",
         "room": "书房",
         "ac_sw": ["climate.scdvb_cn_2002146955_t001"],
         "temp": "sensor.miaomiaoc_cn_blt_3_1bbe5qecclc00_t6_temperature_p_3_1001"},
    ],
    "st": [
        # 书房门 → 主卧：书房门开【且主卧窗也开】才算跑温（窗不开=冷气只是室内重分布）
        {"door": "binary_sensor.isa_cn_blt_3_144u9efpg5c01_dw2hl_contact_state_p_2_2",
         "also_window": "binary_sensor.zhu_wo_zhu_wo_chuang_chuan_gan_qi_men",
         "room": "主卧",
         "ac_sw": ["climate.scdvb_cn_2002962758_t001"],
         "temp": "sensor.zhu_wo_kong_qi_jian_ce_yi_temperature"},
    ],
}


def _get_attr(entity_id: str, attr: str) -> str:
    """获取 HA entity 的指定 attribute。"""
    url = f"{HASS_URL}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HASS_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            val = data.get("attributes", {}).get(attr, "")
            return str(val) if val else ""
    except Exception:
        return ""


def get_state(entity_id: str) -> str:
    url = f"{HASS_URL}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HASS_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return str(json.loads(resp.read()).get("state", ""))
    except Exception:
        return ""


RUNAWAY_DEBOUNCE_SEC = 600  # 门窗连续开≥10min才算有效开启（跑温防抖，防短暂开关门窗误判）


def _open_long(entity_id: str, min_sec: int = RUNAWAY_DEBOUNCE_SEC) -> bool:
    """门窗连续开启 ≥ min_sec 才返回 True。三态：on计时 / off清零 / 异常值保持不变。"""
    if not entity_id:
        return False
    st = get_state(entity_id)
    tsfile = os.path.join(STATE_DIR, f"runaway_open_{entity_id}")
    now = int(time.time())
    if st == "on":
        if not os.path.isfile(tsfile):
            with open(tsfile, "w") as f:
                f.write(str(now))
            return False  # 刚开始开，未到防抖
        try:
            with open(tsfile) as f:
                return (now - int(f.read().strip())) >= min_sec
        except (ValueError, OSError):
            return False
    if st == "off":
        try:
            os.remove(tsfile)
        except OSError:
            pass
    return False  # off 或异常值 → 未确认长开


ROOM_SENSORS = {
    "br": {
        "temp": "sensor.zhu_wo_kong_qi_jian_ce_yi_temperature",
        "hum":  "sensor.zhu_wo_kong_qi_jian_ce_yi_humidity",
        "co2":  "sensor.zhu_wo_kong_qi_jian_ce_yi_co2",
    },
    "st": {
        "temp": "sensor.miaomiaoc_cn_blt_3_1bbe5qecclc00_t6_temperature_p_3_1001",
        "hum":  "sensor.miaomiaoc_cn_blt_3_1bbe5qecclc00_t6_relative_humidity_p_3_1008",
    },
    "lr": {
        "temp": "sensor.ke_ting_kong_qi_jian_ce_yi_temperature",
        "hum":  "sensor.ke_ting_kong_qi_jian_ce_yi_humidity",
        "co2":  "sensor.ke_ting_kong_qi_jian_ce_yi_co2",
    },
    "dr": {
        "temp": "sensor.miaomiaoc_cn_blt_3_1bcgc3ololg00_t6_temperature_p_3_1001",
        "hum":  "sensor.miaomiaoc_cn_blt_3_1bcgc3ololg00_t6_relative_humidity_p_3_1008",
    },
    "sb": {
        "temp": "sensor.zhu_wo_kong_qi_jian_ce_yi_temperature_2",
        "hum":  "sensor.zhu_wo_kong_qi_jian_ce_yi_humidity_2",
        "co2":  "sensor.zhu_wo_kong_qi_jian_ce_yi_co2_2",
    },
    "gb": {
        "temp": "sensor.miaomiaoc_cn_blt_3_1co5da0145k00_t6_temperature_p_3_1001",
        "hum":  "sensor.miaomiaoc_cn_blt_3_1co5da0145k00_t6_relative_humidity_p_3_1008",
    },
    "bath": {
        "temp": "sensor.miaomiaoc_cn_blt_3_1p4nq2vr44001_t1_temperature_p_2_1",
        "hum":  "sensor.miaomiaoc_cn_blt_3_1p4nq2vr44001_t1_relative_humidity_p_2_2",
    },
    "nb": {
        "temp": "sensor.bei_ci_wo_kong_qi_jian_ce_yi_temperature",
        "hum":  "sensor.bei_ci_wo_kong_qi_jian_ce_yi_humidity",
        "co2":  "sensor.bei_ci_wo_kong_qi_jian_ce_yi_co2",
    },
}

# 传感器降级：主传感器 unavailable 时用 AC 回风温度顶替
TEMP_FALLBACK = {
    "br": "climate.scdvb_cn_2002962758_t001",
    "lr": "climate.scdvb_cn_2002193115_t001",
    "dr": "climate.scdvb_cn_2002484870_t001",
    "st": "climate.scdvb_cn_2002146955_t001",
    "nb": "climate.scdvb_cn_2002118217_t001",
    "sb": "climate.scdvb_cn_2002127023_t001",
}

TEMP_COLD, TEMP_COOL, TEMP_HOT = 22, 24, 29
HUM_DRY, HUM_COOL, HUM_HIGH, HUM_WET = 30, 40, 65, 80
CO2_POOR = 1200

# 房间 → AC climate entity（已废弃风冷修正，仅保留条目）
ROOM_AC = {
    "br": "climate.scdvb_cn_2002962758_t001",
    "lr": "climate.scdvb_cn_2002193115_t001",
    "dr": "climate.scdvb_cn_2002484870_t001",
    "st": "climate.scdvb_cn_2002146955_t001",
    "nb": "climate.scdvb_cn_2002118217_t001",
    "sb": "climate.scdvb_cn_2002127023_t001",
}

# 体感温度舒适阈值（杭州四季）
# 春/秋过渡季穿适中，夏穿少，冬穿多
SEASON_CONFIG = {
    "spring": {  # 3-5月
        "at_min": 20, "at_max": 26,
        "tcold": 20, "tcool": 22, "thot": 28,
        "temp_max_normal": 26,
        "label": "春",
    },
    "summer": {  # 6-9月
        "at_min": 24, "at_max": 27.5,
        "tcold": 22, "tcool": 24, "thot": 30,
        "temp_max_normal": 28,
        "label": "夏",
    },
    "autumn": {  # 10-11月
        "at_min": 20, "at_max": 26,
        "tcold": 20, "tcool": 22, "thot": 28,
        "temp_max_normal": 26,
        "label": "秋",
    },
    "winter": {  # 12-2月
        "at_min": 18, "at_max": 24,
        "tcold": 18, "tcool": 20, "thot": 26,
        "temp_max_normal": 25,
        "label": "冬",
    },
}


def _get_season() -> str:
    """根据月份返回杭州季节。"""
    m = datetime.now().month
    if 3 <= m <= 5:
        return "spring"
    elif 6 <= m <= 9:
        return "summer"
    elif 10 <= m <= 11:
        return "autumn"
    else:
        return "winter"

PROFILES = {
    "sleeping":  {"at_max": 28.5, "temp_max": 26.5, "co2_warn": 1000},
    "napping":   {"temp_max": 26.5, "co2_warn": 1000},
    "resting":   {"temp_max": 26.5, "co2_warn": 1000},
    "normal":    {"temp_max": 26.5, "co2_warn": 1200},
    "movie":     {"temp_max": 28,   "co2_warn": 9999},
    "bath":      {"temp_max": 99,   "co2_warn": 9999},
}


def load_room_activity(room: str) -> str:
    try:
        with open(os.path.join(STATE_DIR, "room_state.json")) as f:
            rs = json.load(f).get("rooms", {}).get(room, {})
    except Exception:
        return "unknown"
    act = rs.get("activity", "unknown")
    if isinstance(act, list):
        for p in ("sleeping", "napping", "watching_movie", "watching_tv",
                  "showering", "washing", "using_computer", "occupied",
                  "chaxi", "entering", "resting", "waking_up"):
            if p in act:
                return p
        return act[0] if act else "unknown"
    return act


def get_profile(activity: str) -> dict:
    if activity in ("sleeping", "napping", "resting"):
        return PROFILES["sleeping"]
    if activity in ("watching_movie", "watching_tv"):
        return PROFILES["movie"]
    if activity in ("showering", "washing"):
        return PROFILES["bath"]
    return PROFILES["normal"]


def _calc_at(temp_c: float, rh_pct: float) -> float:
    """体感温度 — 线性湿度补偿法（60%RH 基准零偏移）。
    22~24°C 线性淡入（<22°C 补偿归零），≥24°C 温度越高湿度放大效应越强。
    淡入用于消除原 24°C 硬切点造成的阶跃突变（曾达 ±1.1°C）。
    安全钳位：最多压低 -1.0°C，最多升高 +2.5°C。"""
    # 以 60% RH 为体感基准
    base_comp = (rh_pct - 60) * 0.05
    heat_multiplier = 1.0 + max(0.0, temp_c - 24) * 0.1
    # 22~24°C 线性淡入系数 0.0~1.0，保证 22 与 24 两点均连续
    fade = max(0.0, min(1.0, (temp_c - 22.0) / 2.0))
    compensation = base_comp * heat_multiplier * fade
    # 安全钳位
    clamped_comp = max(-1.0, min(compensation, 2.5))
    return round(temp_c + clamped_comp, 1)


# 体感温度舒适阈值
AT_COMFORT_MIN = 22   # 低于此 → 偏冷不适宜
AT_COMFORT_MAX = 27   # 高于此 → 偏热不适宜


def evaluate_room(room: str) -> dict:
    sensors = ROOM_SENSORS.get(room, {})
    activity = load_room_activity(room)
    profile = get_profile(activity)

    base = {
        "room": room, "activity": activity,
        "all_states": ALL_STATES,
    }

    readings = {}
    issues = []

    # 季节参数
    season = _get_season()
    cfg = SEASON_CONFIG[season]
    tcold, tcool, thot = cfg["tcold"], cfg["tcool"], cfg["thot"]

    # 温度 + 体感
    temp_str = get_state(sensors.get("temp", ""))
    # 降级：主传感器 unavailable → AC 回风温度
    if (not temp_str or temp_str in ("unknown", "unavailable")) and room in TEMP_FALLBACK:
        fb = TEMP_FALLBACK[room]
        fb_data = get_state(fb)
        if fb_data and fb_data not in ("unknown", "unavailable"):
            # climate entity 的 state 是 hvac_mode，读 attributes.current_temperature
            temp_str = _get_attr(fb, "current_temperature")
            if temp_str and temp_str not in ("unknown", "unavailable"):
                base["sensor_disconnected"] = True  # 标记兜底
    hum_str = get_state(sensors.get("hum", ""))
    temp_f = hum_f = None
    if temp_str and temp_str not in ("unknown", "unavailable"):
        try:
            temp_f = float(temp_str)
            readings["temp"] = temp_f
        except ValueError:
            pass
    if hum_str and hum_str not in ("unknown", "unavailable"):
        try:
            hum_f = float(hum_str)
            readings["hum"] = hum_f
        except ValueError:
            pass

    at_val = None
    tmax = profile.get("temp_max", cfg["temp_max_normal"])
    # E: 后半夜(03:00-08:00)深睡代谢降低，睡眠温度上限 +1°C 防凌晨冷醒
    if activity == "sleeping" and 3 <= datetime.now().hour < 10:
        tmax += 1
    # 体感上限严格对齐 climate_engine 的 at_comfort：睡觉后半夜(03:00-10:00) 28.5，其余 27.5。
    # 防止"看板判适宜、引擎仍在压低设定点"的两层 1°C 错位（napping/resting 引擎按 27.5 处理）
    at_max_val = profile.get("at_max", cfg["at_max"])
    if activity in ("sleeping", "napping", "resting"):
        at_max_val = 28.5 if (activity == "sleeping" and 3 <= datetime.now().hour < 10) else 27.5
    thresholds = {
        "temp_max": tmax,          # 偏热线（活动/季节自适应）
        "thot": thot,              # 过热线
        "tcool": tcool, "tcold": tcold,   # 偏冷/过冷线
        "hum_high": HUM_HIGH,      # 偏湿线 65
        "at_min": profile.get("at_min", cfg["at_min"]),   # 体感下限
        "at_max": at_max_val,                            # 体感上限
    }
    if temp_f is not None and hum_f is not None:
        at_val = _calc_at(temp_f, hum_f)
        readings["at"] = round(at_val, 1)
    # 偏热/过热用裸温判断
    if temp_f is not None:
        if temp_f < tcold:
            issues.append(("过冷", f"温度{temp_f}°C<{tcold}°C"))
        elif temp_f < tcool:
            issues.append(("偏冷", f"温度{temp_f}°C<{tcool}°C"))
        elif temp_f >= thot:
            issues.append(("过热", f"温度{temp_f}°C≥{thot}°C"))
        elif temp_f > tmax:
            issues.append(("偏热", f"温度{temp_f}°C>{tmax}°C"))

    # 湿度（洗浴时跳过，复用前面读到的 hum_f）
    if activity not in ("showering", "washing") and hum_f is not None:
        if hum_f < HUM_DRY:
            issues.append(("过干", f"湿度{hum_f}%<{HUM_DRY}%"))
        elif hum_f < HUM_COOL:
            issues.append(("偏干", f"湿度{hum_f}%，偏低"))
        elif hum_f <= HUM_HIGH:
            pass
        elif hum_f <= HUM_WET:
            issues.append(("偏湿", f"湿度{hum_f}%>{HUM_HIGH}%"))
        else:
            issues.append(("过湿", f"湿度{hum_f}%≥{HUM_WET}%"))

    # CO₂（观影时跳过）
    if activity not in ("watching_movie", "watching_tv"):
        co2_str = get_state(sensors.get("co2", ""))
        if co2_str and co2_str not in ("unknown", "unavailable"):
            try:
                co2_f = float(co2_str)
                readings["co2"] = co2_f
                if co2_f > profile["co2_warn"]:
                    issues.append(("空气污浊", f"CO₂ {co2_f}ppm>{profile['co2_warn']}ppm"))
            except ValueError:
                pass

    # 跑温检测（仅主卧/书房）
    # 情况1 窗开：本房窗连续开≥10min → 跑温（无条件）
    # 情况2 门开：门+联通房窗开≥10min → 跑温；联通房窗不开则看温差(联通房比本房高2°C以上=真热入侵→跑温)
    #   注意：温差用动态差(conn_t - room_t)，不用固定tmax，v2引擎绝不开关机所以不振荡
    dw = DOOR_WINDOW.get(room, {})
    if dw:
        room_t = readings.get("temp", 0) or 0
        reasons = []
        # 情况1：本房窗连续开≥10min → 直接跑温（客餐厅多扇窗，任一开即算）
        _wins = dw.get("window", "")
        _wins = _wins if isinstance(_wins, list) else ([_wins] if _wins else [])
        if any(_open_long(w) for w in _wins):
            reasons.append("窗开")
        # 情况2：遍历所有联通门通路，任一满足即跑温
        for p in DOOR_PATHS.get(room, []):
            has_aw = bool(p.get("also_window"))
            # 联通房窗开 → 直接跑温（冷气流经窗户流失外界）
            if has_aw and _open_long(p["also_window"]):
                if _open_long(p["door"]):                                     # 门开且联通房窗开→直接
                    reasons.append(f"联通房窗开→{p['room']}")
                continue                                                      # 窗开了就不再查温差
            if not _open_long(p["door"]):                                     # 门连续开≥10min
                continue
            if has_aw:
                continue   # also_window存在但窗没开, 不满足
            if not all(get_state(sw) == "off" for sw in p["ac_sw"]):           # 联通房AC全关
                continue
            try:
                conn_t = float(get_state(p["temp"]))
                if room_t and conn_t > room_t + 2:                            # 联通房比本房高2°C以上→真热入侵
                    reasons.append(f"门开→{p['room']}({conn_t:.1f}°C>本房{room_t:.1f}°C+2,未开空调)")
            except (ValueError, TypeError):
                pass
        if reasons:
            issues.append(("跑温", " | ".join(reasons)))

    # ── 舒适度 + 最大贡献方（体感溢价 Delta 归因）──
    comfort = "适宜"
    contributor = ""
    at_val = readings.get("at")
    temp_val = readings.get("temp")
    if at_val is not None and temp_val is not None:
        at_max = thresholds.get("at_max", cfg["at_max"])
        delta = at_val - temp_val  # 体感溢价：湿度推高的部分
        if at_val <= at_max:
            comfort = "适宜"
        elif temp_val <= at_max and delta >= 0.5:
            # 裸温未超标，湿度推高体感 → 纯闷
            comfort = "不适宜"
            contributor = "湿度偏高"
        elif temp_val > at_max and delta < 0.5:
            # 裸温本身超标，湿度没补刀 → 干热
            comfort = "不适宜"
            contributor = "温度"
        elif temp_val > at_max and delta >= 0.5:
            # 裸温超标 + 湿度补刀 → 温湿双高
            comfort = "不适宜"
            contributor = "温湿综合"
        # 体感偏冷
        at_min = thresholds.get("at_min", profile.get("at_min", cfg["at_min"]))
        if at_val < at_min:
            comfort = "不适宜"
            contributor = "温度" if temp_val < at_min else "湿度偏低"
    else:
        # 湿度缺失/AT算不出（传感器掉线，温度靠AC回风降级只有裸温）→ 不臆断体感舒适，
        # 避免"过热+舒适"自相矛盾。裸温/裸湿的 issue 照常输出。
        comfort = "数据不足"

    co2_val = readings.get("co2")
    # CO₂高 只作为信息标签，不影响舒适判断（舒适只看体感温度）
    if co2_val and co2_val > profile["co2_warn"]:
        co2_poor = True
    else:
        co2_poor = False

    # ── 贡献方驱动状态 ──
    def _add_contributor_badge(cond_list):
        """根据贡献方确保对应 badge 点亮。"""
        if contributor in ("湿度偏高", "温湿综合") and "偏湿" not in cond_list and "过湿" not in cond_list:
            cond_list.insert(0, "偏湿")
        if contributor == "湿度偏低" and "偏干" not in cond_list and "过干" not in cond_list:
            cond_list.insert(0, "偏干")
        if ("温度" in contributor and at_val is not None and at_val > thresholds["at_max"]
                and "偏热" not in cond_list and "过热" not in cond_list):
            cond_list.insert(0, "偏热")
        if co2_poor and "空气污浊" not in cond_list:
            cond_list.insert(0, "空气污浊")

    # 无传感器 — 标记断连，用AC回风兜底（不消失卡片）
    if not readings:
        base["sensor_disconnected"] = True
        base["condition"] = ["传感器断连"]
        base["comfort"] = "?"
        base["contributor"] = ""
        base["readings"] = {}
        base["thresholds"] = {}
        base["reason"] = "传感器无数据"
        return base

    # 舒适
    if not issues:
        if comfort == "适宜":
            cond = ["舒适"]
        elif comfort == "不适宜":
            cond = ["不适宜"]
        else:  # 数据不足 → 不输出舒适/不适宜
            cond = []
        if contributor:
            _add_contributor_badge(cond)
        at_range = f"{thresholds.get("at_max",27.5)-0.5:.1f}~{thresholds.get("at_max","?")}°C"
        reason = f"体感 {readings.get('at','?')}°C，{activity}模式({at_range}) → {comfort}"
        if contributor:
            reason += f"，{contributor}贡献"
        return {**base, "condition": cond, "comfort": comfort,
                "contributor": contributor,
                "readings": readings, "thresholds": thresholds, "reason": reason}

    # 所有问题 + 舒适度
    cond = [i[0] for i in issues]
    _add_contributor_badge(cond)
    if comfort == "适宜":
        cond.append("舒适")
    elif comfort == "不适宜":
        cond.append("不适宜")
    # 数据不足 → 不追加舒适度结论（避免与裸温 issue 自相矛盾）
    reason = " | ".join(i[1] for i in issues)
    at_range = f"{thresholds.get("at_max",27.5)-0.5:.1f}~{thresholds.get("at_max","?")}°C"
    reason += f" → 体感 {readings.get('at','?')}°C，{activity}模式({at_range})，{comfort}"
    if contributor:
        reason += f"，{contributor}贡献"
    return {**base, "condition": cond, "comfort": comfort,
            "contributor": contributor,
            "readings": readings, "thresholds": thresholds, "reason": reason}


def run() -> dict:
    season = _get_season()
    result = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": int(time.time()),
        "season": SEASON_CONFIG[season]["label"],
        "rooms": {}
    }
    for room in ("br", "st", "lr", "dr", "sb", "gb", "nb", "bath"):
        ev = evaluate_room(room)
        if ev:
            result["rooms"][room] = ev
    with open(os.path.join(STATE_DIR, "env_quality.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
