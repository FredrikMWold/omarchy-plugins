import asyncio
import dataclasses
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import router


class RestartTargetTests(unittest.IsolatedAsyncioTestCase):
    """Public selection/startup and CLI checkpoint, with private local state."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.paths = router.Paths(self.base / "config/fredrik.shield-remote",
                                  self.base / "runtime/fredrik.shield-remote")
        self.paths.prepare()
        self.config = router.Config(remote_address="00:11:22:33:44:55",
                                    shield_host="shield.test", paired_host="shield.test")
        router.save_config(self.paths, self.config)
        self.commands = []
        self.soundbar_response = '{"id": 1, "result": []}'

        async def system_command(command, timeout=15):
            self.commands.append(command)
            if command[0] == "notify-send":
                return ""
            if command[0] == "curl":
                return self.soundbar_response
            if command[:2] == ["udevadm", "settle"]:
                return ""
            if command[:2] == ["udevadm", "info"]:
                return "LIBINPUT_IGNORE_DEVICE=1\nCURRENT_TAGS=:uaccess:seat:\n"
            raise AssertionError(f"Unexpected live input/player/Bluetooth command: {command}")

        self.command_patch = patch.object(router, "run", side_effect=system_command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    async def status(self, backend):
        return await backend.request({"command": "status"})

    async def test_successfully_selected_shield_is_restored_before_first_input(self):
        original = router.Router(self.paths, self.config)
        self.assertEqual((await self.status(original))["target"], "computer")
        await original.request({"command": "target", "target": "shield"})
        restarted = router.Router(self.paths, self.config)
        self.assertEqual((await self.status(restarted))["target"], "shield")
        with self.assertRaisesRegex(RuntimeError, "Shield is disconnected.*not sent to the computer"):
            await restarted.key(164, 1, 1)
        self.assertTrue(all(command[0] == "notify-send" for command in self.commands))

    async def checkpoint(self, response):
        async def old_daemon(reader, writer):
            self.assertEqual(json.loads(await reader.readline()), {"command": "status"})
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(old_daemon, path=str(self.paths.socket))
        environment = {
            **os.environ, "XDG_CONFIG_HOME": str(self.paths.config.parent),
            "XDG_RUNTIME_DIR": str(self.paths.runtime.parent),
            "DBUS_SESSION_BUS_ADDRESS": "unix:abstract=shield-restart-test-unused",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(router.PLUGIN / "router.py"), "checkpoint-target",
                env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                return process.returncode, stdout.decode(), stderr.decode()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        finally:
            server.close()
            await server.wait_closed()
            self.paths.socket.unlink(missing_ok=True)

    async def test_checkpoint_cli_migrates_current_old_daemon_shield_without_target_command(self):
        code, output, error = await self.checkpoint(
            {"ok": True, "state": {"running": True, "target": "shield"}})
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), {"ok": True, "target": "shield", "checkpointed": True})
        restarted = router.Router(self.paths, self.config)
        self.assertEqual((await self.status(restarted))["target"], "shield")
        self.assertEqual(self.commands, [])

    async def test_failed_sony_selection_cannot_change_restored_target_or_reassert_on_startup(self):
        config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        original = router.Router(self.paths, config)
        await original.request({"command": "target", "target": "shield"})
        self.soundbar_response = '{"id": 1, "error": [7, "Not allowed"]}'
        with self.assertRaisesRegex(RuntimeError, "Soundbar rejected HDMI2"):
            await original.request({"command": "target", "target": "computer"})
        count = len(self.commands)
        restarted = router.Router(self.paths, config)
        self.assertEqual((await self.status(restarted))["target"], "shield")
        self.assertEqual(len(self.commands), count)

    async def test_successful_computer_selection_is_restored_and_checkpoint_is_private(self):
        original = router.Router(self.paths, self.config)
        await original.request({"command": "target", "target": "shield"})
        await original.request({"command": "target", "target": "computer"})
        self.assertEqual((await self.status(router.Router(self.paths, self.config)))["target"], "computer")
        state_file = self.paths.runtime / "router-target.json"
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.runtime.stat().st_mode), 0o700)

    async def test_invalid_checkpoint_response_never_overwrites_prior_shield_mode(self):
        original = router.Router(self.paths, self.config)
        await original.request({"command": "target", "target": "shield"})
        for response in (
            {"ok": False, "state": {"running": True, "target": "computer"}},
            {"ok": True, "state": {"running": False, "target": "computer"}},
            {"ok": True, "state": {"running": True, "target": "unknown"}},
            {"ok": True, "state": {"running": True, "target": None}},
            {"ok": True},
        ):
            code, output, error = await self.checkpoint(response)
            self.assertNotEqual(code, 0, output)
            self.assertIn("Cannot checkpoint target", error)
            self.assertEqual((await self.status(router.Router(self.paths, self.config)))["target"], "shield")

    async def test_invalid_saved_mode_fails_closed_instead_of_starting_computer(self):
        state_file = self.paths.runtime / "router-target.json"
        for payload in (
            "not JSON", "[]", '{"schema": 1, "target": "SHIELD"}',
            '{"schema": true, "target": "shield"}',
            '{"schema": 1, "target": "computer", "unexpected": 1}',
        ):
            state_file.write_text(payload)
            with self.assertRaises(ValueError):
                router.Router(self.paths, self.config)
        self.assertEqual(self.commands, [])

    async def test_failed_mode_persistence_restores_prior_sony_input_and_keeps_shield(self):
        config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        original = router.Router(self.paths, config)
        await original.request({"command": "target", "target": "shield"})
        self.commands.clear()
        with patch.object(Path, "replace", side_effect=OSError("checkpoint disk full")):
            with self.assertRaisesRegex(RuntimeError, "Cannot preserve target.*checkpoint disk full"):
                await original.request({"command": "target", "target": "computer"})
        receiver_calls = [command for command in self.commands if command[0] == "curl"]
        self.assertEqual(len(receiver_calls), 2)
        self.assertEqual(
            [json.loads(command[command.index("--data") + 1])["params"][0]["uri"]
             for command in receiver_calls],
            ["extInput:hdmi?port=2", "extInput:hdmi?port=1"])
        self.assertEqual((await self.status(router.Router(self.paths, config)))["target"], "shield")

    async def test_regrab_waits_for_all_held_buttons_before_resuming_restored_shield(self):
        original = router.Router(self.paths, self.config)
        await original.request({"command": "target", "target": "shield"})
        restarted = router.Router(self.paths, self.config)
        self.commands.clear()
        rule = self.base / "input.rules"
        rule.write_text(router.rule_text(self.config.remote_address))
        router.write_private_json(self.paths.config / "input-setup.json",
                                  {"schema": 1, "address": self.config.remote_address})
        complete = asyncio.Event()
        observations = []
        test = self

        class Device:
            name = router.REMOTE_NAME
            uniq = test.config.remote_address
            info = SimpleNamespace(vendor=0x0955, product=0x7217)

            def grab(self):
                pass

            def close(self):
                pass

            def active_keys(self):
                return [164, 139]

            async def async_read_loop(self):
                for code, value in ((103, 1), (164, 2), (164, 0), (103, 0), (139, 0)):
                    observations.append(await test.status(restarted))
                    yield SimpleNamespace(type=1, code=code, value=value, timestamp=time.time)
                observations.append(await test.status(restarted))
                yield SimpleNamespace(type=1, code=164, value=1, timestamp=time.time)
                yield SimpleNamespace(type=1, code=164, value=0, timestamp=time.time)
                complete.set()
                await asyncio.Event().wait()

        with (patch.object(router, "RULE", rule),
              patch.object(router, "find_remote", return_value=Path("/dev/input/test-owned")),
              patch.object(router, "InputDevice", return_value=Device())):
            capture = asyncio.create_task(restarted.input_loop())
            try:
                await asyncio.wait_for(complete.wait(), 3)
                self.assertTrue(all(item["input"].get("waiting_for_release") for item in observations[:-1]))
                self.assertFalse(observations[-1]["input"].get("waiting_for_release"))
                self.assertTrue(all(item["target"] == "shield" for item in observations))
                self.assertTrue(all(not item["error"] for item in observations))
                self.assertIn("Shield is disconnected", (await self.status(restarted))["error"])
            finally:
                capture.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await capture


if __name__ == "__main__":
    unittest.main()
