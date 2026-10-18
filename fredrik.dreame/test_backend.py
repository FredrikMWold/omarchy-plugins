import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import Backend, Store, DEFAULT_PROPERTIES, import_profile
from cloud import CloudError
from test_cloud import DEVICE, PROFILE, SESSION, Transport, success, command_result, property_result


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state")
        self.store.write("profile.json", PROFILE)
        self.store.write("session.json", SESSION)
        self.backend = Backend(self.store, now=lambda: 1000)

    def test_private_permissions_and_no_credentials_in_state(self):
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.root / "session.json").stat().st_mode & 0o777, 0o600)
        response = self.backend.execute({"op": "state"})
        self.assertTrue(response["ok"])
        text = json.dumps(response)
        for value in ("fixture-access", "fixture-refresh", SESSION["username"], PROFILE["clientKey"]):
            self.assertNotIn(value, text)

    def test_symlink_and_insecure_files_are_rejected(self):
        target = self.store.root / "session.json"
        target.unlink()
        target.symlink_to(self.store.root / "profile.json")
        with self.assertRaises(CloudError):
            self.store.read("session.json")
        target.unlink()
        self.store.write("session.json", SESSION)
        target.chmod(0o644)
        with self.assertRaises(CloudError):
            self.store.read("session.json")

    def test_kernel_lock_prevents_overlapping_refreshes_and_releases(self):
        with self.store.lock():
            with self.assertRaisesRegex(CloudError, "still running"):
                with self.store.lock():
                    self.fail("Nested lock was acquired")
            self.assertTrue(self.backend.execute({"op": "state"})["ok"])
        self.assertTrue(self.backend.execute({"op": "state"})["ok"])

    def test_reads_can_wait_for_a_lock_without_retrying_a_command(self):
        with patch("backend.fcntl.flock", side_effect=[BlockingIOError(), None, None]) as lock:
            with patch("backend.time.sleep") as sleep:
                with self.store.lock(wait_seconds=30):
                    pass
        self.assertEqual(lock.call_count, 3)
        sleep.assert_called_once_with(0.05)

    def test_waiting_for_a_lock_is_bounded(self):
        with patch("backend.fcntl.flock", side_effect=BlockingIOError()) as lock:
            with patch("backend.time.monotonic", side_effect=[0, 31]):
                with patch("backend.time.sleep") as sleep:
                    with self.assertRaisesRegex(CloudError, "still running"):
                        with self.store.lock(wait_seconds=30):
                            self.fail("Busy lock was acquired")
        self.assertEqual(lock.call_count, 1)
        sleep.assert_not_called()

    def test_unknown_settings_are_rejected(self):
        self.store.write("settings.json", {"mutationsEnabled": False, "selectedDid": "", "unexpected": True})
        with self.assertRaisesRegex(CloudError, "settings have an invalid format"):
            self.backend.execute({"op": "state"})

    def test_corrupt_storage_is_not_silently_replaced(self):
        target = self.store.root / "settings.json"
        target.write_text("{broken")
        target.chmod(0o600)
        with self.assertRaises(CloudError):
            self.backend.execute({"op": "state"})
        self.assertEqual(target.read_text(), "{broken")

    def test_profile_import_checks_source_hash_without_execution(self):
        source = self.store.root / "untrusted.py"
        source.write_text("raise RuntimeError('must not execute')")
        with self.assertRaisesRegex(CloudError, "checksum mismatch"):
            import_profile(self.store, source)
        self.assertEqual(self.store.read("profile.json"), PROFILE)

    def fake_client(self):
        class Client:
            def device(self, did):
                if did != DEVICE["did"]:
                    raise CloudError("DEVICE_NOT_FOUND", "fixture")
                return DEVICE

            def mutate(self, device, method, params):
                return {"accepted": True}
        client = Client()
        self.backend.client = lambda: client
        return client

    def prepare(self):
        return self.backend.execute({
            "op": "prepare", "deviceId": DEVICE["did"], "method": "action",
            "params": {"siid": 4, "aiid": 1, "in": []},
        })

    def test_writes_disabled_by_default_and_unknown_ops_do_not_send(self):
        self.fake_client()
        self.assertFalse(self.prepare()["ok"])
        self.assertFalse(self.backend.execute({"op": "arbitrary-http", "url": "http://other"})["ok"])
        self.assertFalse(self.backend.execute({"op": "state", "extra": True})["ok"])

    def test_confirmation_is_exact_single_use_and_consumed_before_failure(self):
        client = self.fake_client()
        self.backend.execute({"op": "enable-mutations", "enabled": True})
        preview = self.prepare()["data"]["confirmation"]
        calls = []

        def failed(device, method, params):
            self.assertIsNone(self.store.read("confirmation.json"))
            calls.append((device, method, params))
            raise CloudError("NETWORK_ERROR", "Outcome unknown; no retry")

        client.mutate = failed
        result = self.backend.execute({"op": "confirm", "token": preview["token"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NETWORK_ERROR")
        result = self.backend.execute({"op": "confirm", "token": preview["token"]})
        self.assertEqual(result["error"]["code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(len(calls), 1)

    def test_expired_replaced_and_disabled_confirmations(self):
        self.fake_client()
        self.backend.execute({"op": "enable-mutations", "enabled": True})
        first = self.prepare()["data"]["confirmation"]
        second = self.prepare()["data"]["confirmation"]
        result = self.backend.execute({"op": "confirm", "token": first["token"]})
        self.assertEqual(result["error"]["code"], "CONFIRMATION_REQUIRED")
        self.backend.now = lambda: 1091
        result = self.backend.execute({"op": "confirm", "token": second["token"]})
        self.assertEqual(result["error"]["code"], "CONFIRMATION_EXPIRED")
        self.backend.now = lambda: 1000
        third = self.prepare()["data"]["confirmation"]
        self.backend.execute({"op": "enable-mutations", "enabled": False})
        self.assertEqual(
            self.backend.execute({"op": "confirm", "token": third["token"]})["error"]["code"],
            "CONFIRMATION_REQUIRED",
        )

    def test_signout_only_removes_plugin_session(self):
        self.backend.execute({"op": "enable-mutations", "enabled": True})
        result = self.backend.execute({"op": "logout"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["state"]["sessionAvailable"])
        self.assertFalse(result["state"]["mutationsEnabled"])
        self.assertFalse(result["data"]["cloudSessionRevoked"])
        self.assertIsNotNone(self.store.read("profile.json"))

    def test_status_roundtrip_uses_current_bound_device_and_marks_partial(self):
        record = {"did": 123, "model": DEVICE["model"], "customName": DEVICE["name"], "bindDomain": DEVICE["binding"]}
        values = [property_result(**p, value=0) for p in DEFAULT_PROPERTIES]
        for item in values:
            pair = item["siid"], item["piid"]
            item["value"] = {(2, 1): 13, (3, 1): 100, (3, 2): 1, (4, 4): 1}.get(pair, 0)
            if pair == (4, 23):
                item.update(code=-4004, value=None)
        page = {"records": [record], "total": "1", "current": "1", "size": "100", "pages": "1"}
        transport = Transport(success({"page": page}), command_result(values))
        backend = Backend(self.store, transport, lambda: 1000)
        with patch.object(self.store, "lock", wraps=self.store.lock) as lock:
            result = backend.execute({"op": "status", "deviceId": "123"})
        lock.assert_called_once_with(wait_seconds=30)
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["complete"])
        self.assertEqual(result["data"]["stateLabel"], "Charging completed")
        self.assertEqual(result["data"]["display"][0]["text"], "100%")
        self.assertEqual(len(transport.calls), 2)

    def test_cli_json_contract_and_missing_login(self):
        environment = {**os.environ, "XDG_DATA_HOME": self.temporary.name}
        process = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("backend.py")), "request"],
            input='{"op":"devices"}\n', text=True, capture_output=True, env=environment, timeout=10,
        )
        self.assertEqual(process.returncode, 1)
        response = json.loads(process.stdout)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "SETUP_REQUIRED")
        self.assertEqual(process.stderr, "")


if __name__ == "__main__":
    unittest.main()
