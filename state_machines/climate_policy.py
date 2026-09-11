"""意图层、引擎共用的自动制冷边界；条件标签不等于开机指令。"""
import math
import os
from datetime import timedelta

ACTIVE_STATES = {"occupied", "sleeping", "chaxi", "resting", "napping",
                 "using_computer", "watching_movie", "watching_tv"}
SHARED_ROOMS = ("lr", "dr", "en", "cr", "kt")


def finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def has_runaway(room, conditions, cond_fn):
    return "跑温" in conditions or (
        room in ("lr", "dr") and any("跑温" in cond_fn(r) for r in SHARED_ROOMS))


def has_occupants(room, activity, activity_fn):
    return activity in ACTIVE_STATES or (
        room in ("lr", "dr") and any(activity_fn(r) in ACTIVE_STATES for r in SHARED_ROOMS))


def cooling_allowed(conditions):
    # 舒适允许和裸温偏热同时出现，必须拥有否决权；冷、跑温同理。
    if any(state in conditions for state in ("跑温", "偏冷", "过冷", "舒适")):
        return False
    return any(state in conditions for state in ("偏热", "过热", "过湿"))


def guest_active(state_dir, now):
    """待客保护只对本次夜间有效，旧的 today 标记不能跨天永久生效。"""
    if not (now.hour >= 22 or now.hour < 7):
        return False
    start = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now.hour < 7:
        start -= timedelta(days=1)
    try:
        modified = os.path.getmtime(os.path.join(state_dir, "guest_mode_today"))
        return start.timestamp() <= modified <= now.timestamp()
    except OSError:
        return False
