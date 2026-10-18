"""One-shot program submission and ordered, non-replaying cloud execution."""

import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from cloud import CloudError, require
import native
import planner


ACTIVE = {"queued", "planning", "validating", "applying", "starting", "running", "waiting"}


def unit_name(program_id):
    require(isinstance(program_id, str) and re.fullmatch(r"[a-f0-9]{32}", program_id), "Invalid program identifier.")
    return "fredrik-dreame-" + program_id


def unit_running(program_id):
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit_name(program_id)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CloudError("RUNNER_UNAVAILABLE", "The user-service manager could not be reached.") from error
    if result.returncode not in (0, 3, 4):
        raise CloudError("RUNNER_UNAVAILABLE", "The user-service manager could not report the worker's state.")
    return result.returncode == 0


def read_program(store):
    value = store.read("program.json")
    if value is None:
        return None
    valid = (
        isinstance(value.get("id"), str) and re.fullmatch(r"[a-f0-9]{32}", value["id"])
        and isinstance(value.get("phase"), str)
        and value["phase"] in ACTIVE | {"completed", "failed", "clarification", "cancelled"}
        and all(type(value.get(key)) in (int, float) and value[key] >= 0 for key in ("createdAt", "updatedAt"))
        and all(type(value.get(key)) is int for key in ("stepIndex", "stepCount"))
        and 0 <= value["stepIndex"] <= value["stepCount"] <= 64
        and type(value.get("dispatched")) is bool
        and all(key not in value or type(value[key]) is bool for key in (
            "stopAttempted", "stopAcknowledged", "dockAttempted", "dockAcknowledged",
        ))
        and isinstance(value.get("fingerprint"), str) and isinstance(value.get("deviceId"), str)
    )
    if not valid:
        raise CloudError("INVALID_STORAGE", "The stored cleaning program has an invalid format.")
    return value


def public_program(store):
    value = read_program(store)
    if value is None:
        return None
    result = {key: value[key] for key in (
        "id", "phase", "createdAt", "updatedAt", "stepIndex", "stepCount", "currentTarget",
        "packet", "error", "dispatched", "deviceName", "stopAttempted", "stopAcknowledged",
        "dockAttempted", "dockAcknowledged", "inputText",
    ) if key in value}
    if value["phase"] in ACTIVE and time.time() - value["updatedAt"] > 15 and not unit_running(value["id"]):
        result["phase"] = "failed"
        result["error"] = {
            "code": "RUNNER_INTERRUPTED",
            "message": "The worker exited unexpectedly. No program is resumed or replayed; inspect the robot before submitting another request.",
        }
    return result


