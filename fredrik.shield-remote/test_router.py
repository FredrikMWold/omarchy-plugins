import asyncio
import dataclasses
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

    async def test_computer_navigation_and_home(self):
        for code in (*router.NAVIGATION, 172):
            await self.press(code)
            self.backend.computer.key.assert_awaited_with(code)
        self.backend.client.send_key_command.assert_not_called()

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
                [(name, "START_LONG"), (name, "END_LONG")],
            )

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
        with patch.object(router, "rule_installed", return_value=True):
            self.assertFalse(self.backend.state()["setup_complete"])
            self.paths.service.parent.mkdir(parents=True)
            self.paths.service.write_text(router.service_text())
            self.assertFalse(self.backend.state()["setup_complete"])
            enabled = self.paths.service.parent / "graphical-session.target.wants" / router.SERVICE
            enabled.parent.mkdir()
            enabled.symlink_to(self.paths.service)
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

    async def test_rule_reload_failure_invalidates_previous_setup_receipt(self):
        receipt = self.paths.config / "input-setup.json"
        router.write_private_json(receipt, {"schema": 1, "address": self.config.remote_address})
        with (
            patch.object(router, "RULE", self.paths.config / "test.rules"),
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

    async def test_home_focuses_existing_stremio_without_launching_duplicates(self):
        computer = router.Computer()
        with patch.object(router, "run", new_callable=AsyncMock, side_effect=[
            json.dumps([{"class": "Stremio", "address": "0x123"}]), "ok",
        ]) as command:
            await computer.home()
        self.assertEqual(command.await_count, 2)
        self.assertIn('hl.dsp.focus({window="address:0x123"})', command.await_args.args[0][-1])

    async def test_home_launches_native_stremio_once_then_focuses(self):
        computer = router.Computer()
        with (
            patch.object(router.shutil, "which", return_value="/usr/bin/stremio"),
            patch.object(router, "run", new_callable=AsyncMock, side_effect=[
                "[]", "ok", json.dumps([{"class": "stremio", "address": "0x123"}]), "ok",
            ]) as command,
        ):
            await computer.home()
        self.assertIn('hl.dsp.exec_cmd("/usr/bin/stremio")', command.await_args_list[1].args[0][-1])

    async def test_home_launches_installed_flatpak_stremio(self):
        computer = router.Computer()
        with (
            patch.object(router.shutil, "which", side_effect=[None, "/usr/bin/flatpak"]),
            patch.object(router, "run", new_callable=AsyncMock, side_effect=[
                "[]", "com.stremio.Stremio", "ok",
                json.dumps([{"class": "com.stremio.Stremio", "address": "0x123"}]), "ok",
            ]) as command,
        ):
            await computer.home()
        self.assertIn("flatpak run com.stremio.Stremio", command.await_args_list[2].args[0][-1])

    async def test_missing_stremio_reports_error_without_launching(self):
        computer = router.Computer()
        with (
            patch.object(router.shutil, "which", return_value=None),
            patch.object(router, "run", new_callable=AsyncMock, return_value="[]") as command,
        ):
            with self.assertRaisesRegex(RuntimeError, "Stremio is not installed"):
                await computer.home()
        command.assert_awaited_once()

    async def test_multiple_stremio_windows_are_reported(self):
        with patch.object(router, "run", new_callable=AsyncMock, return_value=json.dumps([
            {"class": "Stremio", "address": "0x123"},
            {"class": "stremio", "address": "0x456"},
        ])):
            with self.assertRaisesRegex(RuntimeError, "Multiple Stremio"):
                await router.Computer().home()

    async def test_legacy_config_migration_preserves_remote_and_tv_credentials(self):
        file = self.paths.config / "config.json"
        key = self.paths.config / "client.key"
        key.write_text("test certificate key")
        for old_app in ("demo", "stremio"):
            file.write_text(json.dumps({"schema": 1, **dataclasses.asdict(self.config),
                                        "home_app": old_app}))
            self.assertEqual(router.load_config(self.paths), self.config)
            migrated = json.loads(file.read_text())
            self.assertEqual(migrated, {"schema": 2, **dataclasses.asdict(self.config)})
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(key.read_text(), "test certificate key")

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
