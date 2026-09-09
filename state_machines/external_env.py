#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部环境状态机 v1 — 室外温湿度、峰谷电、天气预测。

输出：
  current:  当前温湿度、体感温度
  peak:     true/false 峰谷电
  season:   summer/winter
  fresh_eligible: 新风条件是否满足
  forecast: 短期趋势（rising/falling/stable）— 基于过去N小时温度变化
"""

import json, os, time, urllib.request
from datetime import datetime
from collections import defaultdict

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
NOW = int(time.time())


def get_state(entity_id: str) -> str:
    url = f"{HASS_URL}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HASS_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
            return str(d.get("state", ""))
    except Exception:
        return ""


# ══════════════════════════════════════════════
# 传感器 & 参数
# ══════════════════════════════════════════════

OUT_TEMP = "sensor.lumi_cn_lumi_158d000116b3a8_v1_temperature_p_2_1"
OUT_HUM = "sensor.lumi_cn_lumi_158d000116b3a8_v1_relative_humidity_p_2_2"

SEASON_THRESHOLD = 15  # <15°C 算冬季
PEAK_START, PEAK_END = 8, 22

FRESH_TEMP_MIN, FRESH_TEMP_MAX = 20, 26
FRESH_HUM_MAX = 70
FRESH_DELTA_MIN = 2  # 室内外温差


def load_config():
    try:
        with open(os.path.expanduser("~/.hermes/scripts/data/climate_config.json")) as f:
            cfg = json.load(f)
        g = cfg.get("general", {})
        return {
            "season_threshold": g.get("season_threshold", SEASON_THRESHOLD),
            "fresh_temp_min": g.get("fresh_temp_min", FRESH_TEMP_MIN),
            "fresh_temp_max": g.get("fresh_temp_max", FRESH_TEMP_MAX),
            "fresh_hum_max": g.get("fresh_hum_max", FRESH_HUM_MAX),
            "fresh_delta_min": g.get("fresh_delta_min", FRESH_DELTA_MIN),
            "co2_trigger": g.get("co2_trigger_ppm", 1000),
        }
    except Exception:
        return {}


def _fresh_temp_is_eligible(out_temp, cfg: dict) -> bool:
    if out_temp is None:
        return False
    try:
        minimum = cfg.get("fresh_temp_min", FRESH_TEMP_MIN)
        maximum = cfg.get("fresh_temp_max", FRESH_TEMP_MAX)
        return minimum <= float(out_temp) <= maximum
    except (TypeError, ValueError):
        return False


def _calc_at(t: float, rh: float) -> float:
    """体感温度 — 水汽压阈值法，仅需温湿度，适合室内无风。"""
    import math
    e = (rh / 100.0) * 6.105 * math.exp((17.27 * t) / (237.7 + t))
    return round(t + max(0, e - 10) * 0.10, 1)


def _abs_humidity(temp_c: float, rh_pct: float) -> float:
    """绝对湿度 g/m³ (Magnus formula)."""
    import math
    es = 6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5))
    ea = es * rh_pct / 100
    return round(2.1674 * ea / (273.15 + temp_c), 3)


def _dew_point(temp_c: float, rh_pct: float) -> float:
    """露点温度 °C."""
    import math
    a, b = 17.27, 237.7
    gamma = math.log(rh_pct / 100) + a * temp_c / (b + temp_c)
    return round(b * gamma / (a - gamma), 1)


def run() -> dict:
    out_temp_str = get_state(OUT_TEMP)
    out_hum_str = get_state(OUT_HUM)

    try:
        out_temp = float(out_temp_str) if out_temp_str else None
    except (ValueError, TypeError):
        out_temp = None
    try:
        out_hum = float(out_hum_str) if out_hum_str else None
    except (ValueError, TypeError):
        out_hum = None

    cfg = load_config()

    # 峰谷电
    hour = datetime.now().hour
    is_peak = PEAK_START <= hour < PEAK_END

    # 季节
    is_winter = out_temp is not None and out_temp < cfg.get("season_threshold", SEASON_THRESHOLD)

    # 体感温度
    at = _calc_at(out_temp, out_hum) if out_temp is not None and out_hum is not None else None

    # 新风条件 — 绝对湿度比较
    fresh_eligible = False
    fresh_reasons = []

    if out_temp is not None:
        tmin = cfg.get("fresh_temp_min", FRESH_TEMP_MIN)
        tmax = cfg.get("fresh_temp_max", FRESH_TEMP_MAX)
        if not _fresh_temp_is_eligible(out_temp, cfg):
            fresh_reasons.append(f"室外温度{out_temp}°C不在{tmin}-{tmax}°C范围")
    else:
        fresh_reasons.append("无法获取室外温度")

    if out_temp is not None and out_hum is not None:
        out_ah = _abs_humidity(out_temp, out_hum)
        # 读取室内各房间温湿度，计算最低绝对湿度
        indoor_ahs = []
        for room_eid in [
            ("sensor.zhu_wo_kong_qi_jian_ce_yi_temperature", "sensor.zhu_wo_kong_qi_jian_ce_yi_humidity"),
            ("sensor.miaomiaoc_cn_blt_3_1bbe5qecclc00_t6_temperature_p_3_1001", "sensor.miaomiaoc_cn_blt_3_1bbe5qecclc00_t6_relative_humidity_p_3_1008"),
            ("sensor.ke_ting_kong_qi_jian_ce_yi_temperature", "sensor.ke_ting_kong_qi_jian_ce_yi_humidity"),
            ("sensor.zhu_wo_kong_qi_jian_ce_yi_temperature_2", "sensor.zhu_wo_kong_qi_jian_ce_yi_humidity_2"),
        ]:
            try:
                t = float(get_state(room_eid[0]))
                h = float(get_state(room_eid[1]))
                indoor_ahs.append(_abs_humidity(t, h))
            except (ValueError, TypeError):
                pass

        if indoor_ahs:
            min_indoor_ah = min(indoor_ahs)
            if out_ah >= min_indoor_ah:
                fresh_reasons.append(f"室外绝对湿度{out_ah}g/m³ ≥ 室内最低{min_indoor_ah}g/m³")
        else:
            fresh_reasons.append("无法获取室内湿度数据")
    elif out_hum is None:
        fresh_reasons.append("无法获取室外湿度")

    if not fresh_reasons:
        fresh_eligible = True

    # 绝对湿度 + 露点
    out_ah = _abs_humidity(out_temp, out_hum) if out_temp is not None and out_hum is not None else None
    out_dp = _dew_point(out_temp, out_hum) if out_temp is not None and out_hum is not None else None

    # 温度趋势（过去3小时）
    trend = "unknown"
    trend_file = os.path.join(STATE_DIR, "outdoor_history.jsonl")
    try:
        history = []
        if os.path.isfile(trend_file):
            with open(trend_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    if d.get("ts", 0) > NOW - 10800:
                        history.append(d)
        history.append({"ts": NOW, "temp": out_temp, "hum": out_hum})
        # 裁剪
        with open(trend_file, "w") as f:
            for h in history[-180:]:
                f.write(json.dumps(h) + "\n")

        temps = [h["temp"] for h in history if h.get("temp") is not None]
        if len(temps) >= 3:
            if temps[-1] > temps[0] + 0.5:
                trend = "rising"
            elif temps[-1] < temps[0] - 0.5:
                trend = "falling"
            else:
                trend = "stable"
    except Exception:
        pass

    result = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": int(time.time()),
        "current": {
            "temp": out_temp,
            "hum": out_hum,
            "abs_humidity": out_ah,
            "dew_point": out_dp,
            "apparent_temp": at,
        },
        "peak": is_peak,
        "season": "winter" if is_winter else "summer",
        "fresh_eligible": fresh_eligible,
        "fresh_reasons": fresh_reasons if not fresh_eligible else [],
        "forecast": {
            "trend": trend,
            "data_points": len([h for h in (history if 'history' in dir() else []) if h.get("temp")]),
        },
    }

    with open(os.path.join(STATE_DIR, "external_env.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
