import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import router


class ComputerSessionTests(unittest.IsolatedAsyncioTestCase):
    """Public remote input -> real busctl -> a test-owned private endpoint."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.address = f"unix:abstract=shield-theater-test-{uuid.uuid4().hex}"
        config = self.base / "bus.conf"
        config.write_text(
            f'<busconfig><type>session</type><listen>{self.address}</listen>'
            '<auth>EXTERNAL</auth><policy context="default">'
            '<allow own="*"/><allow send_destination="*"/>'
            '<allow receive_sender="*"/></policy></busconfig>')
        self.bus = await asyncio.create_subprocess_exec(
            "dbus-daemon", "--nofork", "--nopidfile", f"--config-file={config}",
            "--print-address=1", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.stop, self.bus)
        response = (await self.bus.stdout.readline()).decode().strip()
        self.assertEqual(response.split(",guid=")[0], self.address)
        self.address = response
        self.environment = patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": self.address})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.commands = []
        execute = router.run

        async def isolated_command(command, timeout=15):
            self.commands.append(command)
            if command[0] != "busctl" or "--user" not in command:
                raise AssertionError(f"Unexpected desktop/player fallback: {command}")
            return await execute(command, timeout)

        self.command_patch = patch.object(router, "run", side_effect=isolated_command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.backend = router.Router(router.Paths(self.base / "config", self.base / "runtime"),
                                     router.Config())
        self.addAsyncCleanup(self.cleanup_launch)

    async def cleanup_launch(self):
        process = self.backend.computer.launch_process
        if process:
            await self.stop(process)
            await self.backend.computer.launch_output

    async def stop(self, process):
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)

    async def endpoint(self, active=True, error="", legacy=False):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/python", str(router.PLUGIN / "theater_bus_fixture.py"),
            json.dumps(active), error, "legacy" if legacy else "current",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.stop, process)
        self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"ready\n")
        return process

    async def press(self, code, timestamp=1):
        try:
            await self.backend.key(code, 1, timestamp)
        finally:
            await self.backend.key(code, 0, timestamp + 0.1)

    async def report(self, endpoint):
        return json.loads(await asyncio.wait_for(endpoint.stdout.readline(), 5))

    @asynccontextmanager
    async def captured_input(self):
        self.backend.config = router.Config(remote_address="00:11:22:33:44:55")
        queue = asyncio.Queue()
        held = set()

        async def events():
            while True:
                code, value = await queue.get()
                if value == 1:
                    held.add(code)
                elif value == 0:
                    held.discard(code)
                yield Mock(type=1, code=code, value=value,
                           timestamp=Mock(return_value=router.time.time()))

        device = Mock(
            uniq=self.backend.config.remote_address,
            info=Mock(vendor=0x0955, product=0x7217),
            async_read_loop=events, active_keys=Mock(side_effect=lambda: list(held)),
        )
        device.name = router.REMOTE_NAME
        original = router.run

        async def input_commands(command, timeout=15):
            if command[:2] == ["udevadm", "settle"]:
                return ""
            if command[:2] == ["udevadm", "info"]:
                return "LIBINPUT_IGNORE_DEVICE=1\nCURRENT_TAGS=:uaccess:seat:\n"
            return await original(command, timeout)

        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router, "find_remote", return_value=Path("/dev/input/event99")),
            patch.object(router, "InputDevice", return_value=device),
            patch.object(router, "run", side_effect=input_commands),
        ):
            capture = asyncio.create_task(self.backend.input_loop())
            try:
                async with asyncio.timeout(2):
                    while not self.backend.input_captured:
                        if capture.done():
                            capture.result()
                        await asyncio.sleep(0.01)
                yield queue, held, device
            finally:
                capture.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await capture

    async def test_active_theater_direction_uses_its_public_session(self):
        endpoint = await self.endpoint()
        await self.press(103)
        self.assertEqual(await self.report(endpoint), {"method": "Dispatch", "arguments": ["up"]})

    async def test_horizontal_hold_forwards_real_press_repeat_release_without_dispatch(self):
        endpoint = await self.endpoint()
        for code, action in ((105, "left"), (106, "right")):
            for value, phase in ((1, "press"), (2, "repeat"), (2, "repeat"), (0, "release")):
                await self.backend.key(code, value, value + 1)
                self.assertEqual(await self.report(endpoint),
                                 {"method": "Direction", "arguments": [action, phase]})
        self.assertFalse(any("Dispatch" in command for command in self.commands))

    async def test_physically_held_direction_stays_live_without_hardware_repeat_events(self):
        endpoint = await self.endpoint()
        async with self.captured_input() as (events, held, device):
            await events.put((106, 1))
            self.assertEqual(await self.report(endpoint),
                             {"method": "Direction", "arguments": ["right", "press"]})
            started = router.time.monotonic()
            repeats = 0
            while router.time.monotonic() - started < 10:
                response = await asyncio.wait_for(self.report(endpoint), 1.5)
                self.assertEqual(response,
                                 {"method": "Direction", "arguments": ["right", "repeat"]})
                repeats += 1
                self.assertIn(106, held)
            self.assertGreaterEqual(repeats, 9)
            self.assertGreater(device.active_keys.call_count, 3)
            await events.put((106, 0))
            async with asyncio.timeout(2):
                while True:
                    response = await self.report(endpoint)
                    if response["arguments"][-1] == "release":
                        break
                    self.assertEqual(response["arguments"], ["right", "repeat"])
            self.assertEqual(response,
                             {"method": "Direction", "arguments": ["right", "release"]})
            self.assertEqual(self.backend.computer.theater_directions, {})

    async def test_missing_held_key_stops_liveness_without_inventing_release(self):
        endpoint = await self.endpoint()
        async with self.captured_input() as (events, held, _):
            await events.put((105, 1))
            self.assertEqual((await self.report(endpoint))["arguments"], ["left", "press"])
            self.assertEqual((await self.report(endpoint))["arguments"], ["left", "repeat"])
            held.clear()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.report(endpoint), 1.5)
            await events.put((105, 0))
            self.assertEqual(await self.report(endpoint),
                             {"method": "Direction", "arguments": ["left", "release"]})

    async def test_unreadable_held_state_cancels_capture_without_committing_seek(self):
        endpoint = await self.endpoint()
        async with self.captured_input() as (events, _, device):
            await events.put((106, 1))
            self.assertEqual((await self.report(endpoint))["arguments"], ["right", "press"])
            device.active_keys.side_effect = OSError("fixture held-key state unavailable")
            self.assertEqual(await self.report(endpoint),
                             {"method": "Direction", "arguments": ["right", "cancel"]})
            async with asyncio.timeout(1):
                while not device.close.called:
                    await asyncio.sleep(0.01)
            self.assertIn("held-key state unavailable", self.backend.input_error)
            self.assertFalse(self.backend.input_captured)
            self.assertEqual(self.backend.computer.theater_directions, {})

    async def test_horizontal_release_after_focus_loss_cancels_instead_of_seeking(self):
        endpoint = await self.endpoint()
        await self.backend.key(105, 1, 1)
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["left", "press"]})
        original = self.backend.computer.theater_boolean

        async def inactive(arguments):
            if arguments[0] == "get-property":
                return False
            return await original(arguments)

        with patch.object(self.backend.computer, "theater_boolean", side_effect=inactive):
            await self.backend.key(105, 0, 2)
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["left", "cancel"]})

    async def test_switching_target_cancels_theater_hold_and_late_release_cannot_seek(self):
        endpoint = await self.endpoint()
        await self.backend.key(106, 1, 1)
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["right", "press"]})
        self.backend.config = router.dataclasses.replace(
            self.backend.config, shield_host="shield.test", paired_host="shield.test")
        self.backend.notice = AsyncMock()
        await self.backend.select("shield")
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["right", "cancel"]})
        before = len(self.commands)
        await self.backend.key(106, 0, 2)
        self.assertEqual(len(self.commands), before)

    async def test_input_disconnect_cancels_horizontal_hold_without_commit(self):
        endpoint = await self.endpoint()
        await self.backend.key(105, 1, 1)
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["left", "press"]})
        await self.backend.computer.cancel_directions()
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["left", "cancel"]})
        before = len(self.commands)
        await self.backend.key(105, 0, 2)
        self.assertEqual(len(self.commands), before)

    async def test_capture_disconnect_sends_cancel_through_the_public_endpoint(self):
        endpoint = await self.endpoint()
        self.backend.config = router.Config(remote_address="00:11:22:33:44:55")

        async def events():
            yield Mock(type=1, code=106, value=1, timestamp=Mock(return_value=router.time.time()))
            raise OSError("fixture remote disconnected")

        device = Mock(
            uniq=self.backend.config.remote_address,
            info=Mock(vendor=0x0955, product=0x7217),
            async_read_loop=events, active_keys=Mock(return_value=[]),
        )
        device.name = router.REMOTE_NAME
        original = router.run

        async def input_commands(command, timeout=15):
            if command[:2] == ["udevadm", "settle"]:
                return ""
            if command[:2] == ["udevadm", "info"]:
                return "LIBINPUT_IGNORE_DEVICE=1\nCURRENT_TAGS=:uaccess:seat:\n"
            return await original(command, timeout)

        with (
            patch.object(router, "setup_ready", return_value=True),
            patch.object(router, "find_remote", return_value=Path("/dev/input/event99")),
            patch.object(router, "InputDevice", return_value=device),
            patch.object(router, "run", side_effect=input_commands),
        ):
            capture = asyncio.create_task(self.backend.input_loop())
            try:
                for phase in ("press", "cancel"):
                    self.assertEqual(await self.report(endpoint),
                                     {"method": "Direction", "arguments": ["right", phase]})
                async with asyncio.timeout(1):
                    while not device.close.called:
                        await asyncio.sleep(0.01)
                self.assertFalse(self.backend.input_connected)
                self.assertEqual(self.backend.computer.theater_directions, {})
                device.close.assert_called_once()
            finally:
                capture.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await capture

    async def test_horizontal_hold_cannot_cross_theater_owner_restart(self):
        endpoint = await self.endpoint()
        await self.backend.key(106, 1, 1)
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["right", "press"]})
        await self.stop(endpoint)
        replacement = await self.endpoint()
        before = len(self.commands)
        await self.backend.key(106, 2, 2)
        await self.backend.key(106, 0, 3)
        self.assertFalse(any("Direction" in command for command in self.commands[before:]))
        await self.backend.key(106, 1, 4)
        self.assertEqual(await self.report(replacement),
                         {"method": "Direction", "arguments": ["right", "press"]})
        await self.backend.key(106, 0, 5)
        self.assertEqual(await self.report(replacement),
                         {"method": "Direction", "arguments": ["right", "release"]})

    async def test_failed_horizontal_repeat_cancels_and_late_release_never_commits(self):
        endpoint = await self.endpoint()
        await self.backend.key(105, 1, 1)
        await self.report(endpoint)
        original = self.backend.computer.theater_call

        async def reject_repeat(method, *arguments, **keywords):
            if method == "Direction" and arguments[-1] == "repeat":
                raise RuntimeError("fixture rejected repeat")
            await original(method, *arguments, **keywords)

        with patch.object(self.backend.computer, "theater_call", side_effect=reject_repeat):
            with self.assertRaisesRegex(RuntimeError, "fixture rejected repeat"):
                await self.backend.key(105, 2, 2)
        self.assertEqual(await self.report(endpoint),
                         {"method": "Direction", "arguments": ["left", "cancel"]})
        before = len(self.commands)
        await self.backend.key(105, 0, 3)
        self.assertEqual(len(self.commands), before)

    async def test_orphan_horizontal_release_and_repeat_do_not_start_a_hold(self):
        await self.endpoint()
        await self.backend.key(106, 0, 1)
        await self.backend.key(106, 2, 2)
        self.assertEqual(self.commands, [])

    async def test_running_legacy_app_keeps_taps_until_reopen_negotiates_hold(self):
        legacy = await self.endpoint(legacy=True)
        for value in (1, 2):
            await self.backend.key(106, value, value)
            self.assertEqual(await self.report(legacy),
                             {"method": "Dispatch", "arguments": ["right"]})
        await self.backend.key(106, 0, 3)
        await self.stop(legacy)
        current = await self.endpoint()
        await self.press(106, 4)
        for phase in ("press", "release"):
            self.assertEqual(await self.report(current),
                             {"method": "Direction", "arguments": ["right", phase]})

    async def test_held_input_cannot_cross_owner_change_to_a_legacy_app(self):
        current = await self.endpoint()
        await self.backend.key(105, 1, 1)
        await self.report(current)
        await self.stop(current)
        legacy = await self.endpoint(legacy=True)
        before = len(self.commands)
        await self.backend.key(105, 2, 2)
        await self.backend.key(105, 0, 3)
        self.assertFalse(any("Direction" in command or "Dispatch" in command
                             for command in self.commands[before:]))
        await self.press(105, 4)
        self.assertEqual(await self.report(legacy),
                         {"method": "Dispatch", "arguments": ["left"]})

    async def test_home_raises_existing_inactive_session_without_launch_or_window_guess(self):
        endpoint = await self.endpoint(active=False)
        for timestamp in (1, 2):
            await self.press(172, timestamp)
            self.assertEqual(await self.report(endpoint), {"method": "Raise", "arguments": []})

    @asynccontextmanager
    async def tv_desktop(self, endpoint):
        self.backend.computer.tv_output = "HDMI-A-1"
        desktop = {
            "monitors": [
                {"id": 1, "name": "DP-2", "activeWorkspace": {"id": 1}},
                {"id": 0, "name": "HDMI-A-1", "activeWorkspace": {"id": 9},
                 "x": 0, "y": 0, "width": 3840, "height": 2160, "scale": 2},
            ],
            "clients": [
                {"pid": endpoint.pid + 1000, "class": "home-theater",
                 "address": "0x456def", "mapped": True},
                {"pid": endpoint.pid, "class": "home-theater", "address": "0x123abc",
                 "mapped": True, "hidden": False, "floating": True,
                 "monitor": 1, "workspace": {"id": 2}, "at": [5589, 26], "size": [960, 1041],
                 "fullscreen": 0},
            ],
            "activewindow": {"class": "kitty", "address": "0x999"},
            "effects": [],
            "reject": False,
            "ignore_focus": False,
            "ignore_center": False,
            "ignore_fullscreen": False,
        }
        execute = router.run

        async def compositor(command, timeout=15):
            if command[0] != "hyprctl":
                return await execute(command, timeout)
            if command[1] == "eval":
                desktop["effects"].append(command[2])
                if desktop["reject"]:
                    return "rejected"
                if "hl.dsp.window.move" in command[2]:
                    desktop["clients"][1].update(monitor=0, workspace={"id": 9})
                elif "hl.dsp.window.center" in command[2]:
                    if not desktop["ignore_center"]:
                        desktop["clients"][1]["at"] = [480, 37]
                elif "hl.dsp.window.fullscreen_state" in command[2]:
                    if not desktop["ignore_fullscreen"]:
                        desktop["clients"][1].update(fullscreen=2, at=[0, 0], size=[1920, 1080])
                elif not desktop["ignore_focus"]:
                    desktop["activewindow"] = desktop["clients"][1].copy()
                return "ok"
            self.assertEqual(command[2], "-j")
            return json.dumps(desktop[command[1]])

        with (
            patch.object(router, "run", side_effect=compositor),
            patch.object(self.backend, "notice", new_callable=AsyncMock),
        ):
            yield desktop

    async def test_panel_home_moves_owned_window_to_tv_current_workspace_and_focuses_it(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            state = await self.backend.request({"command": "open_theater"})
        self.assertEqual(state["target"], "computer")
        self.assertEqual(desktop["effects"], [
            'hl.dispatch(hl.dsp.window.move({window="address:0x123abc",workspace="9",follow=false}))',
            'hl.dispatch(hl.dsp.window.center({window="address:0x123abc"}))',
            'hl.dispatch(hl.dsp.window.fullscreen_state({window="address:0x123abc",'
            'action="set",internal=2,client=2}))',
            'hl.dispatch(hl.dsp.focus({window="address:0x123abc"}))',
        ])
        self.assertEqual(desktop["activewindow"]["monitor"], 0)
        self.assertEqual(desktop["activewindow"]["workspace"]["id"], 9)
        self.assertEqual(desktop["activewindow"]["at"], [0, 0])
        self.assertEqual(desktop["activewindow"]["fullscreen"], 2)

    async def test_remote_home_uses_the_same_tv_workspace_activation(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            await self.press(172)
            await self.press(172, 2)
        self.assertEqual(len(desktop["effects"]), 8)
        self.assertEqual(desktop["activewindow"]["address"], "0x123abc")
        self.assertEqual(desktop["activewindow"]["fullscreen"], 2)

    async def test_tv_home_refuses_disconnected_tv_or_ambiguous_owned_windows(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            monitors = desktop["monitors"].copy()
            desktop["monitors"].pop()
            with self.assertRaisesRegex(RuntimeError, "TV output.*not connected"):
                await self.backend.computer.home()
            desktop["monitors"] = monitors
            desktop["clients"].append(desktop["clients"][1].copy())
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                await self.backend.computer.home()
            self.assertEqual(desktop["effects"], [])

    async def test_tv_home_refuses_invalid_window_address(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            desktop["clients"][1]["address"] = '0x123"; other_action()'
            with self.assertRaisesRegex(RuntimeError, "window address"):
                await self.backend.computer.home()
            self.assertEqual(desktop["effects"], [])

    async def test_tv_home_reports_rejected_move_and_success_without_actual_focus(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            desktop["reject"] = True
            with self.assertRaisesRegex(RuntimeError, "Hyprland rejected"):
                await self.backend.computer.home()
            desktop["reject"] = False
            desktop["ignore_focus"] = True
            with self.assertRaisesRegex(RuntimeError, "did not make.*visible.*TV workspace"):
                await self.backend.computer.home()

    async def test_tv_home_rejects_focused_window_that_remains_offscreen(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            desktop["ignore_center"] = True
            desktop["ignore_fullscreen"] = True
            with self.assertRaisesRegex(RuntimeError, "did not make.*visible.*TV workspace"):
                await self.backend.computer.home()

    async def test_tv_home_requires_fullscreen_instead_of_reporting_windowed_success(self):
        endpoint = await self.endpoint(active=False)
        async with self.tv_desktop(endpoint) as desktop:
            desktop["ignore_fullscreen"] = True
            with self.assertRaisesRegex(RuntimeError, "did not make.*fullscreen"):
                await self.backend.computer.home()

    async def test_active_theater_navigation_and_media_use_exact_session_actions(self):
        endpoint = await self.endpoint()
        for code, action in (
            (103, "up"), (105, "left"), (106, "right"), (108, "down"),
            (353, "confirm"), (158, "back"),
            (164, "play-pause"), (168, "seek-backward"), (208, "seek-forward"),
        ):
            await self.press(code)
            if code in router.HORIZONTAL_DIRECTIONS:
                for phase in ("press", "release"):
                    self.assertEqual(await self.report(endpoint),
                                     {"method": "Direction", "arguments": [action, phase]})
            else:
                self.assertEqual(await self.report(endpoint),
                                 {"method": "Dispatch", "arguments": [action]})

    async def test_home_launches_once_waits_for_ownership_and_reuses_same_session(self):
        launches = self.base / "launches"
        executable = self.base / "home-theater"
        executable.write_text(
            "#!/usr/bin/python\nimport os, time\n"
            f"with open({str(launches)!r}, 'a') as file: file.write('launch\\n')\n"
            "time.sleep(0.15)\n"
            f"os.execv('/usr/bin/python', ['/usr/bin/python', "
            f"{str(router.PLUGIN / 'theater_bus_fixture.py')!r}, 'false'])\n")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{self.base}:{os.environ['PATH']}"}):
            await asyncio.gather(self.press(172, 1), self.backend.computer.home())
            await self.press(172, 2)
        self.assertEqual(launches.read_text(), "launch\n")

    async def test_home_new_session_receives_only_the_executable_and_fullscreen_argument(self):
        arguments = self.base / "arguments.json"
        executable = self.base / "home-theater"
        executable.write_text(
            "#!/usr/bin/python\nimport json, os, sys\n"
            f"with open({str(arguments)!r}, 'w') as file: json.dump(sys.argv, file)\n"
            f"os.execv('/usr/bin/python', ['/usr/bin/python', "
            f"{str(router.PLUGIN / 'theater_bus_fixture.py')!r}, 'false'])\n")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{self.base}:{os.environ['PATH']}"}):
            await self.press(172)
        self.assertEqual(json.loads(arguments.read_text()), [str(executable), "--fullscreen"])

    async def test_early_launch_exit_reports_actionable_application_stderr(self):
        executable = self.base / "home-theater"
        executable.write_text("#!/usr/bin/python\nimport sys\n"
                              "print('configure the TV output first', file=sys.stderr)\nsys.exit(13)\n")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{self.base}:{os.environ['PATH']}"}):
            with self.assertRaisesRegex(RuntimeError, r"exited \(13\).*configure the TV output first"):
                await self.press(172)

    async def test_timed_out_live_launch_stays_tracked_and_home_never_duplicates_it(self):
        launches = self.base / "launches"
        executable = self.base / "home-theater"
        executable.write_text(
            "#!/usr/bin/python\nimport time\n"
            f"with open({str(launches)!r}, 'a') as file: file.write('launch\\n')\n"
            "time.sleep(60)\n")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{self.base}:{os.environ['PATH']}"}):
            try:
                for timestamp in (1, 2):
                    with self.assertRaisesRegex(RuntimeError, "within 12 seconds.*not start a duplicate"):
                        await self.press(172, timestamp)
            finally:
                process = self.backend.computer.launch_process
                if process:
                    await self.stop(process)
                    await self.backend.computer.launch_output
        self.assertEqual(launches.read_text(), "launch\n")

    async def test_missing_binary_and_unavailable_bus_never_launch_stremio(self):
        with patch.object(router.shutil, "which", return_value=None), patch.object(Path, "home", return_value=self.base):
            with self.assertRaisesRegex(RuntimeError, "not installed.*router service's PATH"):
                await self.press(172)
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=shield-theater-test-missing"}):
            with self.assertRaises(RuntimeError):
                await self.press(103)
        self.assertTrue(all(command[0] == "busctl" for command in self.commands))

    async def test_string_or_untyped_active_is_rejected_without_any_fallback(self):
        await self.endpoint()
        execute = router.run
        for response in ('{"type":"b","data":"false"}', '{"type":"s","data":"true"}',
                         '{"type":"b","data":[true,false]}', '[]'):
            async def invalid_property(command, timeout=15):
                if "get-property" in command:
                    return response
                return await execute(command, timeout)

            with patch.object(router, "run", side_effect=invalid_property):
                with self.assertRaisesRegex(RuntimeError, "invalid DBus boolean"):
                    await self.press(103)

    async def test_inactive_theater_keeps_prior_focused_computer_navigation(self):
        await self.endpoint(active=False)
        execute = router.run
        injected = []

        async def focused_computer(command, timeout=15):
            if command == ["hyprctl", "activewindow", "-j"]:
                return '{"class":"kitty","address":"0x456"}'
            if command[:2] == ["hyprctl", "eval"]:
                injected.append(command[-1])
                return "ok"
            return await execute(command, timeout)

        with patch.object(router, "run", side_effect=focused_computer):
            await self.press(103)
        self.assertEqual(injected, ['hl.dispatch(hl.dsp.send_shortcut({mods="",key="Up"}))'])

    async def test_microphone_toggles_dictation_regardless_of_theater_ownership_or_focus(self):
        for active in (None, False, True):
            with self.subTest(active=active):
                endpoint = await self.endpoint(active=active) if active is not None else None
                with patch.object(
                    router, "run", new_callable=AsyncMock,
                    side_effect=['{"alt":"idle"}', '[{"name":"atvvoice-shield-remote"}]',
                                 "headset", "", "", '{"alt":"recording"}',
                                 '{"alt":"recording"}', "", '{"alt":"idle"}', "atvvoice-shield-remote", ""],
                ) as command:
                    await self.press(217)
                    await self.press(217)
                    self.assertTrue(all(
                        call.args[0][0] in ("voxtype", "pactl") for call in command.await_args_list
                    ))
                    self.assertEqual(
                        [call.args[0] for call in command.await_args_list
                         if call.args[0][:2] == ["voxtype", "record"]],
                        [["voxtype", "record", "start"], ["voxtype", "record", "stop"]],
                    )
                if endpoint:
                    await self.stop(endpoint)
        self.assertEqual(self.commands, [])

    async def test_orphan_direction_repeat_after_release_cannot_activate_theater(self):
        endpoint = await self.endpoint()
        await self.press(103)
        await self.report(endpoint)
        before = len(self.commands)
        await self.backend.key(103, 2, 2)
        self.assertFalse(any("Dispatch" in command for command in self.commands[before:]))

    async def test_active_dispatch_rejection_never_reaches_keys_mpris_or_other_players(self):
        for error, code in (("Inactive", 103), ("Unavailable", 164), ("InvalidAction", 353)):
            endpoint = await self.endpoint(error=error)
            with self.assertRaisesRegex(RuntimeError, "fixture rejected action"):
                await self.press(code)
            self.assertEqual((await self.report(endpoint))["method"], "Dispatch")
            await self.stop(endpoint)

    async def test_home_raise_failure_never_launches_or_guesses_a_window(self):
        endpoint = await self.endpoint(active=False, error="Unavailable")
        with self.assertRaisesRegex(RuntimeError, "fixture rejected action"):
            await self.press(172)
        self.assertEqual(await self.report(endpoint), {"method": "Raise", "arguments": []})

    async def test_repeated_or_duplicate_select_media_and_home_never_activate_twice(self):
        endpoint = await self.endpoint()
        for code in (353, 164, 172):
            before = len(self.commands)
            await self.backend.key(code, 1, 1)
            await self.backend.key(code, 1, 1.1)
            await self.backend.key(code, 2, 2)
            await self.backend.key(code, 0, 3)
            calls = [command for command in self.commands[before:]
                     if command[-1] in {"confirm", "play-pause", "Raise"}]
            self.assertEqual(len(calls), 1)
            await self.report(endpoint)

    async def test_held_theater_direction_does_not_inject_into_new_foreground_after_focus_loss(self):
        endpoint = await self.endpoint()
        await self.backend.key(103, 1, 1)
        await self.report(endpoint)
        await self.backend.key(103, 2, 2)
        self.assertEqual(await self.report(endpoint), {"method": "Dispatch", "arguments": ["up"]})
        await self.stop(endpoint)
        inactive = await self.endpoint(active=False)
        await self.backend.key(103, 2, 3)
        await self.stop(inactive)
        active = await self.endpoint()
        await self.backend.key(103, 2, 3.5)
        await self.backend.key(103, 0, 4)
        await self.press(103, 5)
        self.assertEqual(await self.report(active), {"method": "Dispatch", "arguments": ["up"]})
        self.assertTrue(all(command[0] == "busctl" for command in self.commands))


if __name__ == "__main__":
    unittest.main()
