import asyncio
import dataclasses
from contextlib import asynccontextmanager
import json
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import router


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.paths = router.Paths(base / "config", base / "runtime")
        self.paths.prepare()
        self.config = router.Config(
            remote_address="00:11:22:33:44:55",
            shield_host="shield.test", paired_host="shield.test",
        )
        self.backend = router.Router(self.paths, self.config)
        self.backend.client = Mock()
        self.backend.shield_connected = True
        self.backend.computer.key = AsyncMock()
        self.backend.notice = AsyncMock()
        self.device = "/org/bluez/hci0/dev_00_11_22_33_44_55"
        self.bluetooth_state = {"Paired": True, "Bonded": True, "Connected": True}
        self.real_bluez_property = router.bluez_property
        device_patch = patch.object(router, "bluez_device", new_callable=AsyncMock,
                                    return_value=self.device)
        self.bluez_device = device_patch.start()
        self.addCleanup(device_patch.stop)
        property_patch = patch.object(router, "bluez_property", new_callable=AsyncMock,
                                      side_effect=lambda device, name: self.bluetooth_state[name])
        self.bluez_property = property_patch.start()
        self.addCleanup(property_patch.stop)

    async def press(self, code, timestamp=1):
        await self.backend.key(code, 1, timestamp)
        await self.backend.key(code, 0, timestamp + 0.1)

    def dictation_commands(self):
        self.audio = {"default": "headset", "state": "idle", "sources": ["headset", "atvvoice-shield-remote"]}

        async def execute(command, timeout=15):
            if command == ["voxtype", "status", "--format", "json"]:
                return json.dumps({"alt": self.audio["state"]})
            if command == ["pactl", "--format=json", "list", "sources"]:
                return json.dumps([{"name": name} for name in self.audio["sources"]])
            if command == ["pactl", "get-default-source"]:
                return self.audio["default"]
            if command[:2] == ["pactl", "set-default-source"]:
                self.audio["default"] = command[2]
                return ""
            if command[:2] == ["voxtype", "record"]:
                if command[2] == "start":
                    self.audio["state"] = "recording"
                elif command[2] in ("stop", "cancel"):
                    self.audio["state"] = "idle"
                elif command[2] == "toggle":
                    self.audio["state"] = "idle" if self.audio["state"] == "recording" else "recording"
                return ""
            raise AssertionError(f"Unexpected desktop command: {command}")

        return patch.object(router, "run", new_callable=AsyncMock, side_effect=execute)

    @asynccontextmanager
    async def dictation_router(self, backend=None):
        backend = backend or self.backend

        async def idle():
            await asyncio.Event().wait()

        with patch.object(backend, "input_loop", side_effect=idle), patch.object(
            backend, "network_loop", side_effect=idle,
        ):
            service = asyncio.create_task(backend.serve())
            try:
                async with asyncio.timeout(2):
                    while not backend.paths.socket.exists():
                        if service.done():
                            service.result()
                        await asyncio.sleep(0.01)
                yield backend
            finally:
                service.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await service

    async def test_computer_navigation_and_home(self):
        for code in (*router.NAVIGATION, 172):
            await self.press(code)
            self.backend.computer.key.assert_awaited_with(code, repeat=False)
        self.backend.client.send_key_command.assert_not_called()

    async def test_microphone_toggles_dictation_once_per_fresh_computer_press(self):
        self.backend.computer = router.Computer()
        with self.dictation_commands() as command:
            await self.backend.key(217, 1, 1)
            self.assertEqual(self.audio["default"], "atvvoice-shield-remote")
            self.assertEqual(self.audio["state"], "recording")
            await self.backend.key(217, 1, 1.1)
            await self.backend.key(217, 2, 2)
            await self.backend.key(217, 0, 3)
            await self.backend.key(217, 2, 3.1)
            self.assertEqual(self.audio["state"], "recording")
            self.assertEqual(self.backend.last_action, "Computer: dictation toggle")
            await self.press(217, 4)
            self.assertEqual(self.audio["state"], "idle")
            self.assertEqual(self.audio["default"], "headset")
            self.assertEqual(
                [call.args[0] for call in command.await_args_list if call.args[0][:2] == ["voxtype", "record"]],
                [["voxtype", "record", "start"], ["voxtype", "record", "stop"]],
            )
        self.backend.client.send_key_command.assert_not_called()

    async def test_microphone_held_in_shield_cannot_start_dictation_after_target_switch(self):
        self.backend.computer = router.Computer()
        with self.dictation_commands() as command:
            await self.backend.select("shield")
            await self.backend.key(217, 1, 1)
            await self.backend.select("computer")
            await self.backend.key(217, 1, 2)
            await self.backend.key(217, 2, 3)
            await self.backend.key(217, 0, 4)
            command.assert_not_awaited()
            await self.press(217, 5)
            self.assertEqual(self.audio["state"], "recording")
            self.assertEqual(self.audio["default"], "atvvoice-shield-remote")
            await self.press(217, 6)
            self.assertEqual(self.audio["default"], "headset")
        self.backend.client.send_key_command.assert_not_called()

    async def test_dictation_failures_are_explicit_without_search_or_key_fallback(self):
        self.backend.computer = router.Computer()
        for error in (FileNotFoundError("voxtype missing"), RuntimeError("Voxtype daemon unavailable")):
            with self.subTest(error=error), patch.object(
                router, "run", new_callable=AsyncMock, side_effect=error,
            ) as command:
                with self.assertRaises(type(error)) as raised:
                    await self.backend.key(217, 1, 1)
                self.assertIs(raised.exception, error)
                await self.backend.key(217, 0, 2)
                command.assert_awaited_once_with(["voxtype", "status", "--format", "json"])
        self.backend.client.send_key_command.assert_not_called()

    async def test_keyboard_cancel_restores_input_without_another_remote_press(self):
        with self.dictation_commands():
            async with self.dictation_router():
                await self.press(217)
                self.assertEqual(self.audio["default"], "atvvoice-shield-remote")
                self.audio["state"] = "idle"
                await asyncio.sleep(0.4)
                self.assertEqual(self.audio["default"], "headset")
                self.assertFalse((self.paths.runtime / "dictation-input.json").exists())

    async def test_router_shutdown_cancels_remote_recording_and_restores_input(self):
        with self.dictation_commands():
            async with self.dictation_router():
                await self.press(217)
                self.assertEqual(self.audio["state"], "recording")
            self.assertEqual(self.audio["state"], "idle")
            self.assertEqual(self.audio["default"], "headset")
            self.assertFalse((self.paths.runtime / "dictation-input.json").exists())

    async def test_router_restart_recovers_default_left_by_a_crashed_remote_recording(self):
        with self.dictation_commands():
            await self.press(217)
            self.assertEqual(self.audio["default"], "atvvoice-shield-remote")
            restarted = router.Router(self.paths, self.config)
            async with self.dictation_router(restarted):
                self.assertEqual(self.audio["default"], "headset")
                self.assertEqual(self.audio["state"], "idle")
                self.assertFalse((self.paths.runtime / "dictation-input.json").exists())

    async def test_failed_recording_start_restores_input_and_discards_partial_recording(self):
        with self.dictation_commands() as command:
            execute = command.side_effect

            async def failed_start(arguments, timeout=15):
                result = await execute(arguments, timeout)
                if arguments == ["voxtype", "record", "start"]:
                    raise RuntimeError("Start acknowledgement failed")
                return result

            command.side_effect = failed_start
            with self.assertRaisesRegex(RuntimeError, "Start acknowledgement failed"):
                await self.press(217)
            self.assertEqual(self.audio["default"], "headset")
            self.assertEqual(self.audio["state"], "idle")
            self.assertFalse((self.paths.runtime / "dictation-input.json").exists())

    async def test_unavailable_remote_microphone_never_records_the_headset(self):
        with self.dictation_commands() as command:
            self.audio["sources"] = ["headset"]
            with self.assertRaisesRegex(RuntimeError, "Shield microphone is unavailable"):
                await self.press(217)
            self.assertEqual(self.audio["default"], "headset")
            self.assertEqual(self.audio["state"], "idle")
            self.assertFalse(any(
                call.args[0][:2] == ["voxtype", "record"] for call in command.await_args_list
            ))

    async def test_automatic_recording_stop_restores_input_before_transcription_finishes(self):
        with self.dictation_commands():
            async with self.dictation_router():
                await self.press(217)
                self.audio["state"] = "transcribing"
                await asyncio.sleep(0.4)
                self.assertEqual(self.audio["default"], "headset")
                self.assertEqual(self.audio["state"], "transcribing")

    async def test_user_input_change_during_recording_is_not_overwritten_on_stop(self):
        with self.dictation_commands(), self.assertLogs(router.LOG, level="INFO"):
            await self.press(217)
            self.audio["default"] = "another-microphone"
            await self.press(217, 2)
            self.assertEqual(self.audio["default"], "another-microphone")
            self.assertFalse((self.paths.runtime / "dictation-input.json").exists())

    async def test_voxtype_crash_restores_input_and_reports_the_failure(self):
        with self.dictation_commands(), self.assertLogs(router.LOG, level="ERROR"):
            async with self.dictation_router():
                await self.press(217)
                self.audio["state"] = "stopped"
                await asyncio.sleep(0.4)
                self.assertEqual(self.audio["default"], "headset")
                self.assertIn("Voxtype daemon is unavailable", self.backend.last_error)

    async def test_stopping_keyboard_dictation_does_not_change_its_input(self):
        with self.dictation_commands() as command:
            self.audio["state"] = "recording"
            await self.press(217)
            self.assertEqual(self.audio["default"], "headset")
            self.assertEqual(self.audio["state"], "idle")
            self.assertTrue(all(call.args[0][0] == "voxtype" for call in command.await_args_list))

    async def test_shield_home_is_native_not_stremio(self):
        await self.backend.select("shield")
        await self.press(172)
        self.assertEqual(self.backend.client.send_key_command.call_args_list[0].args, ("HOME", "START_LONG"))
        self.assertEqual(self.backend.client.send_key_command.call_args_list[1].args, ("HOME", "END_LONG"))
        self.backend.computer.key.assert_not_awaited()

    async def test_every_shield_mapping_and_key_release(self):
        await self.backend.select("shield")
        for code, name in router.SHIELD_KEYS.items():
            self.backend.client.send_key_command.reset_mock()
            await self.press(code)
            self.assertEqual(
                [call.args for call in self.backend.client.send_key_command.call_args_list],
                [(name, "SHORT")] if code == 116 else [(name, "START_LONG"), (name, "END_LONG")],
            )

    async def test_power_toggles_shield_once_even_when_held(self):
        await self.backend.select("shield")
        await self.backend.key(116, 1, 1)
        await self.backend.key(116, 1, 1.1)
        await self.backend.key(116, 2, 1.5)
        await self.backend.key(116, 2, 2)
        await self.backend.key(116, 0, 3)
        self.backend.client.send_key_command.assert_called_once_with("POWER", "SHORT")
        self.assertNotIn(116, self.backend.shield_pressed)
        await self.press(116, 4)
        self.assertEqual(self.backend.client.send_key_command.call_count, 2)
        self.backend.computer.key.assert_not_awaited()

    async def test_power_never_reaches_computer(self):
        await self.press(116)
        await self.backend.key(116, 2, 2)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_offline_shield_power_is_not_sent_to_computer(self):
        await self.backend.select("shield")
        self.backend.shield_connected = False
        with self.assertRaisesRegex(RuntimeError, "dropped"):
            await self.backend.key(116, 1, 1)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_power_release_after_target_switch_does_not_toggle_again(self):
        await self.backend.select("shield")
        await self.backend.key(116, 1, 1)
        await self.backend.select("computer")
        await self.backend.key(116, 0, 2)
        self.backend.client.send_key_command.assert_called_once_with("POWER", "SHORT")

    async def test_double_menu_switches_without_forwarding_menu(self):
        await self.press(139, 1)
        self.assertEqual(self.backend.target, "computer")
        await self.press(139, 1.4)
        self.assertEqual(self.backend.target, "shield")
        self.backend.client.send_key_command.assert_not_called()
        self.backend.computer.key.assert_not_awaited()
        await self.press(139, 2)
        await self.press(139, 2.4)
        self.assertEqual(self.backend.target, "computer")

    def assert_soundbar_call(self, call, port, host="soundbar.test"):
        arguments = call.args[0]
        self.assertEqual(arguments[0], "curl")
        self.assertEqual(arguments[arguments.index("--noproxy") + 1], "*")
        self.assertIn("--fail-with-body", arguments)
        self.assertEqual(arguments[arguments.index("--connect-timeout") + 1], "2")
        self.assertEqual(arguments[arguments.index("--max-time") + 1], "5")
        self.assertEqual(call.kwargs["timeout"], 6)
        self.assertEqual(arguments[-1], f"http://{host}:10000/sony/avContent")
        self.assertEqual(json.loads(arguments[arguments.index("--data") + 1]), {
            "method": "setPlayContent",
            "params": [{"uri": f"extInput:hdmi?port={port}", "output": ""}],
            "id": 1, "version": "1.2",
        })

    async def test_soundbar_maps_shield_to_hdmi_one_and_computer_to_hdmi_two(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            for target, port in (("shield", 1), ("computer", 2)):
                await self.backend.select(target)
                self.assertEqual(self.backend.target, target)
                self.assert_soundbar_call(command.await_args, port)
        self.assertEqual(command.await_count, 2)

    async def test_unconfigured_soundbar_preserves_existing_routing_without_network_calls(self):
        with patch.object(router, "run", new_callable=AsyncMock) as command:
            await self.backend.select("shield")
            await self.backend.select("computer")
        command.assert_not_awaited()

    async def test_double_menu_also_switches_soundbar_inputs(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            for first, second in ((1, 1.4), (2, 2.4)):
                await self.press(139, first)
                await self.press(139, second)
        self.assert_soundbar_call(command.await_args_list[0], 1)
        self.assert_soundbar_call(command.await_args_list[1], 2)
        self.assertEqual(self.backend.target, "computer")
        self.backend.client.send_key_command.assert_not_called()
        self.backend.computer.key.assert_not_awaited()

    async def test_panel_and_bar_target_requests_also_switch_soundbar(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            for target, port in (("shield", 1), ("computer", 2)):
                state = await self.backend.request({"command": "target", "target": target})
                self.assertEqual(state["target"], target)
                self.assert_soundbar_call(command.await_args, port)

    async def test_open_stremio_also_selects_soundbar_hdmi_two(self):
        await self.backend.select("shield")
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with (
            patch.object(self.backend.computer, "home", new_callable=AsyncMock) as home,
            patch.object(router, "run", new_callable=AsyncMock,
                         return_value='{"id": 1, "result": []}') as command,
        ):
            state = await self.backend.request({"command": "open_stremio"})
        home.assert_awaited_once()
        self.assertEqual(state["target"], "computer")
        self.assert_soundbar_call(command.await_args, 2)

    async def test_reselecting_current_target_reasserts_soundbar_input(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            await self.backend.select("computer")
            await self.backend.select("computer")
        self.assertEqual(command.await_count, 2)
        for call in command.await_args_list:
            self.assert_soundbar_call(call, 2)

    async def test_soundbar_rejections_and_invalid_responses_leave_target_unchanged(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        for response in (
            '{"id": 1, "error": [7, "Not allowed"]}',
            '{"id": 1, "error": [7, "Not allowed"], "result": []}',
            '{"id": 1}', '{"id": 1, "result": null}',
            '{"id": 2, "result": []}', '{"result": []}', '[]', 'not JSON',
        ):
            with (
                self.subTest(response=response),
                patch.object(router, "run", new_callable=AsyncMock, return_value=response),
            ):
                with self.assertRaisesRegex(RuntimeError, "Soundbar"):
                    await self.backend.select("shield")
                self.assertEqual(self.backend.target, "computer")
        self.backend.notice.assert_not_awaited()

    async def test_soundbar_network_and_http_failures_are_explicit_without_target_fallback(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        for error in (RuntimeError("curl timed out"), RuntimeError("HTTP 503"), OSError("curl missing")):
            with (
                self.subTest(error=error),
                patch.object(router, "run", new_callable=AsyncMock, side_effect=error),
            ):
                with self.assertRaisesRegex(RuntimeError, f"Soundbar HDMI1 switch failed: {error}"):
                    await self.backend.select("shield")
                self.assertEqual(self.backend.target, "computer")
        self.backend.notice.assert_not_awaited()

    async def test_soundbar_failure_releases_held_keys_but_keeps_shield_target(self):
        await self.backend.select("shield")
        await self.backend.key(103, 1, 1)
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "error": [7, "Not allowed"]}'):
            with self.assertRaisesRegex(RuntimeError, "Soundbar rejected HDMI2"):
                await self.backend.select("computer")
        self.assertEqual(self.backend.target, "shield")
        self.assertFalse(self.backend.shield_pressed)
        self.backend.client.send_key_command.assert_called_with("DPAD_UP", "END_LONG")
        self.backend.computer.key.assert_not_awaited()

    async def test_soundbar_success_clears_previous_error_only_after_confirmation(self):
        self.backend.last_error = "Previous switch failed"
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")

        async def response(*args, **kwargs):
            self.assertEqual(self.backend.target, "computer")
            self.assertEqual(self.backend.last_error, "Previous switch failed")
            return '{"id": 1, "result": []}'

        with patch.object(router, "run", new_callable=AsyncMock, side_effect=response):
            await self.backend.select("shield")
        self.assertEqual(self.backend.target, "shield")
        self.assertEqual(self.backend.last_error, "")

    async def test_unpaired_shield_selection_does_not_change_soundbar_input(self):
        self.backend.config = dataclasses.replace(
            self.config, soundbar_host="soundbar.test", paired_host="",
        )
        with patch.object(router, "run", new_callable=AsyncMock) as command:
            with self.assertRaisesRegex(ValueError, "Pair"):
                await self.backend.select("shield")
        command.assert_not_awaited()
        self.assertEqual(self.backend.target, "computer")

    async def test_ipv6_soundbar_host_is_bracketed_in_api_url(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="2001:db8::1").validate()
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            await self.backend.select("shield")
        self.assert_soundbar_call(command.await_args, 1, "[2001:db8::1]")

    def assert_soundbar_volume_call(self, call, delta):
        arguments = call.args[0]
        self.assertEqual(arguments[0], "curl")
        self.assertEqual(arguments[-1], "http://soundbar.test:10000/sony/audio")
        self.assertEqual(json.loads(arguments[arguments.index("--data") + 1]), {
            "method": "setAudioVolume",
            "params": [{"output": "", "volume": delta}],
            "id": 1, "version": "1.1",
        })
        self.assertEqual(call.kwargs["timeout"], 6)

    async def test_volume_buttons_adjust_only_soundbar_in_both_targets(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            for target in ("computer", "shield"):
                self.backend.target = target
                for code, delta in ((114, "-1"), (115, "+1")):
                    await self.press(code)
                    self.assert_soundbar_volume_call(command.await_args, delta)
                    self.assertEqual(self.backend.last_action, f"Soundbar: volume {delta}")
                    self.assertEqual(self.backend.target, target)
                    self.assertFalse(self.backend.shield_pressed)
        self.assertEqual(command.await_count, 4)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_soundbar_volume_releases_and_duplicate_presses_do_not_adjust_again(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            await self.backend.key(115, 1, 1)
            await self.backend.key(115, 1, 1.1)
            await self.backend.key(115, 0, 1.2)
        command.assert_awaited_once()
        self.assert_soundbar_volume_call(command.await_args, "+1")
        self.assertNotIn(115, self.backend.pressed)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_soundbar_volume_hold_repeats_at_bounded_rate(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            with patch.object(router.time, "monotonic", return_value=1):
                await self.backend.key(115, 1, 1)
            with patch.object(router.time, "monotonic", return_value=1.119):
                await self.backend.key(115, 2, 1.119)
            self.assertEqual(command.await_count, 1)
            with patch.object(router.time, "monotonic", return_value=1.121):
                await self.backend.key(115, 2, 1.121)
            await self.backend.key(115, 0, 1.2)
        self.assertEqual(command.await_count, 2)
        for call in command.await_args_list:
            self.assert_soundbar_volume_call(call, "+1")
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_new_soundbar_volume_press_is_not_repeat_throttled(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            with patch.object(router.time, "monotonic", return_value=1):
                await self.press(115)
            with patch.object(router.time, "monotonic", return_value=1.01):
                await self.press(114)
        self.assertEqual(command.await_count, 2)
        self.assert_soundbar_volume_call(command.await_args_list[0], "+1")
        self.assert_soundbar_volume_call(command.await_args_list[1], "-1")

    async def test_soundbar_volume_works_with_shield_offline(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        self.backend.target = "shield"
        self.backend.shield_connected = False
        self.backend.client = None
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            await self.press(114)
        self.assert_soundbar_volume_call(command.await_args, "-1")
        self.backend.computer.key.assert_not_awaited()

    async def test_soundbar_volume_failure_never_falls_back_to_target_volume(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        for target in ("computer", "shield"):
            self.backend.target = target
            for response in ('{"id": 1, "error": [7, "Not allowed"]}', 'not JSON', '{"id": 1}'):
                with (
                    self.subTest(target=target, response=response),
                    patch.object(router, "run", new_callable=AsyncMock, return_value=response),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Soundbar"):
                        await self.backend.key(115, 1, 1)
                    await self.backend.key(115, 0, 1.1)
                    self.assertEqual(self.backend.target, target)
                    self.assertEqual(self.backend.last_volume_completed, 0)
                    self.assertFalse(self.backend.shield_pressed)
            with patch.object(router, "run", new_callable=AsyncMock,
                              side_effect=RuntimeError("HTTP 503")):
                with self.assertRaisesRegex(RuntimeError, r"Soundbar volume \+1 failed: HTTP 503"):
                    await self.backend.key(115, 1, 1)
                await self.backend.key(115, 0, 1.1)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_unconfigured_soundbar_retains_target_specific_volume_controls(self):
        with patch.object(router, "run", new_callable=AsyncMock) as command:
            await self.press(114)
            self.backend.computer.key.assert_awaited_once_with(114, repeat=False)
            self.backend.computer.key.reset_mock()
            await self.backend.select("shield")
            await self.press(115)
        self.backend.computer.key.assert_not_awaited()
        self.assertEqual(
            [call.args for call in self.backend.client.send_key_command.call_args_list],
            [("VOLUME_UP", "START_LONG"), ("VOLUME_UP", "END_LONG")],
        )
        command.assert_not_awaited()

    async def test_held_soundbar_volume_release_after_target_switch_never_reaches_shield(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value='{"id": 1, "result": []}') as command:
            await self.backend.key(115, 1, 1)
            await self.backend.select("shield")
            await self.backend.key(115, 0, 2)
        self.assertEqual(command.await_count, 2)
        self.assert_soundbar_volume_call(command.await_args_list[0], "+1")
        self.assert_soundbar_call(command.await_args_list[1], 1)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_single_long_or_slow_menu_does_not_switch(self):
        await self.backend.key(139, 1, 1)
        await self.backend.key(139, 2, 1.2)
        await self.backend.key(139, 1, 1.3)
        await self.backend.key(139, 0, 2)
        await self.press(139, 3)
        self.assertEqual(self.backend.target, "computer")

    async def test_switch_releases_old_shield_keys(self):
        await self.backend.select("shield")
        await self.backend.key(103, 1, 1)
        await self.backend.select("computer")
        await self.backend.key(103, 0, 2)
        self.assertEqual(
            [call.args for call in self.backend.client.send_key_command.call_args_list],
            [("DPAD_UP", "START_LONG"), ("DPAD_UP", "END_LONG")],
        )

    async def test_ignored_buttons_never_reach_either_target(self):
        for target in ("computer", "shield"):
            await self.backend.select(target)
            for code in router.IGNORED:
                if target == "computer" and code == 217:
                    continue
                await self.press(code)
        self.backend.client.send_key_command.assert_not_called()
        self.backend.computer.key.assert_not_awaited()

    async def test_offline_shield_drops_input_without_local_fallback(self):
        await self.backend.select("shield")
        self.backend.shield_connected = False
        with self.assertRaisesRegex(RuntimeError, "dropped"):
            await self.backend.key(103, 1, 1)
        self.backend.computer.key.assert_not_awaited()
        self.backend.client.send_key_command.assert_not_called()

    async def test_shield_repeat_does_not_duplicate_long_start(self):
        await self.backend.select("shield")
        await self.backend.key(103, 1, 1)
        await self.backend.key(103, 2, 2)
        await self.backend.key(103, 0, 3)
        self.assertEqual(self.backend.client.send_key_command.call_count, 2)

    async def test_computer_repeats_only_navigation_and_volume(self):
        await self.backend.key(103, 1, 1)
        await self.backend.key(103, 2, 2)
        await self.backend.key(164, 1, 3)
        await self.backend.key(164, 2, 4)
        self.assertEqual(self.backend.computer.key.await_count, 3)
        self.backend.computer.key.assert_any_await(103, repeat=True)

    async def test_capture_discards_only_stale_computer_direction_repeats(self):
        values = [(103, 1, 99), (103, 2, 99), (108, 2, 99.9), (103, 0, 99), (114, 2, 99)]

        async def events():
            for code, value, timestamp in values:
                yield Mock(type=1, code=code, value=value, timestamp=Mock(return_value=timestamp))

        for target in ("computer", "shield"):
            self.backend.target = target
            device = Mock(
                name=router.REMOTE_NAME, uniq=self.config.remote_address,
                info=Mock(vendor=0x0955, product=0x7217), async_read_loop=events,
                active_keys=Mock(return_value=[]),
            )
            device.name = router.REMOTE_NAME
            with (
                patch.object(router, "setup_ready", return_value=True),
                patch.object(router, "find_remote", return_value=Path("/dev/input/event99")),
                patch.object(router, "InputDevice", return_value=device),
                patch.object(router, "run", new_callable=AsyncMock,
                             return_value="LIBINPUT_IGNORE_DEVICE=1\nCURRENT_TAGS=:uaccess:seat:\n"),
                patch.object(router.time, "time", return_value=100),
                patch.object(router.asyncio, "sleep", side_effect=asyncio.CancelledError),
                patch.object(self.backend, "key", new_callable=AsyncMock) as route,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self.backend.input_loop()
            expected = [value for value in values if target == "shield" or value != (103, 2, 99)]
            self.assertEqual([call.args for call in route.await_args_list], expected)
            device.close.assert_called_once()

    async def test_capture_discards_stale_soundbar_repeats_but_keeps_presses_and_releases(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")
        values = [
            (114, 1, 99), (114, 2, 99), (114, 2, 99.9), (114, 0, 99),
            (115, 1, 99), (115, 2, 99), (115, 2, 99.9), (115, 0, 99),
        ]

        async def events():
            for code, value, timestamp in values:
                yield Mock(type=1, code=code, value=value, timestamp=Mock(return_value=timestamp))

        for target in ("computer", "shield"):
            self.backend.target = target
            device = Mock(
                name=router.REMOTE_NAME, uniq=self.config.remote_address,
                info=Mock(vendor=0x0955, product=0x7217), async_read_loop=events,
                active_keys=Mock(return_value=[]),
            )
            device.name = router.REMOTE_NAME
            with (
                patch.object(router, "setup_ready", return_value=True),
                patch.object(router, "find_remote", return_value=Path("/dev/input/event99")),
                patch.object(router, "InputDevice", return_value=device),
                patch.object(router, "run", new_callable=AsyncMock,
                             return_value="LIBINPUT_IGNORE_DEVICE=1\nCURRENT_TAGS=:uaccess:seat:\n"),
                patch.object(router.time, "time", return_value=100),
                patch.object(router.asyncio, "sleep", side_effect=asyncio.CancelledError),
                patch.object(self.backend, "key", new_callable=AsyncMock) as route,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self.backend.input_loop()
            self.assertEqual(
                [call.args for call in route.await_args_list],
                [event for event in values if event not in ((114, 2, 99), (115, 2, 99))],
            )
            device.close.assert_called_once()

    async def test_menu_direction_chord_navigates_without_switching_target(self):
        await self.backend.key(139, 1, 1)
        await self.backend.key(108, 1, 1.1)
        await self.backend.key(108, 2, 1.2)
        await self.backend.key(108, 0, 1.3)
        await self.backend.key(139, 0, 1.4)
        self.backend.computer.key.assert_any_await(108, repeat=False)
        self.backend.computer.key.assert_any_await(108, repeat=True)
        await self.press(139, 1.45)
        self.assertEqual(self.backend.target, "computer")
        self.backend.client.send_key_command.assert_not_called()

    async def test_menu_direction_chord_keeps_native_shield_navigation(self):
        await self.backend.select("shield")
        await self.backend.key(139, 1, 1)
        await self.press(108, 1.1)
        self.backend.client.send_key_command.assert_any_call("DPAD_DOWN", "START_LONG")
        self.backend.client.send_key_command.assert_any_call("DPAD_DOWN", "END_LONG")
        self.backend.computer.key.assert_not_awaited()

    async def test_unpaired_target_selection_is_explicitly_rejected(self):
        self.backend.config = dataclasses.replace(self.config, paired_host="")
        with self.assertRaisesRegex(ValueError, "Pair"):
            await self.backend.select("shield")
        self.assertEqual(self.backend.target, "computer")

    async def test_unknown_key_is_not_forwarded(self):
        with self.assertRaisesRegex(ValueError, "not forwarded"):
            await self.backend.key(999, 1, 1)
        self.backend.computer.key.assert_not_awaited()

    async def test_unblocking_requires_scoped_rule(self):
        with patch.object(router, "rule_installed", return_value=False), patch.object(router, "run", new_callable=AsyncMock) as command:
            with self.assertRaisesRegex(ValueError, "input setup"):
                await self.backend.request({"command": "connect_remote"})
            command.assert_not_awaited()

    async def test_unpaired_remote_requires_bluetooth_pairing_not_reconnect(self):
        self.bluetooth_state["Paired"] = False
        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router, "run", new_callable=AsyncMock,
                         return_value="Paired: no\nBonded: no\nConnected: yes"),
            patch.object(self.backend, "connect_bluetooth", new_callable=AsyncMock,
                         side_effect=RuntimeError("Unpaired connection attempted")) as connect,
        ):
            with self.assertRaisesRegex(ValueError, "not paired"):
                await self.backend.request({"command": "connect_remote"})
            connect.assert_not_awaited()

    async def test_reconnect_already_disconnected_remote_is_idempotent(self):
        self.backend.input_captured = True
        self.bluetooth_state["Connected"] = False

        async def command(arguments, **kwargs):
            if arguments[1] == "info":
                return "Paired: yes\nBonded: yes\nConnected: no"
            if arguments[1] == "disconnect":
                raise RuntimeError("Failed to disconnect: org.bluez.Error.NotConnected")
            return ""

        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router, "run", new_callable=AsyncMock, side_effect=command),
            patch.object(self.backend, "connect_bluetooth", new_callable=AsyncMock) as connect,
        ):
            await self.backend.request({"command": "connect_remote"})
            connect.assert_awaited_once_with(self.config.remote_address)

    async def test_bluetooth_command_failures_include_stdout_diagnostics(self):
        process = Mock()
        process.returncode = 1
        process.communicate = AsyncMock(return_value=(
            b"Failed to disconnect: org.bluez.Error.NotConnected", b"",
        ))
        with patch.object(router.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                          return_value=process):
            with self.assertRaisesRegex(RuntimeError, "org.bluez.Error.NotConnected"):
                await router.run(["bluetoothctl", "disconnect", self.config.remote_address])

    async def test_rule_without_completed_setup_never_unblocks(self):
        with patch.object(router, "rule_installed", return_value=True), patch.object(router, "run", new_callable=AsyncMock) as command:
            with self.assertRaisesRegex(ValueError, "input setup"):
                await self.backend.request({"command": "connect_remote"})
            command.assert_not_awaited()

    async def test_setup_receipt_must_match_selected_remote(self):
        receipt = self.paths.config / "input-setup.json"
        with patch.object(router, "rule_installed", return_value=True):
            self.assertFalse(router.setup_ready(self.paths, self.config.remote_address))
            router.write_private_json(receipt, {"schema": 1, "address": self.config.remote_address})
            self.assertTrue(router.setup_ready(self.paths, self.config.remote_address))
            self.assertFalse(router.setup_ready(self.paths, "AA:11:22:33:44:55"))
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            receipt.write_text("{")
            with self.assertLogs(router.LOG, level="ERROR"):
                self.assertFalse(router.setup_ready(self.paths, self.config.remote_address))

    async def test_setup_stays_visible_until_matching_service_is_enabled(self):
        router.write_private_json(self.paths.config / "input-setup.json",
                                  {"schema": 1, "address": self.config.remote_address})
        with (
            patch.object(router, "rule_installed", return_value=True),
            patch.dict(router.os.environ, {"XDG_DATA_HOME": str(self.paths.config / "data")}),
        ):
            self.assertFalse(self.backend.state()["setup_complete"])
            self.paths.service.parent.mkdir(parents=True)
            self.paths.service.write_text(router.service_text())
            self.assertFalse(self.backend.state()["setup_complete"])
            enabled = self.paths.service.parent / "graphical-session.target.wants" / router.SERVICE
            enabled.parent.mkdir()
            enabled.symlink_to(self.paths.service)
            self.assertFalse(self.backend.state()["setup_complete"])
            self.paths.microphone_binary.parent.mkdir(parents=True)
            self.paths.microphone_binary.write_text("#!/bin/sh\n")
            self.paths.microphone_binary.chmod(0o755)
            self.paths.microphone_service.write_text(
                router.microphone_service_text(self.paths, self.config.remote_address),
            )
            enabled.with_name(router.MICROPHONE_SERVICE).symlink_to(self.paths.microphone_service)
            self.assertTrue(self.backend.state()["setup_complete"])
            self.paths.service.write_text("# Managed by fredrik.shield-remote\noutdated")
            self.assertFalse(self.backend.state()["setup_complete"])

    async def test_connect_recreates_input_before_unblocking(self):
        self.backend.input_captured = True
        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router, "run", new_callable=AsyncMock) as command,
            patch.object(self.backend, "connect_bluetooth", new_callable=AsyncMock) as connect,
        ):
            await self.backend.request({"command": "connect_remote"})
            connect.assert_awaited_once_with(self.config.remote_address)
        self.assertEqual(
            [call.args[0] for call in command.await_args_list],
            [
                ["busctl", "--system", "call", "org.bluez", self.device,
                 "org.bluez.Device1", "Disconnect"],
                ["busctl", "--system", "set-property", "org.bluez", self.device,
                 "org.bluez.Device1", "Blocked", "b", "false"],
            ],
        )

    async def test_historical_power_tag_does_not_reject_safe_current_tags(self):
        properties = (
            "LIBINPUT_IGNORE_DEVICE=1\n"
            "TAGS=:power-switch:uaccess:seat:\n"
            "CURRENT_TAGS=:uaccess:seat:\n"
        )
        self.assertTrue(router.input_safe(properties))

    async def test_current_power_tag_and_missing_udev_state_are_unsafe(self):
        for properties in (
            "",
            "LIBINPUT_IGNORE_DEVICE=1\n",
            "LIBINPUT_IGNORE_DEVICE=1\nCURRENT_TAGS=:power-switch:uaccess:\n",
            "LIBINPUT_IGNORE_DEVICE=0\nCURRENT_TAGS=:uaccess:\n",
        ):
            self.assertFalse(router.input_safe(properties))

    async def test_failed_connect_restores_blocked_state(self):
        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router, "run", new_callable=AsyncMock) as command,
            patch.object(self.backend, "connect_bluetooth", new_callable=AsyncMock,
                         side_effect=RuntimeError("Connection timed out")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Connection timed out"):
                await self.backend.request({"command": "connect_remote"})
            self.assertEqual(
                [call.args[0][-3:] for call in command.await_args_list[-2:]],
                [["Blocked", "b", "true"], [self.device, "org.bluez.Device1", "Disconnect"]],
            )

    async def test_connect_owns_and_stops_only_its_discovery_process(self):
        scan = Mock()
        scan.returncode = None
        scan.communicate = AsyncMock(return_value=(b"", b""))
        with (
            patch.object(router.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                         return_value=scan) as start,
            patch.object(router, "run", new_callable=AsyncMock,
                         side_effect=["Discovering: yes", ""]) as command,
        ):
            await self.backend.connect_bluetooth(self.config.remote_address)
        self.assertEqual(start.await_args.args[:5], ("bluetoothctl", "--timeout", "30", "scan", "on"))
        command.assert_awaited_with(
            ["busctl", "--system", "call", "org.bluez", self.device, "org.bluez.Device1", "Connect"],
            timeout=25,
        )
        scan.terminate.assert_called_once()
        scan.communicate.assert_awaited_once()

    async def test_bluetooth_connection_is_verified_even_after_successful_call(self):
        self.bluetooth_state["Connected"] = False
        scan = Mock()
        scan.returncode = None
        scan.communicate = AsyncMock(return_value=(b"", b""))
        with (
            patch.object(router.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                         return_value=scan),
            patch.object(router, "run", new_callable=AsyncMock,
                         side_effect=["Discovering: yes", ""]),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                await self.backend.connect_bluetooth(self.config.remote_address)
        scan.terminate.assert_called_once()
        scan.communicate.assert_awaited_once()

    async def test_bluez_property_requires_a_typed_boolean(self):
        for data in (True, False, "yes", None):
            with patch.object(router, "run", new_callable=AsyncMock,
                              return_value=json.dumps({"type": "b", "data": data})):
                if type(data) is bool:
                    self.assertIs(await self.real_bluez_property(self.device, "Paired"), data)
                else:
                    with self.assertRaisesRegex(RuntimeError, "invalid Paired"):
                        await self.real_bluez_property(self.device, "Paired")

    async def test_pairing_requires_rule_and_repair_requires_explicit_confirmation(self):
        with patch.object(router, "run", new_callable=AsyncMock) as command:
            with self.assertRaisesRegex(ValueError, "input setup"):
                await self.backend.request({"command": "pair_remote"})
            command.assert_not_awaited()
            with self.assertRaisesRegex(ValueError, "Confirm"):
                await self.backend.request({"command": "repair_remote"})
            command.assert_not_awaited()

    async def test_pair_unblocks_before_pair_and_verifies_bond_and_capture(self):
        self.bluetooth_state.update(Paired=False, Bonded=False, Connected=False, Blocked=True)
        scan = Mock(returncode=None)
        scan.communicate = AsyncMock(return_value=(b"", b""))
        actions = []

        async def command(arguments, **kwargs):
            if arguments[0] == "bluetoothctl":
                return "Discovering: yes"
            actions.append(arguments)
            if "set-property" in arguments:
                self.bluetooth_state[arguments[-3]] = arguments[-1] == "true"
            elif arguments[-1] == "Pair":
                self.assertFalse(self.bluetooth_state["Blocked"])
                self.assertTrue(self.backend.remote_pairing)
                self.bluetooth_state.update(Paired=True, Bonded=True)
            elif arguments[-1] == "Connect":
                self.bluetooth_state["Connected"] = True
                self.backend.input_captured = True
            return ""

        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                         return_value=scan),
            patch.object(router, "run", new_callable=AsyncMock, side_effect=command),
        ):
            await self.backend.request({"command": "pair_remote"})
        self.assertTrue(self.bluetooth_state["Paired"])
        self.assertTrue(self.bluetooth_state["Bonded"])
        self.assertTrue(self.bluetooth_state["Trusted"])
        self.assertTrue(self.backend.input_captured)
        self.assertFalse(self.backend.remote_pairing)
        self.assertFalse(any("RemoveDevice" in action for action in actions))
        scan.terminate.assert_called_once()

    async def test_pair_timeout_cancels_and_restores_safety_without_forgetting(self):
        self.bluetooth_state.update(Paired=False, Bonded=False, Connected=False, Blocked=True)
        scan = Mock(returncode=None)
        scan.communicate = AsyncMock(return_value=(b"", b""))
        actions = []

        async def command(arguments, **kwargs):
            if arguments[0] == "bluetoothctl":
                return "Discovering: yes"
            actions.append(arguments)
            if arguments[-1] == "Pair":
                raise RuntimeError("busctl timed out")
            if "set-property" in arguments:
                self.bluetooth_state[arguments[-3]] = arguments[-1] == "true"
            return ""

        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                         return_value=scan),
            patch.object(router, "run", new_callable=AsyncMock, side_effect=command),
        ):
            with self.assertRaisesRegex(RuntimeError, "pairing failed: busctl timed out"):
                await self.backend.request({"command": "pair_remote"})
        self.assertTrue(self.bluetooth_state["Blocked"])
        self.assertFalse(self.backend.remote_pairing)
        self.assertEqual(sum(action[-1] == "CancelPairing" for action in actions), 2)
        self.assertFalse(any("RemoveDevice" in action for action in actions))
        scan.terminate.assert_called_once()

    async def test_confirmed_repair_removes_only_selected_bond_then_pairs(self):
        scan = Mock(returncode=None)
        scan.communicate = AsyncMock(return_value=(b"", b""))
        actions = []

        async def command(arguments, **kwargs):
            if arguments[0] == "bluetoothctl":
                return "Discovering: yes"
            actions.append(arguments)
            if "RemoveDevice" in arguments:
                self.bluetooth_state.update(Paired=False, Bonded=False, Connected=False)
            elif arguments[-1] == "Pair":
                self.bluetooth_state.update(Paired=True, Bonded=True)
            elif arguments[-1] == "Connect":
                self.bluetooth_state["Connected"] = True
                self.backend.input_captured = True
            return ""

        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                         return_value=scan),
            patch.object(router, "run", new_callable=AsyncMock, side_effect=command),
        ):
            await self.backend.request({"command": "repair_remote", "confirm": True})
        removals = [action for action in actions if "RemoveDevice" in action]
        self.assertEqual(removals, [[
            "busctl", "--system", "call", "org.bluez", "/org/bluez/hci0",
            "org.bluez.Adapter1", "RemoveDevice", "o", self.device,
        ]])
        self.assertTrue(self.backend.input_captured)
        self.assertEqual(self.backend.config, self.config)

    async def test_no_active_pairing_cancellation_is_expected_not_a_failure(self):
        with (
            patch.object(router, "run", new_callable=AsyncMock,
                         side_effect=RuntimeError("Call failed: Does Not Exist")),
            self.assertLogs(router.LOG, level="INFO"),
        ):
            await router.bluez_cancel_pairing(self.device)

    async def test_disconnect_cancels_pending_connect_even_when_not_connected(self):
        self.bluetooth_state["Connected"] = False
        with patch.object(router, "run", new_callable=AsyncMock, return_value="") as command:
            await router.bluez_disconnect(self.device)
        command.assert_awaited_once_with(
            ["busctl", "--system", "call", "org.bluez", self.device,
             "org.bluez.Device1", "Disconnect"], timeout=5,
        )

    async def test_disconnected_bluez_response_is_idempotent_but_other_errors_are_reported(self):
        with (
             patch.object(router, "run", new_callable=AsyncMock,
                          side_effect=RuntimeError("Call failed: Not Connected")),
             self.assertLogs(router.LOG, level="INFO"),
        ):
             await router.bluez_disconnect(self.device)
        with patch.object(router, "run", new_callable=AsyncMock,
                           side_effect=RuntimeError("Call failed: Connection timed out")):
             with self.assertRaisesRegex(RuntimeError, "Connection timed out"):
                 await router.bluez_disconnect(self.device)

    async def test_auto_connected_response_requires_verified_live_connection(self):
        with (
            patch.object(router, "run", new_callable=AsyncMock,
                          side_effect=RuntimeError("Call failed: Already Connected")),
            self.assertLogs(router.LOG, level="INFO"),
        ):
            await router.bluez_connect(self.device)
        self.bluetooth_state["Connected"] = False
        with patch.object(router, "run", new_callable=AsyncMock,
                           side_effect=RuntimeError("Call failed: Already Connected")):
            with self.assertRaisesRegex(RuntimeError, "Already Connected"):
                 await router.bluez_connect(self.device)

    async def test_expired_pairing_code_is_not_sent(self):
        client = Mock()
        client.async_finish_pairing = AsyncMock()
        self.backend.pairing_client = client
        self.backend.pairing_deadline = time.monotonic() - 1
        with self.assertRaisesRegex(ValueError, "timed out"):
            await self.backend.pair_finish("ab12cd")
        client.async_finish_pairing.assert_not_awaited()
        self.assertIsNone(self.backend.pairing_client)

    async def test_status_remains_available_during_a_command(self):
        async with self.backend.command_lock:
            state = await asyncio.wait_for(self.backend.request({"command": "status"}), 1)
        self.assertTrue(state["running"])

    async def test_private_socket_status_and_duplicate_backend_protection(self):
        service = asyncio.create_task(self.backend.serve())
        try:
            async with asyncio.timeout(2):
                while not self.paths.socket.exists():
                    if service.done():
                        service.result()
                    await asyncio.sleep(0.01)
            response = await asyncio.to_thread(
                router.client_request, self.paths, {"command": "status"},
            )
            self.assertTrue(response["ok"])
            self.assertTrue(response["state"]["running"])
            self.assertEqual(response["state"]["schema"], 1)
            self.assertEqual(stat.S_IMODE(self.paths.socket.stat().st_mode), 0o600)
            duplicate = router.Router(self.paths, self.config)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                await duplicate.serve()
            self.assertTrue(self.paths.socket.exists())
        finally:
            service.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await service
        self.assertFalse(self.paths.socket.exists())

    async def test_soundbar_failure_is_reported_through_socket_state_and_notification(self):
        self.backend.config = dataclasses.replace(self.config, soundbar_host="soundbar.test")

        async def idle():
            await asyncio.Event().wait()

        with (
            patch.object(self.backend, "input_loop", side_effect=idle),
            patch.object(self.backend, "network_loop", side_effect=idle),
            patch.object(router, "run", new_callable=AsyncMock,
                         return_value='{"id": 1, "error": [7, "Not allowed"]}'),
            self.assertLogs(router.LOG, level="ERROR"),
        ):
            service = asyncio.create_task(self.backend.serve())
            try:
                async with asyncio.timeout(2):
                    while not self.paths.socket.exists():
                        if service.done():
                            service.result()
                        await asyncio.sleep(0.01)
                response = await asyncio.to_thread(
                    router.client_request, self.paths, {"command": "target", "target": "shield"},
                )
                self.assertFalse(response["ok"])
                self.assertIn("Soundbar rejected HDMI1", response["error"])
                self.assertEqual(response["state"]["target"], "computer")
                self.assertEqual(response["state"]["error"], response["error"])
                self.backend.notice.assert_awaited_once_with(response["error"])
            finally:
                service.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await service

    async def test_rule_reload_failure_invalidates_previous_setup_receipt(self):
        receipt = self.paths.config / "input-setup.json"
        router.write_private_json(receipt, {"schema": 1, "address": self.config.remote_address})
        with (
            patch.object(router, "RULE", self.paths.config / "test.rules"),
            patch.object(router, "install_microphone"),
            patch.dict(router.os.environ, {"XDG_CONFIG_HOME": str(self.paths.config)}),
            patch.object(router.sys.stdin, "isatty", return_value=False),
            patch.object(router.subprocess, "run", side_effect=[
                Mock(), subprocess.CalledProcessError(1, "udevadm"),
            ]) as command,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                router.install(self.paths, self.config)
        self.assertEqual(command.call_count, 2)
        self.assertFalse(receipt.exists())
        self.assertFalse(self.paths.service.exists())

    async def test_service_activation_failure_never_completes_setup(self):
        receipt = self.paths.config / "input-setup.json"
        router.write_private_json(receipt, {"schema": 1, "address": self.config.remote_address})
        with (
            patch.object(router, "RULE", self.paths.config / "test.rules"),
            patch.object(router, "install_microphone"),
            patch.object(router.sys.stdin, "isatty", return_value=False),
            patch.object(router.subprocess, "run", side_effect=[
                Mock(), Mock(), Mock(), Mock(), subprocess.CalledProcessError(1, "systemctl"),
            ]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                router.install(self.paths, self.config)
        self.assertTrue(self.paths.service.exists())
        self.assertFalse(receipt.exists())
        self.assertFalse(self.backend.state()["setup_complete"])

    async def test_install_writes_receipt_only_after_service_activation(self):
        receipt = self.paths.config / "input-setup.json"

        def command(arguments, **kwargs):
            self.assertFalse(receipt.exists())
            if arguments[-1] == router.SERVICE:
                self.assertIn(arguments, [
                    ["systemctl", "--user", "enable", router.SERVICE],
                    ["systemctl", "--user", "restart", router.SERVICE],
                ])
                self.assertEqual(self.paths.service.read_text(), router.service_text())
            return Mock()

        with (
            patch.object(router, "RULE", self.paths.config / "test.rules"),
            patch.object(router, "install_microphone"),
            patch.object(router.subprocess, "run", side_effect=command) as commands,
        ):
            router.install(self.paths, self.config)
        self.assertEqual(json.loads(receipt.read_text())["address"], self.config.remote_address)
        self.assertEqual([call.args[0][2] for call in commands.call_args_list[-2:]],
                         ["enable", "restart"])

    async def test_generated_service_escapes_systemd_specifiers_in_paths(self):
        with (
            patch.object(router.sys, "executable", "/home/test%user/venv/bin/python"),
            patch.object(router, "PLUGIN", Path("/home/test user%/plugins")),
        ):
            unit = router.service_text()
        self.assertIn('ExecStart="/home/test%%user/venv/bin/python" "/home/test user%%/plugins/router.py" serve', unit)

    async def test_stremio_seek_uses_native_mpris_capability_and_microseconds(self):
        computer = router.Computer()
        player = "org.mpris.MediaPlayer2.stremio"
        responses = [
            json.dumps({"type": "as", "data": [[player]]}),
            json.dumps({"type": "s", "data": "Playing"}),
            json.dumps({"type": "b", "data": True}),
            "",
        ]
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=responses) as command:
            await computer.media(168)
            self.assertEqual(command.await_args.args[0][-3:], ["Seek", "x", "-10000000"])

    async def test_unsupported_mpris_seek_is_reported_without_sending(self):
        computer = router.Computer()
        player = "org.mpris.MediaPlayer2.stremio"
        responses = [
            json.dumps({"type": "as", "data": [player]}),
            json.dumps({"type": "s", "data": "Playing"}),
            json.dumps({"type": "b", "data": False}),
        ]
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=responses) as command:
            with self.assertRaisesRegex(RuntimeError, "does not support seeking"):
                await computer.media(208)
            self.assertEqual(command.await_count, 3)

    async def test_pairing_success_stores_host_and_clears_pairing(self):
        client = Mock()
        client.async_finish_pairing = AsyncMock()
        self.backend.pairing_client = client
        self.backend.config = dataclasses.replace(self.config, paired_host="")
        await self.backend.pair_finish("ab12cd")
        client.async_finish_pairing.assert_awaited_once_with("AB12CD")
        client.disconnect.assert_called_once()
        self.assertIsNone(self.backend.pairing_client)
        self.assertEqual(router.load_config(self.paths).paired_host, "shield.test")

    async def test_pairing_rejection_does_not_mark_host_paired(self):
        client = Mock()
        client.async_finish_pairing = AsyncMock(side_effect=router.InvalidAuth())
        self.backend.pairing_client = client
        self.backend.config = dataclasses.replace(self.config, paired_host="")
        with self.assertRaises(router.InvalidAuth):
            await self.backend.pair_finish("ab12cd")
        self.assertEqual(self.backend.config.paired_host, "")
        self.assertIsNone(self.backend.pairing_client)

    async def test_configuration_is_private_and_strict(self):
        await self.backend.configure({"shield_host": "shield.test"})
        self.assertEqual(router.load_config(self.paths), self.config)
        file = self.paths.config / "config.json"
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.config.stat().st_mode), 0o700)
        with self.assertRaises(ValueError):
            await self.backend.configure({"paired_host": "untrusted.test"})
        with self.assertRaises(ValueError):
            await self.backend.configure({"shield_host": "http://shield.test:6466"})
        with self.assertRaises(ValueError):
            await self.backend.configure({"home_app": "demo"})

    async def test_soundbar_configuration_is_private_optional_and_preserves_existing_settings(self):
        client = self.backend.client
        await self.backend.configure({"soundbar_host": "soundbar.test"})
        config = router.load_config(self.paths)
        self.assertEqual(config, dataclasses.replace(self.config, soundbar_host="soundbar.test"))
        self.assertEqual(self.backend.state()["soundbar"], {"host": "soundbar.test"})
        self.assertEqual(stat.S_IMODE((self.paths.config / "config.json").stat().st_mode), 0o600)
        client.disconnect.assert_not_called()
        self.assertTrue(self.backend.paired)
        await self.backend.configure({"soundbar_host": ""})
        self.assertEqual(router.load_config(self.paths), self.config)
        for host in ("http://soundbar.test:10000", "soundbar.test:10000", "-invalid", "with space"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "Soundbar"):
                    await self.backend.configure({"soundbar_host": host})

    async def test_host_change_disconnects_and_requires_new_pairing(self):
        client = self.backend.client
        await self.backend.configure({"shield_host": "other.test"})
        client.disconnect.assert_called_once()
        self.assertFalse(self.backend.paired)
        self.assertFalse(self.backend.shield_connected)

    async def test_scope_rule_is_specific_and_power_safe(self):
        rule = router.rule_text("AA:11:22:33:44:55")
        self.assertIn('ATTRS{uniq}=="aa:11:22:33:44:55"', rule)
        self.assertIn('ENV{LIBINPUT_IGNORE_DEVICE}="1"', rule)
        self.assertIn('TAG-="power-switch"', rule)
        self.assertIn('TAG+="uaccess"', rule)
        with self.assertRaises(ValueError):
            router.rule_text('bad"\nTAG+="uaccess"')

    async def test_home_raises_existing_theater_without_launching_duplicates(self):
        computer = router.Computer()
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            '{"type": "b", "data": [true]}', "",
        ]) as command:
            await computer.home()
        self.assertEqual(command.await_count, 2)
        self.assertEqual(command.await_args.args[0][-1], "Raise")

    async def test_panel_open_theater_selects_computer_after_opening(self):
        await self.backend.select("shield")
        with patch.object(self.backend.computer, "home", new_callable=AsyncMock) as home:
            state = await self.backend.request({"command": "open_theater"})
        home.assert_awaited_once()
        self.assertEqual(state["target"], "computer")
        self.assertIn("Raised Home Theater", state["last_action"])

    async def test_failed_panel_open_does_not_switch_control_target(self):
        await self.backend.select("shield")
        with patch.object(self.backend.computer, "home", new_callable=AsyncMock,
                          side_effect=RuntimeError("TV is not connected")):
            with self.assertRaisesRegex(RuntimeError, "TV is not connected"):
                await self.backend.request({"command": "open_stremio"})
        self.assertEqual(self.backend.target, "shield")

    async def test_tv_selection_is_private_and_updates_computer_controls(self):
        await self.backend.configure({"tv_output": "HDMI-A-1"})
        self.assertEqual(router.load_config(self.paths).tv_output, "HDMI-A-1")
        self.assertEqual(self.backend.computer.tv_output, "HDMI-A-1")
        self.assertEqual(self.backend.state()["tv_output"], "HDMI-A-1")
        self.assertEqual(stat.S_IMODE((self.paths.config / "config.json").stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, "TV output"):
            await self.backend.configure({"tv_output": 'HDMI-A-1"; other_action()'})

    async def test_stremio_direction_pad_uses_spatial_focus_not_mouse_or_arrows(self):
        for code, action in router.FOCUS_DIRECTIONS.items():
            with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
                '{"type": "b", "data": [false]}',
                '{"class": "com.stremio.Stremio", "address": "0x123"}', '{"activate": false}',
            ]) as command:
                await router.Computer().key(code)
            command.assert_awaited_with([
                "/usr/bin/python", str(router.PLUGIN / "navigation.py"), action,
            ], timeout=8)

    async def test_stremio_select_activates_focused_control_with_enter(self):
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            '{"type": "b", "data": [false]}',
            '{"class": "Stremio", "address": "0x123"}', '{"activate": true}', "ok",
        ]) as command:
            await router.Computer().key(353)
        self.assertIn('key="Return",window="address:0x123"', command.await_args.args[0][-1])

    async def test_select_without_existing_focus_only_initializes_selection(self):
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            '{"type": "b", "data": [false]}',
            '{"class": "Stremio", "address": "0x123"}', '{"activate": false}',
        ]) as command:
            await router.Computer().key(353)
        self.assertEqual(command.await_count, 3)
        self.assertEqual(command.await_args.args[0][-1], "select")

    async def test_stremio_repeats_do_not_accumulate_slow_navigation_calls(self):
        computer = router.Computer()
        computer.last_focus_completed = 1
        with (
            patch.object(router.time, "monotonic", return_value=1.119),
            patch.object(router, "run", new_callable=AsyncMock) as command,
        ):
            await computer.focus("down", {}, repeat=True)
        command.assert_not_awaited()
        with (
            patch.object(router.time, "monotonic", return_value=1.121),
            patch.object(router, "run", new_callable=AsyncMock, return_value='{"activate": false}') as command,
        ):
            await computer.focus("down", {}, repeat=True)
        command.assert_awaited_once()

    async def test_new_press_is_not_throttled_like_repeat(self):
        computer = router.Computer()
        computer.last_focus_completed = 1
        with (
            patch.object(router.time, "monotonic", return_value=1.05),
            patch.object(router, "run", new_callable=AsyncMock, return_value='{"activate": false}') as command,
        ):
            await computer.focus("right", {})
        command.assert_awaited_once()

    async def test_invalid_navigation_response_never_activates_control(self):
        for response in ('[]', '{"activate": "yes"}', '{"activate": true}'):
            with patch.object(router, "run", new_callable=AsyncMock, return_value=response) as command:
                with self.assertRaisesRegex(RuntimeError, "navigation"):
                    await router.Computer().focus("down", {})
            command.assert_awaited_once()

    async def test_navigation_failure_is_explicit_without_mouse_fallback(self):
        with patch.object(router, "run", new_callable=AsyncMock,
                          side_effect=RuntimeError("No unique Stremio accessibility application")) as command:
            with self.assertRaisesRegex(RuntimeError, "accessibility"):
                await router.Computer().focus("right", {})
        command.assert_awaited_once()

    async def test_stremio_back_uses_application_history_shortcut(self):
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            '{"type": "b", "data": [false]}',
            '{"class": "Stremio", "address": "0x123"}',
            '{"activate": false, "key": "BackSpace"}', "ok",
        ]) as command:
            await router.Computer().key(158)
        self.assertIn('key="BackSpace",window="address:0x123"', command.await_args.args[0][-1])

    async def test_stremio_back_closes_popup_before_navigating_history(self):
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            '{"type": "b", "data": [false]}',
            '{"class": "Stremio", "address": "0x123"}',
            '{"activate": false, "key": "Escape"}', "ok",
        ]) as command:
            await router.Computer().key(158)
        self.assertIn('key="Escape",window="address:0x123"', command.await_args.args[0][-1])

    async def test_invalid_back_shortcut_is_not_forwarded(self):
        for key in ("Power", None, [], {}):
            with patch.object(router, "run", new_callable=AsyncMock,
                              return_value=json.dumps({"activate": False, "key": key})) as command:
                with self.assertRaisesRegex(RuntimeError, "Back navigation response"):
                    await router.Computer().focus("back", {})
            command.assert_awaited_once()

    async def test_other_apps_keep_original_navigation_keys(self):
        for code, name in router.NAVIGATION.items():
            with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
                '{"type": "b", "data": [false]}',
                '{"class": "kitty", "address": "0x456"}', "ok",
            ]) as command:
                await router.Computer().key(code)
            self.assertIn(f'key="{name}"', command.await_args.args[0][-1])
            self.assertNotIn("window=", command.await_args.args[0][-1])

    async def test_invalid_active_window_is_reported_without_forwarding(self):
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            '{"type": "b", "data": [false]}', "[]",
        ]) as command:
            with self.assertRaisesRegex(RuntimeError, "active window"):
                await router.Computer().key(103)
        self.assertEqual(command.await_count, 2)

    async def test_legacy_config_migration_preserves_remote_and_tv_credentials(self):
        file = self.paths.config / "config.json"
        key = self.paths.config / "client.key"
        key.write_text("test certificate key")
        for old_app in ("demo", "stremio"):
            old = dataclasses.asdict(self.config)
            old.pop("tv_output")
            old.pop("soundbar_host")
            file.write_text(json.dumps({"schema": 1, **old, "home_app": old_app}))
            self.assertEqual(router.load_config(self.paths), self.config)
            migrated = json.loads(file.read_text())
            self.assertEqual(migrated, {"schema": 4, **dataclasses.asdict(self.config)})
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(key.read_text(), "test certificate key")

    async def test_schema_two_migration_preserves_pairing_and_defaults_tv_selection(self):
        old = dataclasses.asdict(self.config)
        old.pop("tv_output")
        old.pop("soundbar_host")
        file = self.paths.config / "config.json"
        file.write_text(json.dumps({"schema": 2, **old}))
        self.assertEqual(router.load_config(self.paths), self.config)
        self.assertEqual(json.loads(file.read_text()), {"schema": 4, **dataclasses.asdict(self.config)})

    async def test_schema_three_migration_preserves_tv_and_defaults_soundbar_to_disabled(self):
        config = dataclasses.replace(self.config, tv_output="HDMI-A-1")
        old = dataclasses.asdict(config)
        old.pop("soundbar_host")
        file = self.paths.config / "config.json"
        file.write_text(json.dumps({"schema": 3, **old}))
        self.assertEqual(router.load_config(self.paths), config)
        self.assertEqual(json.loads(file.read_text()), {"schema": 4, **dataclasses.asdict(config)})
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    async def test_schema_four_missing_soundbar_field_is_rejected(self):
        payload = {"schema": 4, **dataclasses.asdict(self.config)}
        payload.pop("soundbar_host")
        (self.paths.config / "config.json").write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "configuration fields"):
            router.load_config(self.paths)

    async def test_cold_config_and_auto_detect_before_first_pairing(self):
        self.assertEqual(router.load_config(self.paths), router.Config())
        self.assertFalse(router.client_request(self.paths, {"command": "status"})["state"]["setup_complete"])
        with patch.object(router, "run", new_callable=AsyncMock,
                          return_value="Device 00:11:22:33:44:55 NVIDIA SHIELD Remote") as command:
            self.assertEqual(await router.auto_remote(paired_only=False), self.config.remote_address)
            command.assert_awaited_once_with(["bluetoothctl", "devices"])
            self.assertEqual(await router.auto_remote(), self.config.remote_address)
            command.assert_awaited_with(["bluetoothctl", "devices", "Paired"])

    async def test_invalid_configuration_schema_is_reported(self):
        for schema in (99, True, [], {}):
            (self.paths.config / "config.json").write_text(json.dumps({"schema": schema}))
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                router.load_config(self.paths)


if __name__ == "__main__":
    unittest.main()
