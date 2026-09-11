"""按分钟回放真实决策、队列和执行边界；所有 HA 访问均被隔离。"""
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

if not hasattr(time, "tzset"):
    time.tzset = lambda: None

with mock.patch("os.makedirs"):
    from state_machines import climate_engine as engine
    from state_machines import climate_intent as intent
    from state_machines import device_protection as devices
    from state_machines import climate_policy as policy
    from state_machines import env_quality


class ClimateScenarioTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.write("ac_disabled.json", {})
        self.write("device_soft_off.json", {})
        self.now = datetime(2026, 9, 11, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.start = self.now
        self.cfg = dict(engine.DEFAULTS, rooms=["br"])
        self.activities = {"br": {"activity": "occupied"}}
        self.env = {"br": self.environment()}
        self.external = {"fresh_eligible": False, "current": {"temp": 32, "abs_humidity": 20}}
        self.fresh = "off"
        self.live = {d: {"state": "off", "last_changed": self.changed(-3600),
                         "attributes": {"temperature": 27, "current_temperature": 28,
                                        "fan_mode": "低风"}} for d in devices.DEVICES if d.endswith("_ac")}
        for module in (engine, intent, devices):
            self.stack.enter_context(mock.patch.object(module, "STATE_DIR", str(self.root)))
        self.stack.enter_context(mock.patch.object(time, "time", side_effect=lambda: self.now))
        owner = self

        class ShanghaiClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(owner.now, tz or timezone(timedelta(hours=8)))

        for module in (engine, intent, devices):
            self.stack.enter_context(mock.patch.object(module, "datetime", ShanghaiClock))
        self.stack.enter_context(mock.patch.object(engine, "_cfg", side_effect=lambda: self.cfg))
        self.stack.enter_context(mock.patch.object(intent, "_load_engine_config", side_effect=lambda: self.cfg))
        self.stack.enter_context(mock.patch.object(engine, "_write_snapshot"))
        self.stack.enter_context(mock.patch.object(engine, "_log_comfort_session"))
        self.stack.enter_context(mock.patch.object(engine, "get_state", side_effect=lambda eid: self.fresh if eid == engine.FRESH_SWITCH else "off"))
        self.stack.enter_context(mock.patch.object(engine, "get_attr", return_value=""))
        self.stack.enter_context(mock.patch.object(engine, "get_state_full", side_effect=self.full_state))
        self.stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=AssertionError("HA access forbidden in tests")))

    def changed(self, seconds=0):
        return datetime.fromtimestamp(self.now + seconds, timezone(timedelta(hours=8))).isoformat()

    def full_state(self, eid):
        for dev, definition in devices.DEVICES.items():
            if definition.get("climate") == eid:
                return self.live.get(dev, {})
        return {}

    @staticmethod
    def environment(conditions=None, at=29, comfort="不适宜"):
        return {"condition": conditions or ["偏热", "不适宜"], "comfort": comfort,
                "readings": {"temp": at, "hum": 60, "at": at},
                "thresholds": {"at_max": 27.5, "temp_max": 26.5}}

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")

    def running(self, mode="cool", action="idle", current=26, target=28):
        self.live["br_ac"].update(state=mode)
        self.live["br_ac"]["attributes"].update(
            temperature=target, current_temperature=current, hvac_action=action)

    def tick(self, seconds=0, override_intent=None, stale=False):
        self.now += seconds
        generated = self.now - 181 if stale else self.now
        self.write("room_state.json", {"generated_at": generated, "rooms": self.activities})
        self.write("env_quality.json", {"generated_at": generated, "rooms": self.env})
        self.write("external_env.json", dict(self.external, generated_at=self.now))
        intent.run()
        if override_intent:
            self.write("climate_intent.json", {"generated_at": self.now, "intents": override_intent})
        return engine.run()

    def queued(self, device="br_ac"):
        return devices._load_action(device)

    def assert_no_start(self):
        action = self.queued()
        self.assertTrue(action is None or action["action"] == "off", action)

    def test_runaway_vetoes_sleep_and_stale_comfort_intent(self):
        for activity in ("occupied", "sleeping", "napping", "entering", "empty"):
            with self.subTest(activity=activity):
                self.activities["br"]["activity"] = activity
                self.env["br"] = self.environment(["跑温", "偏热", "不适宜"])
                self.tick(60, override_intent={"br": {"purpose": "comfort", "power_request": "on"}})
                self.assert_no_start()
                produced = intent.run()["intents"]["br"]
                self.assertEqual(produced["purpose"], "energy")
                self.assertEqual(produced["power_request"], "hold")

    def test_runaway_stops_running_dry_without_overwriting_off(self):
        self.running(mode="dry")
        self.env["br"] = self.environment(["跑温", "舒适"], 27, "适宜")
        result = self.tick()
        self.assertEqual(self.queued()["action"], "off")
        self.assertIn("跑温", result["rooms"]["br"]["decision"])

    def test_shared_space_and_guest_cannot_bypass_runaway(self):
        self.cfg["rooms"] = ["lr", "dr"]
        self.activities = {"lr": {"activity": "empty"}, "dr": {"activity": "occupied"}}
        self.env = {"lr": self.environment(), "dr": self.environment(["跑温", "偏热"])}
        self.now += 11 * 3600
        (self.root / "guest_mode_today").write_text("1", encoding="utf-8")
        self.tick()
        for room in ("lr", "dr"):
            self.assertIsNone(self.queued(room + "_ac"))
            self.assertEqual(intent.run()["intents"][room]["purpose"], "energy")

    def test_comfortable_hot_badges_never_start(self):
        self.env["br"] = self.environment(["偏热", "过湿", "舒适"], 27, "适宜")
        for _ in range(5):
            self.tick(60)
            self.assert_no_start()

    def test_cold_wet_and_transient_occupancy_never_start(self):
        for activity, conditions in (("occupied", ["偏冷", "过湿"]),
                                     ("entering", ["过热"]), ("empty", ["过热"]),
                                     ("unknown", ["过热"]), ("unexpected", ["过热"])):
            with self.subTest(activity=activity, conditions=conditions):
                self.activities["br"]["activity"] = activity
                self.env["br"] = self.environment(conditions)
                for _ in range(5):
                    self.tick(60)
                    self.assert_no_start()

    def test_real_demand_must_persist_then_only_one_on_command(self):
        self.tick()
        for _ in range(2):
            self.tick(60)
            self.assert_no_start()
        self.tick(60)
        self.assertEqual(self.queued()["action"], "on")
        self.assertEqual(self.queued()["mode"], "cool")

    def test_demand_disappearance_cancels_queued_on(self):
        self.tick()
        for _ in range(3):
            self.tick(60)
        self.assertEqual(self.queued()["action"], "on")
        self.env["br"] = self.environment(["舒适"], 27, "适宜")
        self.tick(60)
        self.assertIsNone(self.queued())
        self.env["br"] = self.environment()
        self.tick(60)
        self.assertIsNone(self.queued())

    def test_comfortable_continuous_idle_stops_at_30_minutes(self):
        self.running()
        self.env["br"] = self.environment(["偏热", "舒适"], 27, "适宜")
        self.tick()
        for minute in range(1, 31):
            result = self.tick(60)
            if minute < 30:
                action = self.queued()
                self.assertTrue(action is None or action["action"] == "set")
        self.assertEqual(self.queued()["action"], "off")
        self.assertEqual(result["rooms"]["br"]["control"]["unloaded_minutes"], 30)

    def test_loading_or_reheating_resets_idle_clock_and_queued_off(self):
        self.running()
        self.env["br"] = self.environment(["舒适"], 27, "适宜")
        self.tick()
        for _ in range(30):
            self.tick(60)
        self.assertEqual(self.queued()["action"], "off")
        self.live["br_ac"]["attributes"]["hvac_action"] = "cooling"
        result = self.tick(60)
        self.assertIsNone(self.queued())
        self.assertEqual(result["rooms"]["br"]["control"]["unloaded_minutes"], 0)
        self.live["br_ac"]["attributes"]["hvac_action"] = "idle"
        self.env["br"] = self.environment()
        result = self.tick(60)
        self.assertNotEqual(self.queued()["action"], "off")
        self.assertEqual(result["rooms"]["br"]["control"]["unloaded_minutes"], 0)

    def test_data_gap_restarts_idle_evidence(self):
        self.running()
        self.env["br"] = self.environment(["舒适"], 27, "适宜")
        self.tick()
        for _ in range(29):
            self.tick(60)
        result = self.tick(600)
        self.assertIsNone(self.queued())
        self.assertEqual(result["rooms"]["br"]["control"]["unloaded_minutes"], 0)

    def test_new_device_session_restarts_idle_evidence(self):
        self.running()
        self.env["br"] = self.environment(["舒适"], 27, "适宜")
        self.tick()
        for _ in range(29):
            self.tick(60)
        self.live["br_ac"]["last_changed"] = self.changed()
        result = self.tick(60)
        self.assertIsNone(self.queued())
        self.assertEqual(result["rooms"]["br"]["control"]["unloaded_minutes"], 0)

    def test_unloaded_estimate_does_not_override_real_cooling(self):
        self.running(action="cooling", current=26, target=28)
        self.assertFalse(engine._is_unloaded(dict(climate_state="cool", hvac_action="cooling", ac_cur_temp=26, ac_set_temp=28)))
        self.assertTrue(engine._is_unloaded(dict(climate_state="cool", ac_cur_temp=26, ac_set_temp=28)))
        self.assertFalse(engine._is_unloaded(dict(climate_state="cool", ac_cur_temp="nan", ac_set_temp=28)))

    def test_manual_heat_and_fan_only_are_untouched(self):
        for mode in ("heat", "fan_only", "auto"):
            self.running(mode=mode)
            self.env["br"] = self.environment(["跑温", "过热"])
            self.tick(60)
            self.assertIsNone(self.queued())

    def test_missing_or_recent_last_changed_blocks_start(self):
        for changed in ("", "invalid", self.changed(), self.changed(3600)):
            self.live["br_ac"]["last_changed"] = changed
            for _ in range(5):
                self.tick(60)
                self.assert_no_start()

    def test_stale_inputs_clear_queued_engine_but_keep_manual(self):
        devices.submit_action("br_ac", "on", source="engine")
        self.tick(stale=True)
        self.assertIsNone(self.queued())
        devices.submit_action("br_ac", "off", source="manual")
        self.tick(stale=True)
        self.assertEqual(self.queued()["source"], "manual")

    def test_manual_queue_has_priority_over_engine(self):
        devices.submit_action("br_ac", "off", source="manual")
        for _ in range(5):
            self.tick(60)
        self.assertEqual(self.queued()["action"], "off")
        self.assertEqual(self.queued()["source"], "manual")

    def test_engine_set_cannot_restart_after_manual_off(self):
        devices.submit_action("br_ac", "set", mode="cool", temp=27, source="engine")
        with mock.patch.object(devices, "_ha_req", return_value=self.live["br_ac"]), \
                mock.patch.object(devices, "ha_post") as post:
            self.assertFalse(devices._execute_action("br_ac", devices.DEVICES["br_ac"], self.queued()))
        post.assert_not_called()
        self.assertIsNone(self.queued())

    def test_execution_rechecks_manual_off_and_mode(self):
        for state in ("off", "heat", "unavailable"):
            devices.submit_action("br_ac", "on", mode="cool", source="engine")
            live = {"state": state, "last_changed": self.changed()}
            with mock.patch.object(devices, "_ha_req", return_value=live), \
                    mock.patch.object(devices, "ha_post") as post:
                self.assertFalse(devices._execute_action("br_ac", devices.DEVICES["br_ac"], self.queued()))
            post.assert_not_called()

    def test_queued_off_respects_manual_mode_unless_room_disabled(self):
        devices.submit_action("br_ac", "off", source="engine")
        with mock.patch.object(devices, "_ha_req", return_value={"state": "heat"}), \
                mock.patch.object(devices, "ha_post") as post:
            self.assertFalse(devices._execute_action("br_ac", devices.DEVICES["br_ac"], self.queued()))
        post.assert_not_called()
        self.write("ac_disabled.json", {"br": True})
        devices.submit_action("br_ac", "off", source="engine")
        with mock.patch.object(devices, "_ha_req", return_value={"state": "heat"}), \
                mock.patch.object(devices, "DRY_RUN", False), \
                mock.patch.object(devices, "ha_post", return_value=True) as post, \
                mock.patch.object(devices, "_verify_climate_action", return_value=True), \
                mock.patch.object(time, "sleep"):
            self.assertTrue(devices._execute_action("br_ac", devices.DEVICES["br_ac"], self.queued()))
        post.assert_called_once_with("climate/turn_off", {"entity_id": devices.DEVICES["br_ac"]["climate"]})

    def test_unloaded_off_waits_for_device_protection(self):
        self.running()
        self.env["br"] = self.environment(["舒适"], 27, "适宜")
        self.tick()
        for _ in range(30):
            self.tick(60)
        self.assertEqual(self.queued()["action"], "off")
        with mock.patch.object(devices, "get_state", return_value="cool"), \
                mock.patch.object(devices, "get_attrs", return_value=self.live["br_ac"]["attributes"]), \
                mock.patch.object(devices, "get_last_changed", return_value=self.changed(-3600)), \
                mock.patch.object(devices, "load_config", return_value={}), \
                mock.patch.object(devices, "_protection", return_value=(True, 5, self.now + 300)), \
                mock.patch.object(devices, "_execute_action") as execute:
            result = devices.evaluate_device("br_ac", devices.DEVICES["br_ac"])
        execute.assert_not_called()
        self.assertIn("等待执行", result["next_action"])
        self.assertEqual(self.queued()["action"], "off")

    def test_soft_off_and_disabled_never_start(self):
        self.write("device_soft_off.json", {"br_ac": True})
        for _ in range(5):
            self.tick(60)
            self.assertIsNone(self.queued())
        self.write("device_soft_off.json", {})
        self.write("ac_disabled.json", {"br": True})
        for _ in range(5):
            self.tick(60)
            self.assertIsNone(self.queued())

    def test_closed_switch_blocks_submission_and_discards_old_on_and_set(self):
        for filename, flags in (("ac_disabled.json", {"br": True}),
                                ("device_soft_off.json", {"br_ac": True})):
            for action in ("on", "set"):
                with self.subTest(file=filename, action=action):
                    self.write("ac_disabled.json", {})
                    self.write("device_soft_off.json", {})
                    self.assertTrue(devices.submit_action("br_ac", action, source="engine"))
                    self.write(filename, flags)
                    self.assertIsNone(self.queued())
                    self.assertFalse(devices.submit_action("br_ac", action, source="engine", force=True))
                    self.assertFalse((self.root / "br_ac_action.json").exists())

    def test_switch_closed_after_queue_read_prevents_execution(self):
        for filename, flags in (("ac_disabled.json", {"br": True}),
                                ("device_soft_off.json", {"br_ac": True})):
            self.write("ac_disabled.json", {})
            self.write("device_soft_off.json", {})
            devices.submit_action("br_ac", "on", source="engine")
            action = self.queued()
            self.write(filename, flags)
            with mock.patch.object(devices, "_ha_req") as request, \
                    mock.patch.object(devices, "ha_post") as post:
                self.assertFalse(devices._execute_action("br_ac", devices.DEVICES["br_ac"], action))
            request.assert_not_called()
            post.assert_not_called()

    def test_switch_closed_during_live_ha_read_prevents_start(self):
        devices.submit_action("br_ac", "on", source="engine")
        action = self.queued()

        def live_read(*args):
            self.write("device_soft_off.json", {"br_ac": True})
            return self.live["br_ac"]

        with mock.patch.object(devices, "_ha_req", side_effect=live_read), \
                mock.patch.object(devices, "ha_post") as post:
            self.assertFalse(devices._execute_action("br_ac", devices.DEVICES["br_ac"], action))
        post.assert_not_called()

    def test_missing_or_invalid_switch_file_does_not_restore_auto_control(self):
        for filename in ("ac_disabled.json", "device_soft_off.json"):
            for content in (None, "{", "[]", '{"br": "false", "br_ac": "false"}'):
                with self.subTest(file=filename, content=content):
                    self.write("ac_disabled.json", {})
                    self.write("device_soft_off.json", {})
                    if content is None:
                        (self.root / filename).unlink()
                    else:
                        (self.root / filename).write_text(content, encoding="utf-8")
                    result = self.tick(60)
                    self.assertIsNone(self.queued())
                    self.assertFalse(devices.submit_action("br_ac", "on", source="engine"))
                    self.assertIn("开关状态未知", result["rooms"]["br"]["decision"])

    def test_device_soft_off_also_blocks_fresh_air_and_dehumidifier(self):
        for device in ("fresh_air", "br_dehum"):
            self.write("device_soft_off.json", {device: True})
            self.assertFalse(devices.submit_action(device, "on", source="engine"))
            self.assertIsNone(self.queued(device))
            self.assertTrue(devices.submit_action(device, "off", source="engine"))
            self.assertEqual(self.queued(device)["action"], "off")

    def test_switch_snapshot_reports_controller_observation(self):
        self.write("ac_disabled.json", {"br": True})
        self.write("device_soft_off.json", {"br_ac": False})
        with mock.patch.object(devices, "get_state", return_value="off"), \
                mock.patch.object(devices, "get_attrs", return_value={}), \
                mock.patch.object(devices, "get_last_changed", return_value=self.changed(-3600)), \
                mock.patch.object(devices, "load_config", return_value={}), \
                mock.patch.object(devices, "_protection", return_value=(False, 0, 0)):
            result = devices.evaluate_device("br_ac", devices.DEVICES["br_ac"])
        control = result["control_switches"]
        self.assertTrue(control["room_disabled"])
        self.assertFalse(control["device_soft_off"])
        self.assertFalse(control["automation_allowed"])
        self.assertEqual(control["observed_at"], int(self.now))

    def test_explicit_reenable_requires_new_demand_confirmation(self):
        self.tick()
        for _ in range(3):
            self.tick(60)
        self.assertEqual(self.queued()["action"], "on")
        self.write("device_soft_off.json", {"br_ac": True})
        self.tick(60)
        self.assertIsNone(self.queued())
        self.write("device_soft_off.json", {"br_ac": False})
        self.tick(60)
        self.assertIsNone(self.queued())
        for _ in range(3):
            self.tick(60)
        self.assertEqual(self.queued()["action"], "on")

    def test_guest_expires_at_end_of_same_night(self):
        self.now += 11 * 3600
        path = self.root / "guest_mode_today"
        path.write_text("1", encoding="utf-8")
        os.utime(path, (self.now, self.now))
        now = datetime.fromtimestamp(self.now, timezone(timedelta(hours=8)))
        self.assertTrue(policy.guest_active(str(self.root), now))
        self.assertTrue(policy.guest_active(str(self.root), now + timedelta(hours=7)))
        self.assertFalse(policy.guest_active(str(self.root), now + timedelta(hours=8)))
        self.assertFalse(policy.guest_active(str(self.root), now + timedelta(days=1)))

    def test_cold_comfort_contributor_does_not_invent_hot_badge(self):
        readings = {env_quality.ROOM_SENSORS["br"]["temp"]: "19",
                    env_quality.ROOM_SENSORS["br"]["hum"]: "60",
                    env_quality.ROOM_SENSORS["br"]["co2"]: "500"}
        with mock.patch.object(env_quality, "STATE_DIR", str(self.root)), \
                mock.patch.object(env_quality, "get_state", side_effect=lambda eid: readings.get(eid, "off")), \
                mock.patch.object(env_quality, "load_room_activity", return_value="occupied"), \
                mock.patch.object(env_quality, "_get_season", return_value="summer"):
            result = env_quality.evaluate_room("br")
        self.assertIn("过冷", result["condition"])
        self.assertNotIn("偏热", result["condition"])

    def test_sleep_window_keeps_target_and_does_not_start_when_comfortable(self):
        self.activities["br"]["activity"] = "sleeping"
        self.env["br"] = self.environment(["偏热", "舒适"], 28, "适宜")
        self.now -= 8 * 3600
        self.tick()
        self.assertEqual(intent.run()["intents"]["br"]["comfort_target"], 28.5)
        self.assertIsNone(self.queued())

    def test_old_automatic_queue_expires_after_3_minutes(self):
        devices.submit_action("br_ac", "on", source="engine")
        self.now += 181
        self.assertIsNone(self.queued())

    def test_fresh_air_is_per_room_and_respects_exact_two_degrees(self):
        self.cfg["rooms"] = ["br", "st"]
        self.activities["st"] = {"activity": "occupied"}
        self.env["br"] = self.environment(at=28)
        self.env["st"] = self.environment(at=27.8)
        self.external = {"fresh_eligible": True, "current": {"temp": 26, "abs_humidity": 0.01}}
        self.fresh = "on"
        result = self.tick()
        self.assertIn("br", result["fresh"]["holding"])
        self.assertNotIn("st", result["fresh"]["holding"])
        for _ in range(30):
            result = self.tick(60)
        self.assertNotIn("br", result["fresh"]["holding"])
        self.assertEqual(self.queued()["action"], "on")
        self.env["st"] = self.environment(at=29)
        result = self.tick(60)
        self.assertIn("st", result["fresh"]["holding"])
        self.assertIsNone(self.queued("st_ac"))

    def test_empty_standby_ignores_precool_and_only_sends_off(self):
        self.activities["br"]["activity"] = "empty"
        self.running(mode="dry")
        self.write("precool_schedule.json", {"wd_today": True, "schedule": {"br": [
            {"is_workday": True, "hour": 12, "start_min": 0}]}})
        (self.root / "engine_br_eco_since").write_text(str(int(self.now) - 1800), encoding="utf-8")
        self.tick()
        self.assertEqual(self.queued()["action"], "off")

    def test_setpoint_never_cools_more_when_comfortable_or_already_loading(self):
        for sense in (23, 26.5, 27, 27.5):
            sp, _ = engine._compute_setpoint(27.5, 27.5, False, True, 26, sense, 30, False, 16, 32)
            self.assertGreaterEqual(sp, 30)
        sp, reason = engine._compute_setpoint(27.5, 27.5, False, True, 29, 29, 25, False, 16, 32)
        self.assertEqual(sp, 25)
        self.assertEqual(reason, "运行中等到位")
        sp, reason = engine._compute_setpoint(27.5, 27.5, False, True, 28, 27.6, 26, False, 16, 32)
        self.assertEqual(sp, 26)
        self.assertEqual(reason, "运行中等到位")
        sp, _ = engine._compute_setpoint(30, 30, True, True, 29, 32, 28, False, 16, 32)
        self.assertGreaterEqual(sp, 29)

    def test_cold_setpoint_recovery_is_separate_from_comfort_band(self):
        for target, sense, reference, current, expected in (
                (28.5, 25.6, 26, 25, 28),  # 用户主卧反馈：必须主动回升。
                (28.5, 28.0, 26, 25, 28),  # 恰好低于目标 0.5°C 也属于偏冷。
                (28.5, 28.01, 26, 25, 26), # 舒适带不套用偏冷回升。
                (27.5, 27.0, 26, 25, 27),  # 普通有人目标同样适用。
                (28.5, 25.6, 30, 25, 30),  # 不降低用户原本更高的设定点。
                (28.5, 25.6, 26, 29, 29)): # 回风较高时仍要抬至可卸载水平。
            with self.subTest(target=target, sense=sense, reference=reference, current=current):
                point, reason = engine._compute_setpoint(
                    target, target, False, True, current, sense, reference, False, 16, 32)
                self.assertEqual(point, expected)
                if expected > reference:
                    self.assertEqual(reason, "校准回升")

    def test_sleeping_bedroom_cold_recovery_reaches_device_queue(self):
        self.now -= 8 * 3600  # 北京时间 04:00，睡眠目标 28.5°C。
        self.activities["br"]["activity"] = "sleeping"
        self.env["br"] = self.environment(["舒适"], 25.6, "适宜")
        self.running(action="cooling", current=25, target=26)
        self.live["br_ac"]["last_changed"] = self.changed(-3600)
        result = self.tick()
        action = self.queued()
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "set")
        self.assertEqual(action["temp"], 28)
        self.assertIn("校准回升", result["rooms"]["br"]["decision"])


if __name__ == "__main__":
    unittest.main()
