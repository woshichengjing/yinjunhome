#!/usr/bin/env python3
"""Fetch QWeather forecasts and publish a credential-free dashboard snapshot.

Configuration stays server-side in ``~/.hermes/.env``:
``QWEATHER_API_HOST`` plus ``QWEATHER_API_KEY`` (or ``QWEATHER_JWT``).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


LATITUDE = 30.31
LONGITUDE = 120.12
OUT = Path(os.environ.get("QWEATHER_FORECAST_OUT", "/app/static/data/weather_forecast.json"))
REFRESH_SECONDS = 30 * 60
ZONE = ZoneInfo("Asia/Shanghai")


def _load_env() -> None:
    paths = (
        Path.home() / ".hermes" / ".env",
        Path("/home/hermeswebui/.hermes/.env"),
    )
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percent(value):
    number = _number(value)
    if number is None:
        return None
    return round(number * 100 if 0 <= number <= 1 else number)


def _nested(data, *path, default=None):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _iso_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZONE)
    return parsed.astimezone(ZONE).isoformat(timespec="minutes")


def _local_date(value: str) -> str:
    return _iso_time(value)[:10]


def _headers() -> dict[str, str]:
    token = os.environ.get("QWEATHER_JWT", "").strip()
    api_key = (os.environ.get("QWEATHER_API_KEY", "") or os.environ.get("QWEATHER_KEY", "")).strip()
    if token:
        return {"Authorization": f"Bearer {token}", "User-Agent": "yinjunhome-weather/1.0"}
    if api_key:
        return {"X-QW-Api-Key": api_key, "User-Agent": "yinjunhome-weather/1.0"}
    raise RuntimeError("QWEATHER_JWT or QWEATHER_API_KEY is not configured")


def _request(path: str, params: dict[str, object]) -> dict:
    host = os.environ.get("QWEATHER_API_HOST", "").strip().removeprefix("https://").rstrip("/")
    if not host or "/" in host or "." not in host:
        raise RuntimeError("QWEATHER_API_HOST is not configured correctly")
    url = "https://" + host + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _hourly_snapshot(payload: dict) -> dict[str, list]:
    hours = payload.get("hours")
    if not isinstance(hours, list) or len(hours) < 24:
        raise RuntimeError("QWeather hourly response is incomplete")
    result = {"time": [], "temperature_2m": [], "weather_code": [], "weather_text": [], "precipitation_probability": [], "is_day": []}
    for item in hours[:24]:
        forecast_time = _iso_time(item["forecastTime"])
        result["time"].append(forecast_time)
        result["temperature_2m"].append(_number(_nested(item, "temperature", "value")))
        result["weather_code"].append(int(_number(_nested(item, "condition", "code"), -1)))
        result["weather_text"].append(str(_nested(item, "condition", "text", default="未知")))
        result["precipitation_probability"].append(_percent(_nested(item, "precipitation", "probability")))
        hour = datetime.fromisoformat(forecast_time).hour
        result["is_day"].append(1 if 7 <= hour < 19 else 0)
    return result


def _daily_snapshot(payload: dict) -> dict[str, list]:
    days = payload.get("days")
    if not isinstance(days, list) or len(days) < 7:
        raise RuntimeError("QWeather daily response is incomplete")
    result = {"time": [], "temperature_2m_min": [], "temperature_2m_max": [], "weather_code": [], "weather_text": [], "precipitation_probability_max": []}
    for item in days[:7]:
        daytime = item.get("daytime") or {}
        nighttime = item.get("nighttime") or {}
        rain_values = [
            _percent(_nested(daytime, "precipitation", "probability")),
            _percent(_nested(nighttime, "precipitation", "probability")),
        ]
        result["time"].append(_local_date(item["forecastStartTime"]))
        result["temperature_2m_min"].append(_number(_nested(item, "temperatureMin", "value")))
        result["temperature_2m_max"].append(_number(_nested(item, "temperatureMax", "value")))
        result["weather_code"].append(int(_number(_nested(daytime, "condition", "code"), -1)))
        result["weather_text"].append(str(_nested(daytime, "condition", "text", default="未知")))
        result["precipitation_probability_max"].append(max((v for v in rain_values if v is not None), default=None))
    return result


def fetch_forecast() -> dict:
    coordinates = f"/{LATITUDE:.2f}/{LONGITUDE:.2f}"
    hourly = _request("/weather/v1/hourly" + coordinates, {"hours": 24, "localTime": "true", "lang": "zh"})
    daily = _request("/weather/v1/daily" + coordinates, {"days": 7, "localTime": "true", "lang": "zh"})
    attributions = list(dict.fromkeys(
        list(_nested(hourly, "metadata", "attributions", default=[]) or [])
        + list(_nested(daily, "metadata", "attributions", default=[]) or [])
    ))
    return {
        "source": "qweather",
        "location": {"name": "杭州", "latitude": LATITUDE, "longitude": LONGITUDE},
        "updated_at": datetime.now(ZONE).isoformat(timespec="seconds"),
        "hourly": _hourly_snapshot(hourly),
        "daily": _daily_snapshot(daily),
        "attributions": attributions or ["https://developer.qweather.com/attribution.html"],
    }


def refresh_if_stale(max_age: int = REFRESH_SECONDS) -> bool:
    if OUT.is_file() and time.time() - OUT.stat().st_mtime < max_age:
        return False
    _load_env()
    snapshot = fetch_forecast()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=OUT.parent, delete=False) as handle:
        json.dump(snapshot, handle, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(handle.name)
    os.replace(temporary, OUT)
    return True


def main() -> None:
    try:
        changed = refresh_if_stale()
        print("[qweather] forecast refreshed" if changed else "[qweather] cached forecast is fresh", flush=True)
    except Exception as exc:
        print(f"[qweather] forecast refresh failed: {exc}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