class ProgramService:
    def __init__(self, backend, *, plan_function=planner.plan, sleep=time.sleep):
        self.backend = backend
        self.store = backend.store
        self.plan_function = plan_function
        self.sleep = sleep

    def start(self, prompt, did):
        require(isinstance(prompt, str) and 1 <= len(prompt.strip()) <= 4000, "Enter a cleaning request of at most 4000 characters.")
        previous = read_program(self.store)
        if previous and unit_running(previous["id"]):
            raise CloudError("PROGRAM_ACTIVE", "A cleaning request is already being planned or executed.")
        client = self.backend.client()
        device = client.device(did)
        native.require_idle(native.read_snapshot(client, device))
        value = {
            "id": secrets.token_hex(16), "phase": "queued", "prompt": prompt,
            "inputText": prompt,
            "deviceId": device["did"], "fingerprint": client.session["fingerprint"],
            "deviceName": device["name"],
            "createdAt": self.backend.now(), "updatedAt": self.backend.now(),
            "stepIndex": 0, "stepCount": 0, "dispatched": False,
        }
        self.store.remove("confirmation.json")
        self.store.remove("program-cancel.json")
        self.store.write("program.json", value)
        command = [
            "systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit_name(value["id"]),
            "--property=Type=exec", "--property=UMask=0077", "--property=RuntimeMaxSec=9h",
            "--property=NoNewPrivileges=yes", "--setenv=PATH=" + os.environ.get("PATH", ""),
            "--setenv=XDG_DATA_HOME=" + str(self.store.root.parent),
            sys.executable, str(Path(__file__).with_name("backend.py")), "run-program", "--program-id", value["id"],
        ]
        try:
            launched = subprocess.run(command, capture_output=True, timeout=15)
            if launched.returncode:
                raise CloudError("RUNNER_START_FAILED", "The cleaning worker could not be started. No robot command was sent.")
        except (OSError, subprocess.TimeoutExpired) as error:
            self.store.write("program-cancel.json", {"id": value["id"]})
            self.update(value["id"], phase="failed", error={
                "code": "RUNNER_START_FAILED", "message": "The user-service manager could not start the worker.",
            })
            raise CloudError("RUNNER_START_FAILED", "The cleaning worker could not be started; inspect the program status.") from error
        except CloudError as error:
            self.store.write("program-cancel.json", {"id": value["id"]})
            self.update(value["id"], phase="failed", error={"code": error.code, "message": str(error)})
            raise
        return {"program": public_program(self.store)}

    def current(self, program_id):
        value = read_program(self.store)
        if not value or value.get("id") != program_id:
            raise CloudError("PROGRAM_REPLACED", "This program is no longer current.")
        return value

    def check(self, program_id):
        value = self.current(program_id)
        cancelled = self.store.read("program-cancel.json")
        if value["phase"] == "cancelled" or cancelled and cancelled.get("id") == program_id:
            raise CloudError("PROGRAM_CANCELLED", "The queued program was cancelled.")
        session = self.store.read("session.json")
        if not session or session.get("fingerprint") != value["fingerprint"] or not session.get("refreshToken"):
            raise CloudError("PROGRAM_AUTH_CHANGED", "The account changed or signed out. The queue will not continue.")
        if self.backend.now() - value["createdAt"] >= 8 * 3600:
            raise CloudError("PROGRAM_TIMEOUT", "The eight-hour program limit was reached. Inspect the robot; no command was replayed.")
        return value

    def update(self, program_id, **fields):
        value = self.current(program_id)
        value.update(fields)
        value["updatedAt"] = self.backend.now()
        self.store.write("program.json", value)
        return value

    def snapshot(self, program_id):
        self.check(program_id)
        client = self.backend.client()
        device = client.device(self.current(program_id)["deviceId"])
        return client, device, native.read_snapshot(client, device)

    def run(self, program_id):
        unit_name(program_id)
        with self.store.lock(name="program.lock"):
            with self.store.lock(wait_seconds=30):
                value = self.check(program_id)
                if value["phase"] != "queued":
                    raise CloudError("PROGRAM_ALREADY_CLAIMED", "A previously claimed program is never resumed or replayed.")
                self.update(program_id, phase="planning")
            try:
                packet = self.plan_function(value["prompt"], program_id, self.store.root)
                with self.store.lock(wait_seconds=30):
                    self.check(program_id)
                    self.update(program_id, packet=packet, prompt="")
                    if packet["type"] == "clarification":
                        self.update(program_id, phase="clarification")
                        return
                    self.update(program_id, phase="validating")
                    client, device, snapshot = self.snapshot(program_id)
                    native.require_idle(snapshot)
                    rooms = native.read_rooms(client, device)
                    steps = native.compile_steps(packet, rooms, snapshot)
                    self.update(program_id, stepCount=len(steps))
                for index, step in enumerate(steps):
                    self.execute_step(program_id, index, step, rooms["signature"])
                with self.store.lock(wait_seconds=30):
                    self.check(program_id)
                    self.update(program_id, phase="completed", stepIndex=len(steps))
            except CloudError as error:
                with self.store.lock(wait_seconds=30):
                    self.update(program_id, phase="cancelled" if error.code == "PROGRAM_CANCELLED" else "failed",
                                error={"code": error.code, "message": str(error)})
                raise
            except Exception as error:
                with self.store.lock(wait_seconds=30):
                    self.update(program_id, phase="failed", error={
                        "code": "RUNNER_FAILED",
                        "message": "The program worker failed unexpectedly. No command was replayed; inspect the robot.",
                    })
                print(f"Dreame program worker failed: {type(error).__name__}", file=sys.stderr)
                raise

    def verify_settings(self, program_id, settings, snapshot, *, active=False):
        check = {
            "phase": "after-start" if active else "before-start",
            "expected": settings,
            "observed": {
                **{key: snapshot[key] for key in ("state", "task", "mode", "custom", "suction", "water", "wetness")},
                "switches": {key: snapshot["switches"].get(key) for key in (
                    "SmartHost", "BackWashType", "CleanRoute", "SuctionMax",
                )},
            },
        }
        try:
            native.verify_settings(settings, snapshot, active=active)
        except CloudError:
            self.update(program_id, settingsCheck={**check, "matched": False})
            raise
        self.update(program_id, settingsCheck={**check, "matched": True})

    def execute_step(self, program_id, index, step, map_signature):
        with self.store.lock(wait_seconds=30):
            client, device, snapshot = self.snapshot(program_id)
            native.require_idle(snapshot)
            if native.read_rooms(client, device)["signature"] != map_signature:
                raise CloudError("MAP_CHANGED", "The active map/room bindings changed. The queue was stopped without remapping targets.")
            settings = native.settings_for(step["job"], snapshot)
            self.update(program_id, phase="applying", stepIndex=index, currentTarget=step["room"] or "Whole home")
        for setting in settings:
            with self.store.lock(wait_seconds=30):
                client, device, snapshot = self.snapshot(program_id)
                native.require_idle(snapshot)
                client.mutate(device, "set_properties", [setting])
        with self.store.lock(wait_seconds=30):
            client, device, snapshot = self.snapshot(program_id)
            native.require_idle(snapshot)
            if native.read_rooms(client, device)["signature"] != map_signature:
                raise CloudError("MAP_CHANGED", "The active map changed before dispatch; no start was sent.")
            self.verify_settings(program_id, settings, snapshot)
            self.update(program_id, phase="starting", dispatched=True)
            client.mutate(device, "action", native.start_action(step, snapshot))
        seen_active, stable_complete = False, 0
        last_checked_state = None
        started = self.backend.now()
        while True:
            self.sleep(5)
            with self.store.lock(wait_seconds=30):
                client, device, snapshot = self.snapshot(program_id)
                if snapshot["error"] or snapshot["cancelled"]:
                    raise CloudError("ROBOT_INTERRUPTED", "The robot reported a fault or cancellation. No following job will start.")
                task = snapshot["task"]
                if task in (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 31, 32, 33):
                    raise CloudError("ROBOT_PAUSED", "The robot was paused. The remaining queue was stopped; resume manually if wanted.")
                expected_task = 1 if step["segment"] is None else 3
                if task == expected_task:
                    if not seen_active or (snapshot["state"] in (1, 7, 12) and snapshot["state"] != last_checked_state):
                        try:
                            self.verify_settings(program_id, settings, snapshot, active=True)
                        except CloudError:
                            self.control_device(program_id, client, device, "stop")
                            raise
                    seen_active = True
                elif task not in (0, 25, 26):
                    raise CloudError("ROBOT_TASK_CHANGED", "The robot entered an unexpected task. The queue will not continue.")
                last_checked_state = snapshot["state"]
                complete = seen_active and task == 0 and snapshot["state"] in (6, 13)
                stable_complete = stable_complete + 1 if complete else 0
                self.update(program_id, phase="running" if task else "waiting")
                if stable_complete >= 2:
                    return
                if not seen_active and self.backend.now() - started > 180:
                    raise CloudError("START_NOT_OBSERVED", "The start was acknowledged but an active cleaning task was not observed. It will not be replayed.")

    def check_cancel_context(self, value, expected_context):
        if expected_context is None:
            return
        session = self.store.read("session.json")
        if (not session or session.get("fingerprint") != expected_context["fingerprint"]
                or value["fingerprint"] != expected_context["fingerprint"]
                or value["deviceId"] != expected_context["deviceId"]
                or self.backend.settings()["selectedDid"] != expected_context["deviceId"]):
            raise CloudError(
                "PROGRAM_CONTEXT_CHANGED",
                "The control request no longer matches the current account and selected vacuum. No robot command was sent.",
            )

    def cancel(self, program_id, *, expected_context=None):
        unit_name(program_id)
        value = self.current(program_id)
        self.check_cancel_context(value, expected_context)
        self.store.write("program-cancel.json", {"id": program_id})
        try:
            result = subprocess.run(
                ["systemctl", "--user", "stop", unit_name(program_id)],
                capture_output=True, timeout=15,
            )
            if result.returncode and unit_running(program_id):
                raise CloudError("RUNNER_STOP_FAILED", "The worker could not be stopped; the cancellation flag remains set.")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CloudError("RUNNER_STOP_FAILED", "The worker could not be stopped; the cancellation flag remains set.") from error
        with self.store.lock(wait_seconds=30):
            value = self.current(program_id)
            self.check_cancel_context(value, expected_context)
            if value["phase"] in ("completed", "clarification"):
                raise CloudError("PROGRAM_NOT_ACTIVE", "This program is finished; no Stop command was sent.")
            value = self.update(program_id, phase="cancelled")
            if value["dispatched"] and not value.get("dockAcknowledged"):
                if value.get("stopAttempted") and not value.get("stopAcknowledged"):
                    raise CloudError("STOP_UNVERIFIED", "Stop was already attempted and will not be replayed. Inspect the robot manually.")
                if value.get("dockAttempted"):
                    raise CloudError("DOCK_UNVERIFIED", "Return to dock was already attempted and will not be replayed. Inspect the robot manually.")
                client = self.backend.client()
                device = client.device(value["deviceId"])
                if not value.get("stopAcknowledged"):
                    self.control_device(program_id, client, device, "stop")
                self.control_device(program_id, client, device, "dock")
            value = self.current(program_id)
        return {
            "queueCancelled": True, "stopAcknowledged": value.get("stopAcknowledged", False),
            "dockAcknowledged": value.get("dockAcknowledged", False), "physicalOutcomeVerified": False,
        }

    def control_device(self, program_id, client, device, control):
        siid, aiid, code, message = {
            "stop": (4, 2, "STOP_UNVERIFIED",
                     "The remaining queue is stopped, but physical Stop could not be verified. It will not be replayed; inspect the robot manually."),
            "dock": (3, 1, "DOCK_UNVERIFIED",
                     "Stop was acknowledged, but return to dock could not be verified. It will not be replayed; inspect the robot manually."),
        }[control]
        self.update(program_id, **{control + "Attempted": True})
        try:
            result = client.command(device, "action", {"did": device["did"], "siid": siid, "aiid": aiid, "in": []}, False)
            if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 0:
                raise CloudError(code, message)
        except CloudError as error:
            self.update(program_id, error={"code": code, "message": message})
            raise CloudError(code, message) from error
        self.update(program_id, **{control + "Acknowledged": True})

    def invalidate(self):
        value = read_program(self.store)
        if not value or value.get("phase") not in ACTIVE:
            return
        self.store.write("program-cancel.json", {"id": value["id"]})
        try:
            result = subprocess.run(
                ["systemctl", "--user", "stop", unit_name(value["id"])], capture_output=True, timeout=15,
            )
            if result.returncode and unit_running(value["id"]):
                raise CloudError("RUNNER_STOP_FAILED", "The program worker could not be stopped.")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CloudError("RUNNER_STOP_FAILED", "The program worker could not be stopped.") from error
        self.update(value["id"], phase="cancelled", error={
            "code": "PROGRAM_AUTH_CHANGED",
            "message": "Account changed or signed out; the remaining queue was cancelled. An already running robot task may continue.",
        })
