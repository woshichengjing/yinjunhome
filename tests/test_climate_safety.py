import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


if not hasattr(time, "tzset"):
    time.tzset = lambda: None


with mock.patch("os.makedirs"):
    from state_machines import climate_engine
    from state_machines import climate_intent
    from state_machines import climate_logger
    from state_machines import device_protection
    from state_machines import env_quality
    from state_machines import external_env


class ClimateDecisionTests(unittest.TestCase):
    def test_automatic_heating_mode_is_removed(self):
        mode = climate_engine._determine_mode(
            ["偏冷"], "cool", 18.0, 18.0, 25.0, 8.0)
        self.assertEqual(mode, "cool")
        self.assertFalse(climate_engine._automatic_cooling_allowed(["偏冷"]))
        self.assertFalse(climate_engine._automatic_cooling_allowed(["过冷"]))
        self.assertFalse(climate_engine._automatic_cooling_allowed(["舒适"]))
        self.assertFalse(climate_engine._automatic_cooling_allowed(["空气污浊"]))
        self.assertFalse(climate_engine._automatic_cooling_allowed(["偏湿"]))
        self.assertFalse(climate_engine._automatic_cooling_allowed(["偏冷", "过湿"]))
        self.assertTrue(climate_engine._automatic_cooling_allowed(["偏热"]))
        self.assertTrue(climate_engine._automatic_cooling_allowed(["过湿"]))
        self.assertEqual(climate_intent._automatic_power_request(["舒适"]), "hold")
        self.assertEqual(climate_intent._automatic_power_request(["偏湿"]), "hold")
        self.assertEqual(climate_intent._automatic_power_request(["偏冷", "过湿"]), "hold")
        self.assertEqual(climate_intent._automatic_power_request(["偏热"]), "on")
        self.assertEqual(climate_intent._automatic_power_request(["过湿"]), "on")

    def test_fresh_air_temperature_bounds_are_inclusive(self):
        cfg = {"fresh_temp_min": 20, "fresh_temp_max": 26}
        self.assertFalse(external_env._fresh_temp_is_eligible(None, cfg))
        self.assertFalse(external_env._fresh_temp_is_eligible(19.9, cfg))
        self.assertTrue(external_env._fresh_temp_is_eligible(20, cfg))
        self.assertTrue(external_env._fresh_temp_is_eligible(26, cfg))
        self.assertFalse(external_env._fresh_temp_is_eligible(26.1, cfg))

    def test_intent_is_primary_energy_source(self):
        self.assertTrue(climate_engine._energy_save_from_intent(
            {"purpose": "energy"}, False))
        self.assertFalse(climate_engine._energy_save_from_intent(
            {"purpose": "comfort"}, True))
        self.assertTrue(climate_engine._energy_save_from_intent({}, True))

    def test_suite_bath_activity_is_reflected_in_intent(self):
        now = int(time.time())
        room_state = {
            "generated_at": now,
            "rooms": {
                "br": {"activity": "empty"},
                "bath": {"activity": "occupied"},
            }
        }
        env_state = {"generated_at": now, "rooms": {}}

        def load_state(name):
            if name == "room_state.json":
                return room_state
            if name == "env_quality.json":
                return env_state
            return {}

        with tempfile.TemporaryDirectory() as state_dir:
            with mock.patch.object(climate_intent, "STATE_DIR", state_dir), \
                    mock.patch.object(climate_intent, "_load", side_effect=load_state), \
                    mock.patch.object(climate_intent, "_load_engine_config", return_value={"suite_bath": {"br": "bath"}}):
                Path(state_dir, "ac_disabled.json").write_text("{}", encoding="utf-8")
                Path(state_dir, "device_soft_off.json").write_text("{}", encoding="utf-8")
                result = climate_intent.run()
        self.assertEqual(result["intents"]["br"]["purpose"], "comfort")
        self.assertEqual(result["intents"]["br"]["occupancy"], "occupied")
        self.assertEqual(result["intents"]["br"]["power_request"], "hold")

    def test_energy_intents_never_request_power_on(self):
        now = int(time.time())
        room_state = {"generated_at": now, "rooms": {
            "br": {"activity": "occupied"},
            "st": {"activity": "empty"},
        }}
        env_state = {"generated_at": now, "rooms": {
            "br": {"condition": ["跑温", "偏热"], "readings": {}, "thresholds": {}},
            "st": {"condition": ["舒适"], "readings": {}, "thresholds": {}},
        }}

        def load_state(name):
            if name == "room_state.json":
                return room_state
            if name == "env_quality.json":
                return env_state
            return {}

        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(climate_intent, "STATE_DIR", state_dir), \
                mock.patch.object(climate_intent, "_load", side_effect=load_state):
            Path(state_dir, "ac_disabled.json").write_text("{}", encoding="utf-8")
            Path(state_dir, "device_soft_off.json").write_text("{}", encoding="utf-8")
            result = climate_intent.run()

        self.assertEqual(result["intents"]["br"]["reason"], "occupied_runaway")
        self.assertEqual(result["intents"]["br"]["power_request"], "hold")
        self.assertEqual(result["intents"]["st"]["reason"], "energy_empty")
        self.assertEqual(result["intents"]["st"]["power_request"], "hold")

    def test_stale_core_input_produces_hold_intent(self):
        stale = int(time.time()) - climate_intent.INPUT_MAX_AGE_SECONDS - 1

        def load_state(name):
            if name == "room_state.json":
                return {"generated_at": stale, "rooms": {"br": {"activity": "occupied"}}}
            if name == "env_quality.json":
                return {"generated_at": int(time.time()), "rooms": {}}
            return {}

        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(climate_intent, "STATE_DIR", state_dir), \
                mock.patch.object(climate_intent, "_load", side_effect=load_state):
            result = climate_intent.run()

        self.assertTrue(all(
            intent["purpose"] == "input_stale" and intent["power_request"] == "hold"
            for intent in result["intents"].values()
        ))

    def test_legacy_timestamp_is_accepted_during_service_rollout(self):
        payload = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self.assertTrue(climate_engine._is_fresh(payload))
        self.assertTrue(climate_intent._is_fresh(payload))

    def test_engine_refreshes_switch_and_last_changed_from_ha(self):
        changed = "2026-09-09T12:00:00+08:00"
        devices = {"br_ac": {"switch": "on", "last_changed": "stale"}}
        with mock.patch.object(climate_engine, "get_state_full", return_value={
            "state": "off", "last_changed": changed,
        }):
            known = climate_engine._refresh_ac_states(["br_ac"], devices)
        self.assertTrue(known)
        self.assertEqual(devices["br_ac"]["switch"], "off")
        self.assertEqual(devices["br_ac"]["last_changed"], changed)

    def test_recent_live_manual_off_is_not_overwritten(self):
        now = int(time.time())
        changed = datetime.fromtimestamp(now).astimezone().isoformat()
        config = dict(climate_engine.DEFAULTS, rooms=["br"])

        def load_state(name):
            payloads = {
                "room_state.json": {"generated_at": now, "rooms": {"br": {"activity": "occupied"}}},
                "env_quality.json": {"generated_at": now, "rooms": {"br": {
                    "condition": ["偏热", "不适宜"],
                    "comfort": "不适宜",
                    "readings": {"temp": 28.5, "hum": 50, "at": 28.5},
                    "thresholds": {"at_max": 27.5, "temp_max": 28},
                }}},
                "climate_intent.json": {"generated_at": now, "intents": {"br": {
                    "purpose": "comfort", "comfort_target": 27.5,
                }}},
                "external_env.json": {"generated_at": now, "fresh_eligible": False,
                                      "current": {"temp": 30, "abs_humidity": 15}},
                "device_protection.json": {"devices": {"br_ac": {
                    "switch": "on", "climate_state": "cool", "ac_set_temp": "27",
                }}},
            }
            return payloads.get(name, {})

        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(climate_engine, "STATE_DIR", state_dir), \
                mock.patch.object(climate_engine, "_cfg", return_value=config), \
                mock.patch.object(climate_engine, "_load_json", side_effect=load_state), \
                mock.patch.object(climate_engine, "get_state", return_value="off"), \
                mock.patch.object(climate_engine, "get_attr", return_value=""), \
                mock.patch.object(climate_engine, "get_state_full", return_value={
                    "state": "off", "last_changed": changed,
                }), \
                mock.patch.object(climate_engine, "_write_snapshot"), \
                mock.patch.object(climate_engine, "_act") as act:
            Path(state_dir, "ac_disabled.json").write_text("{}", encoding="utf-8")
            Path(state_dir, "device_soft_off.json").write_text("{}", encoding="utf-8")
            result = climate_engine.run()

        act.assert_not_called()
        self.assertEqual(result["rooms"]["br"]["decision"], "关机未满30分钟→不控")

    def test_fresh_air_is_tried_before_starting_air_conditioner(self):
        now = int(time.time())
        changed = datetime.fromtimestamp(now - 3600).astimezone().isoformat()
        config = dict(climate_engine.DEFAULTS, rooms=["br"])

        def load_state(name):
            payloads = {
                "room_state.json": {"generated_at": now, "rooms": {"br": {"activity": "occupied"}}},
                "env_quality.json": {"generated_at": now, "rooms": {"br": {
                    "condition": ["偏热", "不适宜"],
                    "comfort": "不适宜",
                    "readings": {"temp": 28.5, "hum": 60, "at": 28.5},
                    "thresholds": {"at_max": 27.5, "temp_max": 28},
                }}},
                "climate_intent.json": {"generated_at": now, "intents": {"br": {
                    "purpose": "comfort", "comfort_target": 27.5,
                }}},
                "external_env.json": {"generated_at": now, "fresh_eligible": True,
                                      "current": {"temp": 22, "abs_humidity": 10}},
                "device_protection.json": {"devices": {"br_ac": {
                    "switch": "off", "climate_state": "off", "ac_set_temp": "27",
                }}},
            }
            return payloads.get(name, {})

        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(climate_engine, "STATE_DIR", state_dir), \
                mock.patch.object(climate_engine, "_cfg", return_value=config), \
                mock.patch.object(climate_engine, "_load_json", side_effect=load_state), \
                mock.patch.object(climate_engine, "get_state", return_value="off"), \
                mock.patch.object(climate_engine, "get_attr", return_value=""), \
                mock.patch.object(climate_engine, "get_state_full", return_value={
                    "state": "off", "last_changed": changed,
                }), \
                mock.patch.object(climate_engine.time, "time", return_value=now) as clock, \
                mock.patch.object(climate_engine, "_write_snapshot"), \
                mock.patch.object(climate_engine, "_act", return_value="on") as act:
            Path(state_dir, "ac_disabled.json").write_text("{}", encoding="utf-8")
            Path(state_dir, "device_soft_off.json").write_text("{}", encoding="utf-8")
            result = climate_engine.run()

            act.assert_any_call(climate_engine.FRESH_DEV, "on")
            self.assertFalse(any(call.args and call.args[0] == "br_ac" for call in act.call_args_list))
            self.assertEqual(result["rooms"]["br"]["decision"], "新风优先观察→空调保持关")

            # 新风持续未确认启动时，不重置首次尝试时间；超过宽限期由 AC 兜底。
            act.reset_mock()
            clock.return_value = now + config["auto_start_confirm_min"] * 60
            climate_engine.run()

        act.assert_any_call(climate_engine.FRESH_DEV, "on")
        self.assertTrue(any(call.args and call.args[0] == "br_ac" for call in act.call_args_list))

    def test_disabled_marker_waits_for_confirmed_off(self):
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(climate_engine, "STATE_DIR", state_dir), \
                mock.patch.object(climate_engine, "_act") as act:
            decision = climate_engine._handle_disabled_room(
                "br", ["br_ac"], {"br_ac": {"switch": "unknown"}})
            marker = Path(state_dir, "engine_br_disabled_done")
            self.assertEqual(decision, "禁用状态未知→待确认")
            self.assertFalse(marker.exists())
            act.assert_not_called()

            decision = climate_engine._handle_disabled_room(
                "br", ["br_ac"], {"br_ac": {"switch": "on"}})
            self.assertEqual(decision, "禁用→关机待确认")
            self.assertFalse(marker.exists())
            act.assert_called_once_with("br_ac", "off")

            decision = climate_engine._handle_disabled_room(
                "br", ["br_ac"], {"br_ac": {"switch": "off"}})
            self.assertEqual(decision, "已禁用")
            self.assertTrue(marker.is_file())

    def test_stale_core_input_blocks_all_automatic_actions(self):
        now = int(time.time())
        stale = now - climate_engine.INPUT_MAX_AGE_SECONDS - 1
        config = dict(climate_engine.DEFAULTS, rooms=["br"])

        def load_state(name):
            payloads = {
                "room_state.json": {"generated_at": stale, "rooms": {"br": {"activity": "occupied"}}},
                "env_quality.json": {"generated_at": now, "rooms": {"br": {
                    "condition": ["过热"], "readings": {"temp": 31, "hum": 70, "at": 32},
                }}},
                "climate_intent.json": {"generated_at": now, "intents": {"br": {
                    "purpose": "comfort", "comfort_target": 27.5,
                }}},
                "external_env.json": {"generated_at": now, "fresh_eligible": True,
                                      "current": {"temp": 22, "abs_humidity": 10}},
            }
            return payloads.get(name, {})

        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(climate_engine, "STATE_DIR", state_dir), \
                mock.patch.object(climate_engine, "_cfg", return_value=config), \
                mock.patch.object(climate_engine, "_load_json", side_effect=load_state), \
                mock.patch.object(climate_engine, "get_state", return_value="off"), \
                mock.patch.object(climate_engine, "get_attr", return_value=""), \
                mock.patch.object(climate_engine, "get_state_full", return_value={
                    "state": "off", "last_changed": "2026-09-09T00:00:00+08:00",
                }), \
                mock.patch.object(climate_engine, "_write_snapshot"), \
                mock.patch.object(climate_engine, "_act") as act:
            result = climate_engine.run()

        act.assert_not_called()
        self.assertFalse(result["inputs"]["fresh"])
        self.assertEqual(result["rooms"]["br"]["decision"], "输入数据过期→不控")
        self.assertEqual(result["fresh"]["decision"], "输入数据过期→不控")
        self.assertEqual(result["dehum"]["decision"], "输入数据过期→不控")

    def test_low_humidity_cold_effect_never_adds_wet_badge(self):
        temp_eid = env_quality.ROOM_SENSORS["br"]["temp"]
        hum_eid = env_quality.ROOM_SENSORS["br"]["hum"]
        co2_eid = env_quality.ROOM_SENSORS["br"]["co2"]

        def state(entity_id):
            if entity_id == temp_eid:
                return "24.5"
            if entity_id == hum_eid:
                return "20"
            if entity_id == co2_eid:
                return "500"
            return "off"

        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(env_quality, "STATE_DIR", state_dir), \
                mock.patch.object(env_quality, "load_room_activity", return_value="occupied"), \
                mock.patch.object(env_quality, "_get_season", return_value="summer"), \
                mock.patch.object(env_quality, "get_state", side_effect=state):
            result = env_quality.evaluate_room("br")

        self.assertEqual(result["contributor"], "湿度偏低")
        self.assertIn("过干", result["condition"])
        self.assertNotIn("偏湿", result["condition"])

    def test_missing_outdoor_data_never_enables_fresh_air(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with mock.patch.object(external_env, "STATE_DIR", state_dir), \
                    mock.patch.object(external_env, "get_state", return_value=""), \
                    mock.patch.object(external_env, "load_config", return_value={}):
                result = external_env.run()
        self.assertFalse(result["fresh_eligible"])
        self.assertIsNone(result["current"]["apparent_temp"])


class DeviceExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state_dir = self.temporary.name
        self.state_patch = mock.patch.object(device_protection, "STATE_DIR", self.state_dir)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.temporary.cleanup()

    def _queue(self, dev_id="br_ac", action="on", **kwargs):
        device_protection.submit_action(dev_id, action, **kwargs)
        return device_protection._load_action(dev_id)

    def test_failed_command_stays_queued_without_protection_stamp(self):
        action = self._queue(temp=27, mode="cool")
        with mock.patch.object(device_protection, "DRY_RUN", False), \
                mock.patch.object(device_protection, "ha_post", return_value=False), \
                mock.patch.object(device_protection.time, "sleep"):
            executed = device_protection._execute_action(
                "br_ac", {"climate": "climate.test"}, action)
        self.assertFalse(executed)
        self.assertTrue(Path(self.state_dir, "br_ac_action.json").is_file())
        self.assertFalse(Path(self.state_dir, "br_ac_lastexec").exists())

    def test_confirmed_command_is_cleared_and_stamped(self):
        action = self._queue(temp=27, mode="cool")
        with mock.patch.object(device_protection, "DRY_RUN", False), \
                mock.patch.object(device_protection, "ha_post", return_value=True), \
                mock.patch.object(device_protection, "_verify_climate_action", return_value=True), \
                mock.patch.object(device_protection.time, "sleep"):
            executed = device_protection._execute_action(
                "br_ac", {"climate": "climate.test"}, action)
        self.assertTrue(executed)
        self.assertFalse(Path(self.state_dir, "br_ac_action.json").exists())
        self.assertTrue(Path(self.state_dir, "br_ac_lastexec").is_file())

    def test_dry_run_consumes_simulated_command_without_stamp(self):
        action = self._queue(temp=27, mode="cool")
        with mock.patch.object(device_protection, "DRY_RUN", True):
            executed = device_protection._execute_action(
                "br_ac", {"climate": "climate.test"}, action)
        self.assertTrue(executed)
        self.assertFalse(Path(self.state_dir, "br_ac_action.json").exists())
        self.assertFalse(Path(self.state_dir, "br_ac_lastexec").exists())

    def test_expired_command_is_discarded(self):
        path = Path(self.state_dir, "br_ac_action.json")
        path.write_text(json.dumps({
            "action": "on",
            "ts": int(time.time()) - device_protection.ACTION_MAX_AGE_SECONDS - 1,
        }), encoding="utf-8")
        self.assertIsNone(device_protection._load_action("br_ac"))
        self.assertFalse(path.exists())

    def test_uncomfortable_period_does_not_start_comfort_session(self):
        with mock.patch.object(climate_engine, "STATE_DIR", self.state_dir):
            climate_engine._log_comfort_session(
                "br", int(time.time()), False, ["br_ac"], False, False)
        self.assertFalse(Path(self.state_dir, "engine_br_comfort_since").exists())


class ClimateAuditTests(unittest.TestCase):
    def test_device_execution_change_is_logged(self):
        with tempfile.TemporaryDirectory() as state_dir:
            root = Path(state_dir)
            intent = {"intents": {"br": {"purpose": "comfort"}}}
            mode = {"modes": {"br": "舒适"}}
            device = {"devices": {"br_ac": {
                "state": "running", "climate_state": "cool",
                "ac_set_temp": "27", "next_action": "无",
                "restart_blocked": False,
            }}}
            for name, payload in (
                    ("climate_intent.json", intent),
                    ("room_mode.json", mode),
                    ("device_protection.json", device),
                    ("env_quality.json", {}),
                    ("room_state.json", {})):
                root.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")
            log_file = root / "climate_decision.jsonl"
            snapshot = root / "logger_snapshot.json"
            with mock.patch.object(climate_logger, "STATE_DIR", state_dir), \
                    mock.patch.object(climate_logger, "LOG_FILE", str(log_file)), \
                    mock.patch.object(climate_logger, "SNAPSHOT_FILE", str(snapshot)):
                self.assertEqual(climate_logger.run(), 1)
                self.assertEqual(climate_logger.run(), 0)
                device["devices"]["br_ac"]["next_action"] = "已执行: 设置 26°C"
                root.joinpath("device_protection.json").write_text(
                    json.dumps(device), encoding="utf-8")
                self.assertEqual(climate_logger.run(), 1)


if __name__ == "__main__":
    unittest.main()
