#!/usr/bin/env python3
"""每分钟采集关键传感器快照 + 压缩机计时器 → /app/static/data/sensors.json"""
import json, os, sys, urllib.request, time

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

OUT = "/app/static/data/sensors.json"
CLIMATE_DIR = "/tmp/hermes_climate"

SENSORS = {
    "br_temp": "sensor.zhu_wo_kong_qi_jian_ce_yi_temperature",
    "lr_temp": "sensor.ke_ting_kong_qi_jian_ce_yi_temperature",
    "st_temp": "sensor.miaomiaoc_cn_blt_3_1bbe5qecclc00_t6_temperature_p_3_1001",
    "out_temp": "sensor.lumi_cn_lumi_158d000116b3a8_v1_temperature_p_2_1",
    "out_hum": "sensor.lumi_cn_lumi_158d000116b3a8_v1_relative_humidity_p_2_2",
    "lr_co2": "sensor.ke_ting_kong_qi_jian_ce_yi_co2",
    "br_co2": "sensor.zhu_wo_kong_qi_jian_ce_yi_co2",
    "home_occ": "input_boolean.jia_li_you_ren",
    "br_ac_sw": "climate.scdvb_cn_2002962758_t001",
    "st_ac_sw": "climate.scdvb_cn_2002146955_t001",
    "br_door": "binary_sensor.isa_cn_blt_3_1lnimpgbs4c00_dw2hl_contact_state_p_2_2",
    "st_door": "binary_sensor.isa_cn_blt_3_144u9efpg5c01_dw2hl_contact_state_p_2_2",
    "xin_feng": "switch.giot_cn_2002953222_v82ksm_channel_4_p_3_1",
    "br_ac_temp": "climate.scdvb_cn_2002962758_t001",
    "st_ac_temp": "climate.scdvb_cn_2002146955_t001",
    "br_bed": "binary_sensor.linp_cn_blt_3_1llu98b44c400_ps1bb_pressure_present_state_p_2_1060",
    "br_occ": "binary_sensor.zhu_wo_ren_zai_chuan_gan_qi",  # 主卧mmWave人在传感器（非jia_li_you_ren）
    "st_occ": "sensor.linp_cn_1005702677_hb01_occupancy_status_p_2_1",
}

# Timer state files
TIMER_FILES = {
    "br_on_time": "br_on_time",
    "st_on_time": "st_on_time",
    "br_lastoff": "br_lastoff",
    "st_lastoff": "st_lastoff",
    "br_door_open": "br_door_open_time",
    "st_door_open": "st_door_open_time",
    "br_door_suppress": "br_door_suppress",
    "br_bed_off": "br_bed_off_time",
    "br_wake_done": "br_wake_done",
    "empty_last_run": "empty_last_run",
    "br_precool": "br_precool",
    "br_precool_done": "br_precool_done",
    "st_precool": "st_precool",
    "st_precool_done": "st_precool_done",
}

MIN_RUN = 15 * 60  # 15 min in seconds
RESTART_DELAY = 15 * 60
DEBOUNCE_DOOR = 5 * 60
DEBOUNCE_OCC = 5 * 60
LAZY_INTERVAL = 10 * 60
WAKE_WINDOW = 30 * 60
WAKE_OFF_TIME = 10 * 60
PRECOOL_TIMEOUT = 120 * 60


def get_state(entity_id):
    try:
        req = urllib.request.Request(
            f"{HASS_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {HASS_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"state": "error", "attributes": {}}


def read_timer(name):
    path = os.path.join(CLIMATE_DIR, name)
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def main():
    now = int(time.time())
    result = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "sensors": {}, "timers": {}}

    # Batch fetch HA sensors
    for key, eid in SENSORS.items():
        data = get_state(eid)
        attrs = data.get("attributes", {})
        result["sensors"][key] = {
            "state": data.get("state", "?"),
            "friendly_name": attrs.get("friendly_name", key),
            "unit": attrs.get("unit_of_measurement", ""),
            "temperature": attrs.get("temperature", ""),
            "current_temperature": attrs.get("current_temperature", ""),
        }

    # AC state normalization: climate entity → "on"/"off" for dashboard
    for key in ("br_ac_sw", "st_ac_sw"):
        raw = result["sensors"].get(key, {}).get("state", "?")
        result["sensors"][key]["state"] = "off" if raw in ("off", "unknown", "unavailable", "?") else "on"

    # Read timer state files
    timers_raw = {}
    for key, fname in TIMER_FILES.items():
        timers_raw[key] = read_timer(fname)

    # Compute timer values (seconds remaining / total seconds)
    def remaining(since, cap):
        if not since: return {"elapsed": 0, "remaining": 0, "cap": cap, "active": False}
        elapsed = now - since
        if elapsed >= cap: return {"elapsed": cap, "remaining": 0, "cap": cap, "active": False}
        return {"elapsed": elapsed, "remaining": cap - elapsed, "cap": cap, "active": True}

    def since_active(since, cap):
        """Timer counts UP: e.g. wake window, lazy timer"""
        if not since: return {"elapsed": 0, "remaining": cap, "cap": cap, "active": False}
        elapsed = now - since
        if elapsed >= cap: return {"elapsed": cap, "remaining": 0, "cap": cap, "active": False}
        return {"elapsed": elapsed, "remaining": cap - elapsed, "cap": cap, "active": True}

    def file_exists(name):
        return os.path.isfile(os.path.join(CLIMATE_DIR, name))

    t = {}
    t["br_min_runtime"] = remaining(timers_raw["br_on_time"], MIN_RUN)
    t["st_min_runtime"] = remaining(timers_raw["st_on_time"], MIN_RUN)
    t["br_restart"] = remaining(timers_raw["br_lastoff"], RESTART_DELAY)
    t["st_restart"] = remaining(timers_raw["st_lastoff"], RESTART_DELAY)
    t["br_door_debounce"] = remaining(timers_raw["br_door_open"], DEBOUNCE_DOOR)
    t["st_door_debounce"] = remaining(timers_raw["st_door_open"], DEBOUNCE_DOOR)
    t["br_door_suppress"] = file_exists("br_door_suppress")
    t["br_bed_off_timer"] = remaining(timers_raw["br_bed_off"], WAKE_OFF_TIME)
    t["br_wake_done"] = file_exists("br_wake_done")
    t["empty_lazy"] = since_active(timers_raw["empty_last_run"], LAZY_INTERVAL)
    t["br_precool"] = remaining(timers_raw["br_precool"], PRECOOL_TIMEOUT)
    t["br_precool_done"] = file_exists("br_precool_done")
    t["st_precool"] = remaining(timers_raw["st_precool"], PRECOOL_TIMEOUT)
    t["st_precool_done"] = file_exists("st_precool_done")

    result["timers"] = t

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # The dashboard reads a credential-free local snapshot. Refreshing is
    # throttled inside qweather_forecast, so this is safe for a 1-minute timer.
    try:
        from qweather_forecast import refresh_if_stale
        refresh_if_stale()
    except Exception as exc:
        print(f"[snapshot] QWeather forecast unavailable: {exc}", flush=True)


if __name__ == "__main__":
    main()
