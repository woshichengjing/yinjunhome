#!/usr/bin/env python3
"""Room state machine - 5s polling, batch read HA, local eval, batch write HA entity + room_state.json."""
import json, os, time, urllib.request, sys

os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

HA = "http://10.90.1.19:8123"
STATE_DIR = "/tmp/hermes_states"
HASS_TOKEN = ""

def _load_token():
    global HASS_TOKEN
    for p in [os.path.expanduser("~/.hermes/.env"), "/root/.hermes/.env", "/home/hermeswebui/.hermes/.env"]:
        if os.path.isfile(p):
            with open(p) as f:
                for line in f:
                    if line.startswith("HASS_TOKEN="):
                        HASS_TOKEN = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        break
        if HASS_TOKEN:
            break
    if not HASS_TOKEN:
        print("[room_state] FATAL: HASS_TOKEN not found", flush=True)
        sys.exit(1)

_load_token()

OCC_DELAY = 5
EMP_DELAY = 10
SLEEP_START = 21
SLEEP_END = 12
CAT_NIGHT_START = 22
CAT_NIGHT_END = 7
CAT_NOISE_MAX = 45
STATE_CN = {"empty": "无人", "entering": "进入中", "occupied": "有人活动", "sleeping": "睡觉"}

ROOMS = {
    "lr": {"occ": "binary_sensor.xiaomi_cn_820766243_p1_occupancy_status_p_2_1",
           "no_one": "sensor.xiaomi_cn_820766243_p1_no_one_duration_p_2_4",
           "lights_group": "light.mijia_cn_group_1681097797967228928_group3_s_2_light",
           "tv1": "media_player.tcl_85q10g_pro_28cd_10_5",
           "tv2": "media_player.tcl_85q10g_pro_28cd_10_90_1_8_tcl_85q10g_pro_28cd_10_90_1_8",
           "tv_box": "media_player.ke_ting_de_xiao_mi_he_zi_2",
           "noise": "sensor.ke_ting_kong_qi_jian_ce_yi_noise"},
    "dr": {"occ": "binary_sensor.izq_cn_1094698163_24_occupancy_status_p_2_1",
           "no_one": "sensor.izq_cn_1094698163_24_no_one_duration_p_2_4",
           "lights_group": "light.mijia_cn_group_1683634107420463104_group3_s_2_light",
           "tv1": "media_player.tcl_85q10g_pro_28cd_10_5",
           "tv2": "media_player.tcl_85q10g_pro_28cd_10_90_1_8_tcl_85q10g_pro_28cd_10_90_1_8",
           "tv_box": "media_player.ke_ting_de_xiao_mi_he_zi_2",
           "noise": "sensor.ke_ting_kong_qi_jian_ce_yi_noise"},
    "kt": {"occ": "binary_sensor.izq_cn_1093543654_24_occupancy_status_p_2_1",
           "no_one": "sensor.izq_cn_1093543654_24_no_one_duration_p_2_4"},
    "cr": {"occ": "binary_sensor.linp_cn_1005702335_hb01_occupancy_status_p_2_1",
           "no_one": "sensor.linp_cn_1005702335_hb01_no_one_duration_p_2_4"},
    "gb": {"occ": "binary_sensor.linp_cn_1082094403_hb01_occupancy_status_p_2_1",
           "no_one": "sensor.linp_cn_1082094403_hb01_no_one_duration_p_2_4"},
    "bath": {"occ": "binary_sensor.linp_cn_blt_3_1pjv3016t0g01_es4b_occupancy_status_p_2_1078",
             "no_one": "sensor.linp_cn_blt_3_1pjv3016t0g01_es4b_no_one_duration_p_2_1082"},
    "nb": {"occ": "binary_sensor.izq_cn_1086711016_24_occupancy_status_p_2_1",
           "no_one": "sensor.izq_cn_1086711016_24_no_one_duration_p_2_4",
           "illuminance": "sensor.izq_cn_1086711016_24_illumination_p_2_5"},
    "en": {"occ": "binary_sensor.linp_cn_1005702682_hb01_occupancy_status_p_2_1",
           "no_one": "sensor.linp_cn_1005702682_hb01_no_one_duration_p_2_4"},
    "st": {"occ": "binary_sensor.linp_cn_1005702677_hb01_occupancy_status_p_2_1",
           "no_one": "sensor.linp_cn_1005702677_hb01_no_one_duration_p_2_4"},
    "sb": {"occ": "binary_sensor.izq_cn_1086679488_24_occupancy_status_p_2_1",
           "no_one": "sensor.izq_cn_1086679488_24_no_one_duration_p_2_4",
           "illuminance": "sensor.izq_cn_1086679488_24_illumination_p_2_5"},
    "br": {"occ": "binary_sensor.linp_cn_1083284825_hb01_occupancy_status_p_2_1",
           "no_one": "sensor.linp_cn_1083284825_hb01_no_one_duration_p_2_4",
           "illuminance": "sensor.linp_cn_1083284825_hb01_illumination_p_2_5"},
}


