import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


if not hasattr(time, "tzset"):
    time.tzset = lambda: None


with mock.patch("os.makedirs"):
    from state_machines import climate_engine
    from state_machines import climate_intent
    from state_machines import climate_logger
    from state_machines import device_protection
    from state_machines import external_env


class ClimateDecisionTests(unittest.TestCase):
    def test_automatic_heating_mode_is_removed(self):
        mode = climate_engine._determine_mode(
            ["偏冷"], "cool", 18.0, 18.0, 25.0, 8.0)
        self.assertEqual(mode, "cool")
        self.assertFalse(climate_engine._automatic_cooling_allowed(["偏冷"]))
        self.assertFalse(climate_engine._automatic_cooling_allowed(["过冷"]))
        self.assertTrue(climate_engine._automatic_cooling_allowed(["舒适"]))

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
        room_state = {
            "rooms": {
                "br": {"activity": "empty"},
                "bath": {"activity": "occupied"},
            }
        }

        def load_state(name):
            return room_state if name == "room_state.json" else {}

        with tempfile.TemporaryDirectory() as state_dir:
            with mock.patch.object(climate_intent, "STATE_DIR", state_dir), \
                    mock.patch.object(climate_intent, "_load", side_effect=load_state), \
                    mock.patch.object(climate_intent, "_load_engine_config", return_value={"suite_bath": {"br": "bath"}}):
                result = climate_intent.run()
        self.assertEqual(result["intents"]["br"]["purpose"], "comfort")
        self.assertEqual(result["intents"]["br"]["occupancy"], "occupied")

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
