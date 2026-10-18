import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import Backend, Store
from cloud import CloudError
from native import MODEL, PROPERTIES
from planner import model_answer
from programs import ProgramService, public_program
from test_cloud import PROFILE, SESSION
from test_native import job, snapshot


PROGRAM_ID = "a" * 32
DEVICE = {"did": "123", "name": "Fixture", "model": MODEL, "binding": ""}
ROOMS = {"mapId": "765", "rooms": {"Kitchen": 7, "Bathroom": 9}, "signature": "fixture-map"}


def packet(jobs):
    return {"version": 1, "capabilitiesVersion": 2, "requestId": PROGRAM_ID, "type": "program", "jobs": jobs}


class Client:
    def __init__(self):
        self.session = dict(SESSION)
        self.state = snapshot()
        self.calls = []
        self.polls = 0
        self.action_error = None
        self.stop_calls = 0
        self.stop_result = {"code": 0}
        self.dock_calls = 0
        self.dock_result = {"code": 0}
        self.control_calls = []

    def command(self, device, method, params, read_only):
        assert method == "action" and read_only is False
        control = params["siid"], params["aiid"]
        self.control_calls.append(control)
        if control == (4, 2):
            self.stop_calls += 1
            return self.stop_result
        assert control == (3, 1)
        self.dock_calls += 1
        if isinstance(self.dock_result, CloudError):
            raise self.dock_result
        return self.dock_result

    def device(self, did):
        assert did == DEVICE["did"]
        return DEVICE

    def mutate(self, device, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "action":
            if self.action_error:
                raise self.action_error
            self.state.update(state=1, task=1 if params["siid"] == 2 else 3)
            self.polls = 0
        else:
            for prop in params:
                name = next(k for k, pair in PROPERTIES.items() if pair == (prop["siid"], prop["piid"]))
                if name == "switches":
                    selected = json.loads(prop["value"])
                    self.state["switches"][selected["k"]] = selected["v"]
                else:
                    self.state[name] = prop["value"]
        return {"accepted": True}


class ProgramTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "private")
        self.store.write("profile.json", PROFILE)
        self.store.write("session.json", SESSION)
        self.now = 1000
        self.backend = Backend(self.store, now=lambda: self.now)
        self.client = Client()
        self.backend.client = lambda: self.client
        self.planned = packet([job()])
        self.service = ProgramService(self.backend, plan_function=lambda *args: self.planned, sleep=self.sleep)
        self.store.write("program.json", {
            "id": PROGRAM_ID, "phase": "queued", "prompt": "Fixture request", "deviceId": DEVICE["did"],
            "fingerprint": SESSION["fingerprint"], "createdAt": self.now, "updatedAt": self.now,
            "stepIndex": 0, "stepCount": 0, "dispatched": False,
        })
        self.addCleanup(patch.stopall)
        patch("programs.native.read_snapshot", side_effect=lambda *args: copy.deepcopy(self.client.state)).start()
        patch("programs.native.read_rooms", return_value=ROOMS).start()

    def sleep(self, seconds):
        self.now += seconds
        self.client.polls += 1
        if self.client.polls >= 2:
            self.client.state.update(state=13, task=0)

    def actions(self):
        return [params for method, params in self.client.calls if method == "action"]

    def test_ordered_room_and_mop_tasks_wait_for_observed_completion(self):
        self.planned = packet([job(rooms=["Bathroom", "Kitchen"]), job("mop")])
        self.service.run(PROGRAM_ID)
        self.assertEqual(self.store.read("program.json")["phase"], "completed")
        self.assertEqual(self.store.read("program.json")["stepIndex"], 3)
        actions = self.actions()
        self.assertEqual([json.loads(a["in"][1]["value"])["selects"][0][0] for a in actions[:2]], [9, 7])
        self.assertEqual(actions[2], {"siid": 2, "aiid": 1, "in": []})
        self.assertGreaterEqual(self.now, 1045)

    def test_invalid_later_target_sends_no_settings_or_start(self):
        self.planned = packet([job(), job(rooms=["Office"])])
        with self.assertRaisesRegex(CloudError, "no unique room"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.read("program.json")["phase"], "failed")

    def test_clarification_sends_no_device_command(self):
        self.planned = {
            "version": 1, "capabilitiesVersion": 2, "requestId": PROGRAM_ID, "type": "clarification",
            "issues": [{"code": "UNKNOWN_ROOM", "question": "Which room?"}],
        }
        self.service.run(PROGRAM_ID)
        self.assertEqual(self.store.read("program.json")["phase"], "clarification")
        self.assertEqual(self.client.calls, [])

    def test_ambiguous_start_is_not_replayed_or_resumed(self):
        self.client.action_error = CloudError("NETWORK_ERROR", "Fixture ambiguous dispatch")
        with self.assertRaises(CloudError):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)
        self.assertTrue(self.store.read("program.json")["dispatched"])
        with self.assertRaisesRegex(CloudError, "never resumed"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)

    def test_cancellation_while_luna_is_planning_prevents_all_writes(self):
        def cancel(*args):
            self.store.write("program-cancel.json", {"id": PROGRAM_ID})
            return self.planned
        self.service.plan_function = cancel
        with self.assertRaises(CloudError):
            self.service.run(PROGRAM_ID)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.read("program.json")["phase"], "cancelled")

    def test_changed_account_prevents_execution(self):
        self.store.write("session.json", {**SESSION, "fingerprint": "changed"})
        with self.assertRaisesRegex(CloudError, "account changed"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(self.client.calls, [])

    def test_paused_robot_never_advances_to_next_job(self):
        self.planned = packet([job(), job("mop")])
        def pause(seconds):
            self.now += seconds
            self.client.state.update(state=3, task=6)
        self.service.sleep = pause
        with self.assertRaisesRegex(CloudError, "paused"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)

    def test_changed_map_stops_before_start(self):
        with patch("programs.native.read_rooms", side_effect=[ROOMS, ROOMS, {**ROOMS, "signature": "changed"}]):
            with self.assertRaisesRegex(CloudError, "map changed"):
                self.service.run(PROGRAM_ID)
        self.assertEqual(self.actions(), [])

    def test_unobserved_start_times_out_without_replay(self):
        def no_start(seconds):
            self.now += seconds
            self.client.state.update(state=13, task=0)
        self.service.sleep = no_start
        with self.assertRaisesRegex(CloudError, "not observed"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)

    def test_manual_mutations_are_blocked_during_program(self):
        with patch("programs.unit_running", return_value=True):
            response = self.backend.execute({"op": "enable-mutations", "enabled": True})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "PROGRAM_ACTIVE")

    def test_program_state_does_not_expose_fingerprint_or_prompt(self):
        with patch("programs.unit_running", return_value=True):
            public = public_program(self.store)
        self.assertNotIn("fingerprint", public)
        self.assertNotIn("prompt", public)
        self.assertNotIn("deviceId", public)

    def test_duplicate_submit_cannot_launch_a_second_worker(self):
        with patch("programs.unit_running", return_value=True):
            with patch("programs.subprocess.run") as launch:
                with self.assertRaisesRegex(CloudError, "already"):
                    self.service.start("Vacuum", DEVICE["did"])
                launch.assert_not_called()

    def test_cancel_stops_once_even_when_two_monitors_cancel(self):
        self.service.update(PROGRAM_ID, phase="running", dispatched=True)
        with patch("programs.subprocess.run") as stop:
            stop.return_value.returncode = 0
            for _ in range(2):
                response = self.service.cancel(PROGRAM_ID)
                self.assertTrue(response["stopAcknowledged"])
                self.assertTrue(response["dockAcknowledged"])
                self.assertFalse(response["physicalOutcomeVerified"])
        self.assertEqual(self.client.stop_calls, 1)
        self.assertEqual(self.client.dock_calls, 1)
        self.assertEqual(self.client.control_calls, [(4, 2), (3, 1)])
        self.assertTrue(public_program(self.store)["dockAcknowledged"])

    def test_ambiguous_stop_is_not_replayed(self):
        self.service.update(PROGRAM_ID, phase="running", dispatched=True)
        self.client.stop_result = {"code": -1}
        with patch("programs.subprocess.run") as stop:
            stop.return_value.returncode = 0
            for _ in range(2):
                with self.assertRaises(CloudError):
                    self.service.cancel(PROGRAM_ID)
        self.assertEqual(self.client.stop_calls, 1)
        self.assertEqual(self.client.dock_calls, 0)

    def test_ambiguous_docking_is_not_replayed(self):
        for result in ({"code": -1}, {"code": True}, {}, CloudError("NETWORK_ERROR", "Fixture timeout")):
            with self.subTest(result=result):
                self.service.update(PROGRAM_ID, phase="running", dispatched=True,
                                    stopAttempted=False, stopAcknowledged=False,
                                    dockAttempted=False, dockAcknowledged=False)
                self.client.stop_calls = self.client.dock_calls = 0
                self.client.dock_result = result
                with patch("programs.subprocess.run") as stop:
                    stop.return_value.returncode = 0
                    for _ in range(2):
                        with self.assertRaises(CloudError) as failure:
                            self.service.cancel(PROGRAM_ID)
                        self.assertEqual(failure.exception.code, "DOCK_UNVERIFIED")
                self.assertEqual(self.client.stop_calls, 1)
                self.assertEqual(self.client.dock_calls, 1)
                value = self.store.read("program.json")
                self.assertTrue(value["stopAcknowledged"])
                self.assertTrue(value["dockAttempted"])
                self.assertFalse(value["dockAcknowledged"])

    def test_cancel_before_dispatch_does_not_move_robot(self):
        with patch("programs.subprocess.run") as stop:
            stop.return_value.returncode = 0
            response = self.service.cancel(PROGRAM_ID)
        self.assertTrue(response["queueCancelled"])
        self.assertFalse(response["dockAcknowledged"])
        self.assertEqual(self.client.control_calls, [])

    def test_end_after_acknowledged_stop_only_requests_docking(self):
        self.service.update(PROGRAM_ID, phase="cancelled", dispatched=True,
                            stopAttempted=True, stopAcknowledged=True)
        with patch("programs.subprocess.run") as stop:
            stop.return_value.returncode = 0
            self.service.cancel(PROGRAM_ID)
        self.assertEqual(self.client.control_calls, [(3, 1)])

    def test_mop_wetness_changed_by_room_start_stops_without_next_job(self):
        self.planned = packet([job("mop", rooms=["Kitchen"]), job()])
        def changed(seconds):
            self.now += seconds
            self.client.state["wetness"] = 30
        self.service.sleep = changed
        with self.assertRaisesRegex(CloudError, "wetness: expected 32, read 30"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual(self.client.stop_calls, 1)
        value = self.store.read("program.json")
        self.assertTrue(value["stopAcknowledged"])
        self.assertEqual(self.client.dock_calls, 0)
        check = value["settingsCheck"]
        self.assertFalse(check["matched"])
        self.assertEqual(check["phase"], "after-start")
        self.assertEqual(check["observed"]["wetness"], 30)
        self.assertEqual(check["observed"]["task"], 3)
        self.assertIn({"siid": 28, "piid": 1, "value": 32}, check["expected"])
        self.assertNotIn("settingsCheck", public_program(self.store))

    def test_successful_start_retains_only_bounded_setting_diagnostics(self):
        self.planned = packet([job("mop-after-vac", rooms=["Kitchen"])])
        self.service.run(PROGRAM_ID)
        check = self.store.read("program.json")["settingsCheck"]
        self.assertTrue(check["matched"])
        self.assertEqual(check["phase"], "after-start")
        self.assertEqual(check["observed"]["wetness"], 32)
        self.assertEqual(set(check["observed"]["switches"]), {
            "SmartHost", "BackWashType", "CleanRoute", "SuctionMax",
        })

    def test_before_start_mismatch_records_detail_without_start_or_stop(self):
        self.planned = packet([job("mop", rooms=["Kitchen"])])
        mutate = self.client.mutate
        def changed(device, method, params):
            result = mutate(device, method, params)
            if method == "set_properties" and params[-1]["piid"] == 50:
                if json.loads(params[-1]["value"])["k"] == "CleanRoute":
                    self.client.state["wetness"] = 30
            return result
        self.client.mutate = changed
        with self.assertRaisesRegex(CloudError, "before start .*wetness: expected 32, read 30"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.client.stop_calls, 0)
        check = self.store.read("program.json")["settingsCheck"]
        self.assertFalse(check["matched"])
        self.assertEqual(check["phase"], "before-start")
        self.assertEqual(check["observed"]["task"], 0)

    def kitchen_vacuum_then_mop(self):
        self.planned = packet([{
            **job("mop-after-vac", rooms=["Kitchen"]),
            "passes": 1, "suction": "max", "wetness": 28,
            "washing": {"by": "area", "value": 10}, "route": "intensive",
        }])
        mutate = self.client.mutate
        def start_vacuum(device, method, params):
            result = mutate(device, method, params)
            if method == "action":
                self.client.state["switches"]["CleanRoute"] = 1
            return result
        self.client.mutate = start_vacuum

    def test_vacuum_first_standard_route_continues_and_checks_intensive_mop_phase(self):
        self.kitchen_vacuum_then_mop()
        def phases(seconds):
            self.now += seconds
            self.client.polls += 1
            if self.client.polls == 2:
                self.client.state.update(state=7)
                self.client.state["switches"]["CleanRoute"] = 2
            elif self.client.polls >= 3:
                self.client.state.update(state=13, task=0)
        self.service.sleep = phases
        self.service.run(PROGRAM_ID)
        value = self.store.read("program.json")
        self.assertEqual(value["phase"], "completed")
        self.assertEqual(self.client.stop_calls, 0)
        self.assertEqual(len(self.actions()), 1)
        self.assertTrue(value["settingsCheck"]["matched"])
        self.assertEqual(value["settingsCheck"]["observed"]["state"], 7)
        self.assertEqual(value["settingsCheck"]["observed"]["switches"]["CleanRoute"], 2)

    def test_mopping_route_downgrade_still_stops_before_next_job(self):
        self.kitchen_vacuum_then_mop()
        self.planned["jobs"].append(job())
        def phases(seconds):
            self.now += seconds
            self.client.polls += 1
            if self.client.polls == 2:
                self.client.state.update(state=7)
            elif self.client.polls >= 3:
                self.client.state.update(state=13, task=0)
        self.service.sleep = phases
        with self.assertRaisesRegex(CloudError, "CleanRoute: expected 2, read 1"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual(self.client.stop_calls, 1)
        value = self.store.read("program.json")
        self.assertEqual(value["settingsCheck"]["observed"]["state"], 7)
        self.assertTrue(value["stopAcknowledged"])

    def test_mopping_route_is_rechecked_after_station_transition(self):
        self.kitchen_vacuum_then_mop()
        def phases(seconds):
            self.now += seconds
            self.client.polls += 1
            if self.client.polls == 2:
                self.client.state.update(state=7)
                self.client.state["switches"]["CleanRoute"] = 2
            elif self.client.polls == 3:
                self.client.state.update(state=17, task=25)
                self.client.state["switches"]["CleanRoute"] = 1
            elif self.client.polls == 4:
                self.client.state.update(state=7, task=3)
            elif self.client.polls >= 5:
                self.client.state.update(state=13, task=0)
        self.service.sleep = phases
        with self.assertRaisesRegex(CloudError, "CleanRoute: expected 2, read 1"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(self.client.polls, 4)
        self.assertEqual(self.client.stop_calls, 1)

    def test_boost_charge_requirement_is_rechecked_after_previous_job(self):
        self.planned = packet([job(), {**job(), "maxSuctionPower": True}])
        def discharge(seconds):
            self.sleep(seconds)
            self.client.state["battery"] = 99
        self.service.sleep = discharge
        with self.assertRaisesRegex(CloudError, "fully charged"):
            self.service.run(PROGRAM_ID)
        self.assertEqual(len(self.actions()), 1)

    def test_launch_timeout_marks_cancel_before_late_worker_can_claim(self):
        import subprocess
        self.store.remove("program.json")
        with patch("programs.subprocess.run", side_effect=subprocess.TimeoutExpired("systemd-run", 15)):
            with self.assertRaises(CloudError):
                self.service.start("Vacuum", DEVICE["did"])
        value = self.store.read("program.json")
        self.assertEqual(value["phase"], "failed")
        self.assertEqual(self.store.read("program-cancel.json")["id"], value["id"])
        with self.assertRaises(CloudError):
            self.service.run(value["id"])
        self.assertEqual(self.client.calls, [])

    def test_logout_removes_credentials_even_if_worker_stop_fails(self):
        with patch("programs.ProgramService.invalidate", side_effect=CloudError("RUNNER_STOP_FAILED", "Fixture failure")):
            response = self.backend.execute({"op": "logout"})
        self.assertFalse(response["ok"])
        self.assertIsNone(self.store.read("session.json"))
        self.assertFalse(self.backend.settings()["mutationsEnabled"])

    def test_login_resets_old_manual_authority_before_worker_stop_failure(self):
        self.store.write("settings.json", {"selectedDid": "123", "mutationsEnabled": True})
        self.store.write("confirmation.json", {"token": "fixture"})
        with patch("backend.CloudClient"):
            with patch("programs.ProgramService.invalidate", side_effect=CloudError("RUNNER_STOP_FAILED", "Fixture failure")):
                with self.assertRaises(CloudError):
                    self.backend.login("eu", "fixture", "fixture")
        self.assertIsNone(self.store.read("confirmation.json"))
        self.assertEqual(self.backend.settings(), {"selectedDid": "", "mutationsEnabled": False})

    def test_invalid_stored_program_is_reported_explicitly(self):
        for fields in ({"phase": []}, {"id": "not-a-unit"}, {"stepIndex": -1}, {"updatedAt": "yesterday"},
                       {"dockAcknowledged": "yes"}, {"dockAttempted": 1}):
            value = self.store.read("program.json")
            self.store.write("program.json", {**value, **fields})
            with self.assertRaises(CloudError) as failure:
                public_program(self.store)
            self.assertEqual(failure.exception.code, "INVALID_STORAGE")
            self.store.write("program.json", value)


class PlannerEventTests(unittest.TestCase):
    def test_jsonl_preserves_response_without_terminal_line_wrapping(self):
        content = json.dumps(packet([job()]))
        events = [
            {"type": "assistant.reasoning", "data": {"content": "not returned"}},
            {"type": "assistant.message", "data": {"model": "gpt-6-luna", "content": content, "toolRequests": []}},
        ]
        self.assertEqual(model_answer("\n".join(json.dumps(e) for e in events)), content)

    def test_tools_wrong_models_and_multiple_answers_are_rejected(self):
        message = {"type": "assistant.message", "data": {"model": "gpt-6-luna", "content": "{}"}}
        for events in (
            [{"type": "tool.execution_start"}],
            [{**message, "data": {**message["data"], "model": "other-model"}}],
            [{**message, "data": {**message["data"], "toolRequests": [{"name": "shell"}]}}],
            [message, message], [],
        ):
            with self.assertRaises(CloudError):
                model_answer("\n".join(json.dumps(e) for e in events))


if __name__ == "__main__":
    unittest.main()