def fetch_all():
    """Batch read all HA entities in one request."""
    try:
        req = urllib.request.Request(
            f"{HA}/api/states",
            headers={"Authorization": f"Bearer {HASS_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            all_e = json.loads(r.read())
        return {e["entity_id"]: e["state"] for e in all_e}
    except Exception as e:
        print(f"  [fetch_all] error: {e}", flush=True)
        return {}


def push_room_state(room, act, dur, reason, since, last_act=""):
    """Batch POST to HA entities (5 per room)."""
    payloads = [
        (f"input_text.room_state_{room}", act),
        (f"input_number.room_state_dur_{room}", str(dur)),
        (f"input_text.room_state_{room}_reason", reason),
        (f"input_text.room_state_{room}_since", since),
        (f"input_text.room_state_{room}_last_activity", last_act),
    ]
    for eid, val in payloads:
        try:
            data = json.dumps({"state": val}).encode()
            req = urllib.request.Request(
                f"{HA}/api/states/{eid}",
                data=data, method="POST",
                headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception as e:
            print(f"  [push] {eid}: {e}", flush=True)


def eval_room(room, cfg, states, since_states):
    """Local evaluation, no HA API calls."""
    NOW = time.localtime()
    NOW_TS = int(time.mktime(NOW))

    occ = (states.get(cfg["occ"], "off") or "off").lower()

    # ═══ 状态机独立计时：不依赖传感器 no_one ═══
    # occ 变 off 时记录时间戳，用本地计时判断空房时长
    off_file = os.path.join("/tmp/hermes_states", f"occ_off_{room}")
    on_file = os.path.join("/tmp/hermes_states", f"occ_on_{room}")
    if occ == "off":
        if not os.path.isfile(off_file):
            with open(off_file, "w") as f: f.write(str(NOW_TS))
        try:
            off_since = int(open(off_file).read().strip())
            no_one = (NOW_TS - off_since) // 60  # 状态机自己计时的无人分钟
        except:
            no_one = 0
        # 清除有人标记
        try: os.remove(on_file)
        except OSError: pass
    else:
        # occ=on → 清除无人标记，记录有人开始时间
        try: os.remove(off_file)
        except OSError: pass
        no_one = 0
        if not os.path.isfile(on_file):
            with open(on_file, "w") as f: f.write(str(NOW_TS))
        try:
            on_since = int(open(on_file).read().strip())
            occ_dur = (NOW_TS - on_since) // 60  # 有人持续时长（分钟）
        except:
            occ_dur = 0

    sv = since_states.get(f"input_text.room_state_{room}_since", "") or ""
    la = since_states.get(f"input_text.room_state_{room}_last_activity", "") or ""

    ST = 0
    sv_state = ""
    if "|" in sv:
        parts = sv.split("|")
        sv_state = parts[0].strip("[]") if parts else ""
        try: ST = int(parts[1])
        except: pass
    md = (NOW_TS - ST) // 60 if ST else 0
    # occ=on 时，md 应反映"有人持续时长"，而非"距上次状态变化时长"
    if occ == "on":
        md = occ_dur

    # 保持 since 时间戳：同一状态不重置
    _empty_since = f"[empty]|{ST if sv_state == 'empty' and ST else NOW_TS}"
    _sleep_since = f"[sleeping]|{ST if sv_state == 'sleeping' and ST else NOW_TS}"
    _occ_since = f"[occupied]|{ST if (sv_state in ('occupied','entering') or 'occupied' in sv_state) and ST else NOW_TS}"

    hour = NOW.tm_hour
    is_night = (hour >= SLEEP_START) or (hour < SLEEP_END)

    # BR sleep detection: night(0-12) + occupied + illuminance < 100
    if room == "br":
        try:
            illuminance = int(float(states.get(cfg.get("illuminance", ""), "999") or 999))
        except:
            illuminance = 999
        dark = illuminance < 100
        if occ == "on" and is_night and dark and "occupied" in la:
            push_room_state(room, "sleeping", md,
                f"夜间{hour}点 + 有人活动 + 亮度仅{illuminance}lx(<100) → 判定为睡觉",
                _sleep_since, "sleeping")
            return
        if "sleeping" in la:
            if occ == "off" and no_one >= EMP_DELAY:
                push_room_state(room, "empty", md,
                    f"已醒来，无人持续{md}分钟 → 确认离开卧室", _empty_since)
                return
            if is_night and dark:
                push_room_state(room, "sleeping", md,
                    f"夜间{hour}点 + 亮度{illuminance}lx(<100) → 光线暗，保持睡觉状态", _sleep_since, "sleeping")
                return

    # SB/NB sleep detection: night(0-12) + occupied + illuminance < 100
    if room in ("sb", "nb"):
        try:
            illuminance = int(float(states.get(cfg.get("illuminance", ""), "999") or 999))
        except:
            illuminance = 999
        dark = illuminance < 100
        if occ == "on" and is_night and dark and "occupied" in la:
            push_room_state(room, "sleeping", md,
                f"夜间{hour}点 + 有人活动 + 亮度仅{illuminance}lx(<100) → 判定为睡觉",
                _sleep_since, "sleeping")
            return
        if "sleeping" in la:
            if occ == "off" and no_one >= EMP_DELAY:
                push_room_state(room, "empty", md,
                    f"已醒来，无人持续{md}分钟 → 确认离开卧室", _empty_since)
                return
            if is_night and dark:
                push_room_state(room, "sleeping", md,
                    f"夜间{hour}点 + 亮度{illuminance}lx(<100) → 光线暗，保持睡觉状态", _sleep_since, "sleeping")
                return

    # ── LR/DR cat detection: night + lights off + TV off + quiet ──
    # 待客保护：夜间手动开机后，当晚禁用猫检测，次日07:00清除
    _guest_file = os.path.join("/tmp/hermes_states", "guest_mode_today")
    _guest_skip = False
    if os.path.isfile(_guest_file):
        if NOW.tm_hour >= 7:
            try: os.remove(_guest_file)
            except OSError: pass
        else:
            _guest_skip = True
    if not _guest_skip and room in ("lr", "dr") and occ == "on" and "sleeping" not in la:
        cat_hour = NOW.tm_hour
        cat_night = cat_hour >= CAT_NIGHT_START or cat_hour < CAT_NIGHT_END
        if cat_night:
            lights_off = (states.get(cfg.get("lights_group", ""), "off") or "off").lower() == "off"
            tv1_st = (states.get(cfg.get("tv1", ""), "off") or "off").lower()
            tv2_st = (states.get(cfg.get("tv2", ""), "off") or "off").lower()
            tv_box_st = (states.get(cfg.get("tv_box", ""), "off") or "off").lower()
            tv_off = all(s not in ("playing", "on") for s in (tv1_st, tv2_st, tv_box_st))
            try:
                noise_val = int(float(states.get(cfg.get("noise", "999"), "999") or 999))
            except:
                noise_val = 999
            quiet = noise_val < CAT_NOISE_MAX
            if lights_off and tv_off and quiet:
                push_room_state(room, "empty", md,
                    f"夜间{cat_hour}点，灯全关、电视全关、噪音仅{noise_val}dB(<45) → 四条件全满足，疑似猫，不触发有人活动",
                    _empty_since)
                return

    # ── ST projector detection ──
    if room == "st":
        proj = cfg.get("projector", "")
        if proj:
            proj_st = states.get(proj, "") or ""
            if proj_st and proj_st not in ("off", "unavailable", "unknown", ""):
                push_room_state(room, "watching_movie", md,
                    f"投影仪状态={proj_st} → 判定为观影模式", f"[watching_movie]|{NOW_TS}", "watching_movie")
                return

    # Generic state evaluation
    if occ == "off" and no_one >= EMP_DELAY:
        push_room_state(room, "empty", md,
            f"传感器持续{md}分钟无人(≥{EMP_DELAY}分钟阈值) → 确认无人", _empty_since)
    elif occ == "off" and no_one < EMP_DELAY:
        if "occupied" in la or "sleeping" in la:
            prev = "occupied" if "occupied" in la else "sleeping"
            push_room_state(room, prev, md,
                f"离开{no_one}分钟(<{EMP_DELAY}分钟阈值)，且之前是{STATE_CN.get(prev, prev)}状态 → 短暂离开，保持原状态", sv, prev)
        else:
            push_room_state(room, "empty", md,
                f"传感器显示无人{no_one}分钟(<{EMP_DELAY}分钟阈值)，之前非活动状态 → 判定为无人", _empty_since)
    elif md < OCC_DELAY:
        if "occupied" in la or "sleeping" in la:
            push_room_state(room, la, md,
                f"有人持续{md}分钟(<{OCC_DELAY}分钟确认阈值)，之前是{STATE_CN.get(la, la)} → 维持原状态",
                f"[{la}]|{ST if ST else NOW_TS}", la)
        else:
            push_room_state(room, "entering", md,
                f"传感器检测到有人{md}分钟(<{OCC_DELAY}分钟确认阈值) → 进入中，等待确认",
                f"[entering]|{ST if ST else NOW_TS}")
    else:
        push_room_state(room, "occupied", md,
            f"传感器持续有人{md}分钟(≥{OCC_DELAY}分钟确认阈值) → 确认有人活动", _occ_since, "occupied")


def main():
    t0 = time.time()

    states = fetch_all()
    if not states:
        return time.time() - t0

    # Build since_states from fetch results
    since_states = {}
    for k, v in states.items():
        if "_room_state_" in k and ("_since" in k or "_last_activity" in k):
            since_states[k] = v
    for k, v in states.items():
        if k.startswith("input_text.room_state_") and not k.endswith("_reason"):
            since_states[k] = v

    for room, cfg in ROOMS.items():
        try:
            eval_room(room, cfg, states, since_states)
        except Exception as e:
            print(f"  [{room}] eval: {e}", flush=True)

    # Write room_state.json for other modules
    room_states = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rooms": {}}
    for room in ROOMS:
        act = states.get(f"input_text.room_state_{room}", "")
        dur = states.get(f"input_number.room_state_dur_{room}", "0")
        reason = states.get(f"input_text.room_state_{room}_reason", "")
        room_states["rooms"][room] = {
            "activity": act,
            "duration": int(float(dur)) if dur else 0,
            "reason": reason[:100] if reason else "",
        }

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "room_state.json"), "w") as f:
            json.dump(room_states, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [write] room_state.json: {e}", flush=True)

    elapsed = time.time() - t0
    if elapsed > 4:
        print(f"  [perf] cycle={elapsed:.1f}s (slow!)", flush=True)
    return elapsed


if __name__ == "__main__":
    _cycles = [0]
    print(f"[room_state] started, {len(ROOMS)} rooms, token={HASS_TOKEN[:10]}...", flush=True)
    while True:
        try:
            elapsed = main()
            _cycles[0] += 1
            if _cycles[0] % 12 == 1:
                print(f"  [beat] c={_cycles[0]} t={elapsed:.1f}s", flush=True)
            time.sleep(max(0.5, 5 - elapsed))
        except Exception as e:
            print(f"  [CRASH] {e}", flush=True)
            import traceback; traceback.print_exc()
            time.sleep(5)
