from __future__ import annotations

import argparse
import asyncio
import dataclasses
from contextlib import asynccontextmanager
import fcntl
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth, VoiceStream
from evdev import InputDevice

from tv_voice import VoiceRemote, stream_microphone


LOG = logging.getLogger("shield-remote")
PLUGIN = Path(__file__).resolve().parent
SERVICE = "omarchy-shield-remote.service"
MICROPHONE_SERVICE = "atvvoice-shield-remote.service"
RULE = Path("/etc/udev/rules.d/71-fredrik-shield-remote.rules")
REMOTE_NAME = "NVIDIA SHIELD Remote"
REMOTE_MIC = "atvvoice-shield-remote"
TV_VOICE_TIMEOUT = 30
ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
NAVIGATION = {103: "Up", 105: "Left", 106: "Right", 108: "Down", 353: "Return", 158: "Escape"}
FOCUS_DIRECTIONS = {103: "up", 105: "left", 106: "right", 108: "down"}
HORIZONTAL_DIRECTIONS = {105, 106}
STREMIO_CLASSES = {"stremio", "com.stremio.stremio"}
THEATER_SERVICE = "org.home_theater.Remote"
THEATER_OBJECT = "/org/home_theater/Remote"
THEATER_ACTIONS = {
    103: "up", 105: "left", 106: "right", 108: "down",
    353: "confirm", 158: "back",
    164: "play-pause", 168: "seek-backward", 208: "seek-forward",
}
SHIELD_KEYS = {
    103: "DPAD_UP", 105: "DPAD_LEFT", 106: "DPAD_RIGHT", 108: "DPAD_DOWN",
    353: "DPAD_CENTER", 158: "BACK", 172: "HOME", 164: "MEDIA_PLAY_PAUSE",
    168: "MEDIA_REWIND", 208: "MEDIA_FAST_FORWARD", 114: "VOLUME_DOWN", 115: "VOLUME_UP",
    116: "POWER",
}
IGNORED = {217, 393}
REPEAT = {103, 105, 106, 108, 114, 115}
VOLUME_KEYS = {114: "-1", 115: "+1"}
NETWORK_ERRORS = (CannotConnect, ConnectionClosed, InvalidAuth, OSError, TimeoutError)


@dataclasses.dataclass(frozen=True)
class Config:
    remote_address: str = ""
    shield_host: str = ""
    paired_host: str = ""
    tv_output: str = ""
    soundbar_host: str = ""

    def validate(self) -> Config:
        if self.remote_address and not ADDRESS.fullmatch(self.remote_address):
            raise ValueError("Remote address must be a Bluetooth address")
        for host in (self.shield_host, self.paired_host):
            validate_host(host)
        validate_host(self.soundbar_host, "Soundbar")
        if self.tv_output and not re.fullmatch(r"[A-Za-z0-9_.:-]+", self.tv_output):
            raise ValueError("TV output must be a display name such as HDMI-A-1")
        return dataclasses.replace(self, remote_address=self.remote_address.upper())


def validate_host(host: str, device: str = "Shield") -> None:
    if not isinstance(host, str):
        raise ValueError(f"{device} host must be text")
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError(f"Enter a {device} IP address or hostname, without a URL or port") from None


@dataclasses.dataclass(frozen=True)
class Paths:
    config: Path
    runtime: Path

    @classmethod
    def local(cls) -> Paths:
        home = Path.home()
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            raise RuntimeError("Run Shield Remote in your graphical user session")
        return cls(
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "fredrik.shield-remote",
            Path(runtime) / "fredrik.shield-remote",
        )

    def prepare(self) -> None:
        for directory in (self.config, self.runtime):
            if directory.is_symlink():
                raise RuntimeError(f"Private machine-local directory must not be a symlink: {directory}")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    @property
    def socket(self) -> Path:
        return self.runtime / "router.sock"

    @property
    def service(self) -> Path:
        return self.config.parent / "systemd/user" / SERVICE

    @property
    def data(self) -> Path:
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "fredrik.shield-remote"

    @property
    def microphone_service(self) -> Path:
        return self.service.parent / MICROPHONE_SERVICE

    @property
    def microphone_binary(self) -> Path:
        return self.data / "bin/atvvoice"


def load_config(paths: Paths) -> Config:
    file = paths.config / "config.json"
    if not file.exists():
        return Config()
    payload = json.loads(file.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Unsupported Shield Remote configuration")
    schema = payload.pop("schema", None)
    if type(schema) is not int or schema not in (1, 2, 3, 4):
        raise ValueError("Unsupported Shield Remote configuration")
    if schema == 1 and payload.pop("home_app", None) not in ("demo", "stremio"):
        raise ValueError("Invalid legacy Computer Home setting")
    if schema in (1, 2):
        payload.setdefault("tv_output", "")
    if schema < 4:
        payload.setdefault("soundbar_host", "")
    if set(payload) != {field.name for field in dataclasses.fields(Config)}:
        raise ValueError("Invalid Shield Remote configuration fields")
    if not all(isinstance(value, str) for value in payload.values()):
        raise ValueError("Shield Remote configuration values must be text")
    config = Config(**payload).validate()
    if schema < 4:
        LOG.info("Migrating machine-local Shield Remote configuration")
        save_config(paths, config)
    return config


def save_config(paths: Paths, config: Config) -> None:
    paths.prepare()
    payload = {"schema": 4, **dataclasses.asdict(config.validate())}
    write_private_json(paths.config / "config.json", payload)


def write_private_json(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(payload, file, indent=2)
        file.write("\n")
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def saved_target(paths: Paths) -> str:
    file = paths.runtime / "router-target.json"
    if file.is_symlink():
        raise RuntimeError("Refusing symlinked router target state")
    if not file.exists():
        return "computer"
    payload = json.loads(file.read_text())
    if (not isinstance(payload, dict) or set(payload) != {"schema", "target"}
            or type(payload["schema"]) is not int or payload["schema"] != 1
            or payload["target"] not in ("computer", "shield")):
        raise ValueError("Invalid saved router target; refusing input startup")
    return payload["target"]


def save_target(paths: Paths, target: str) -> None:
    if target not in ("computer", "shield"):
        raise ValueError("Unknown control target")
    paths.prepare()
    write_private_json(paths.runtime / "router-target.json", {"schema": 1, "target": target})


async def run(command: list[str], timeout: float = 15) -> str:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise RuntimeError(f"{command[0]} timed out") from None
    if process.returncode:
        raise RuntimeError(stderr.decode().strip() or stdout.decode().strip()
                           or f"{command[0]} exited with {process.returncode}")
    return stdout.decode().strip()


async def bluez_device(address: str) -> str | None:
    output = await run(["busctl", "--system", "--list", "tree", "org.bluez"])
    pattern = re.compile(r"/org/bluez/hci\d+/dev_" + re.escape(address.replace(":", "_")))
    matches = [line.strip() for line in output.splitlines() if pattern.fullmatch(line.strip())]
    if len(matches) > 1:
        raise RuntimeError("Selected remote exists on multiple Bluetooth adapters")
    return matches[0] if matches else None


async def bluez_property(device: str, name: str) -> bool:
    response = json.loads(await run([
        "busctl", "--system", "--json=short", "get-property", "org.bluez",
        device, "org.bluez.Device1", name,
    ]))
    if not isinstance(response, dict) or type(response.get("data")) is not bool:
        raise RuntimeError(f"BlueZ returned an invalid {name} property")
    return response["data"]


async def bluez_call(device: str, method: str, timeout: float = 5) -> None:
    await run(["busctl", "--system", "call", "org.bluez", device,
               "org.bluez.Device1", method], timeout=timeout)


async def bluez_set(device: str, name: str, value: bool) -> None:
    await run(["busctl", "--system", "set-property", "org.bluez", device,
               "org.bluez.Device1", name, "b", "true" if value else "false"], timeout=5)


async def bluez_disconnect(device: str) -> None:
    # Disconnect also cancels an in-flight Connect while Connected is still false.
    try:
        await bluez_call(device, "Disconnect")
    except RuntimeError as error:
        if str(error) != "Call failed: Not Connected":
            raise
        LOG.info("Selected remote is already disconnected")


async def bluez_connect(device: str, timeout: float = 25) -> None:
    try:
        await bluez_call(device, "Connect", timeout=timeout)
    except RuntimeError as error:
        if str(error) != "Call failed: Already Connected" or not await bluez_property(device, "Connected"):
            raise
        LOG.info("Selected remote connected automatically")
    if not await bluez_property(device, "Connected"):
        raise RuntimeError("BlueZ did not confirm the remote connection")


async def bluez_cancel_pairing(device: str) -> None:
    try:
        await bluez_call(device, "CancelPairing", timeout=3)
    except RuntimeError as error:
        if str(error) != "Call failed: Does Not Exist":
            raise
        LOG.info("Selected remote has no pending pairing to cancel")


@asynccontextmanager
async def bluetooth_discovery(timeout: int = 30):
    scan = await asyncio.create_subprocess_exec(
        "bluetoothctl", "--timeout", str(timeout), "scan", "on",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(scan.communicate())
    try:
        try:
            async with asyncio.timeout(3):
                while "Discovering: yes" not in await run(["bluetoothctl", "show"]):
                    if scan.returncode is not None:
                        stdout, stderr = await output
                        raise RuntimeError(stderr.decode().strip() or stdout.decode().strip()
                                           or "Bluetooth discovery stopped unexpectedly")
                    await asyncio.sleep(0.1)
        except TimeoutError:
            raise RuntimeError("Bluetooth discovery did not start; turn Bluetooth on in Omarchy") from None
        yield
    finally:
        if scan.returncode is None:
            scan.terminate()
        await output


async def dispatch(expression: str) -> None:
    response = await run(["hyprctl", "eval", f"hl.dispatch({expression})"])
    if response != "ok":
        raise RuntimeError(f"Hyprland rejected the action: {response}")


async def send_key(key: str, window: dict | None = None) -> None:
    arguments = f'mods="",key={json.dumps(key)}'
    if window:
        arguments += f",window={json.dumps('address:' + window['address'])}"
    await dispatch(f"hl.dsp.send_shortcut({{{arguments}}})")


class Computer:
    def __init__(self, tv_output: str = "") -> None:
        self.tv_output = tv_output
        self.last_focus_completed = 0.0
        self.home_lock = asyncio.Lock()
        self.launch_process: asyncio.subprocess.Process | None = None
        self.launch_output: asyncio.Task | None = None
        self.direction_routes: dict[int, bool | None] = {}
        self.theater_directions: dict[int, str] = {}
        self.direction_updated: dict[int, float] = {}
        self.direction_owners: dict[int, str] = {}
        self.direction_protocol: tuple[str, bool] | None = None

    async def theater_boolean(self, arguments: list[str]) -> bool:
        response = json.loads(await run(
            ["busctl", "--user", "--auto-start=no", "--json=short", *arguments], timeout=3))
        if not isinstance(response, dict) or response.get("type") != "b":
            raise RuntimeError("Home Theater returned an invalid DBus boolean")
        value = response.get("data")
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if type(value) is not bool:
            raise RuntimeError("Home Theater returned an invalid DBus boolean")
        return value

    async def theater_owned(self) -> bool:
        return await self.theater_boolean([
            "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner", "s", THEATER_SERVICE,
        ])

    async def theater_owner(self) -> str:
        response = json.loads(await run([
            "busctl", "--user", "--auto-start=no", "--json=short", "call",
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetNameOwner", "s", THEATER_SERVICE,
        ], timeout=3))
        if not isinstance(response, dict) or response.get("type") != "s":
            raise RuntimeError("Home Theater returned an invalid DBus owner")
        owner = response.get("data")
        if isinstance(owner, list) and len(owner) == 1:
            owner = owner[0]
        if not isinstance(owner, str) or not owner.startswith(":"):
            raise RuntimeError("Home Theater returned an invalid DBus owner")
        return owner

    async def theater_call(self, method: str, *arguments: str, owner: str | None = None) -> None:
        await run([
            "busctl", "--user", "--auto-start=no", "call", owner or THEATER_SERVICE,
            THEATER_OBJECT, THEATER_SERVICE, method, *arguments,
        ], timeout=3)

    async def supports_direction(self, owner: str) -> bool:
        if self.direction_protocol is not None and self.direction_protocol[0] == owner:
            return self.direction_protocol[1]
        xml = await run([
            "busctl", "--user", "--auto-start=no", "--xml-interface", "introspect",
            owner, THEATER_OBJECT, THEATER_SERVICE,
        ], timeout=3)
        try:
            node = ET.fromstring(xml)
        except ET.ParseError as error:
            raise RuntimeError(f"Invalid Home Theater introspection: {error}") from error
        interface = node.find(f"./interface[@name='{THEATER_SERVICE}']")
        if interface is None:
            raise RuntimeError("Home Theater introspection omitted its remote interface")
        method = interface.find("./method[@name='Direction']")
        supported = method is not None
        if supported and [(argument.get("type"), argument.get("direction", "in"))
                          for argument in method.findall("arg")] != [("s", "in"), ("s", "in")]:
            raise RuntimeError("Home Theater returned an invalid Direction signature")
        if not supported:
            LOG.info("Running Home Theater uses the older remote API; retaining legacy "
                     "direction taps until the application is reopened")
        self.direction_protocol = (owner, supported)
        return supported

    async def release_direction(self, code: int, commit: bool = True) -> None:
        owner = self.theater_directions.pop(code, None)
        self.direction_updated.pop(code, None)
        self.direction_owners.pop(code, None)
        self.direction_routes.pop(code, None)
        if owner is None:
            return
        if not await self.theater_owned() or await self.theater_owner() != owner:
            LOG.info("Discarded held direction after Home Theater ownership changed")
            return
        active = await self.theater_boolean([
            "get-property", owner, THEATER_OBJECT, THEATER_SERVICE, "Active",
        ])
        phase = "release" if commit and active else "cancel"
        await self.theater_call("Direction", "ss", THEATER_ACTIONS[code], phase, owner=owner)

    async def cancel_directions(self) -> None:
        errors = []
        for code in tuple(self.direction_owners):
            try:
                await self.release_direction(code, commit=False)
            except (RuntimeError, OSError) as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("Cannot cancel held Home Theater directions: " + "; ".join(errors))

    async def home(self) -> None:
        async with self.home_lock:
            if not await self.theater_owned():
                if self.launch_process is None or self.launch_process.returncode is not None:
                    executable = shutil.which("home-theater")
                    if not executable:
                        local = Path.home() / ".local/bin/home-theater"
                        if local.is_file() and os.access(local, os.X_OK):
                            executable = str(local)
                    if not executable:
                        raise RuntimeError(
                            "Home Theater is not installed; install home-theater in "
                            "~/.local/bin or the router service's PATH")
                    self.launch_process = await asyncio.create_subprocess_exec(
                        executable, "--fullscreen", stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE)
                    self.launch_output = asyncio.create_task(self.launch_process.communicate())
                deadline = time.monotonic() + 12
                while not await self.theater_owned():
                    if self.launch_process.returncode is not None:
                        _, stderr = await self.launch_output
                        code = self.launch_process.returncode
                        detail = stderr.decode(errors="replace").strip() or "no stderr diagnostic"
                        raise RuntimeError(f"Home Theater exited ({code}) before DBus readiness: {detail}")
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Home Theater did not own org.home_theater.Remote within 12 seconds; "
                            "the launch is still tracked (Home will not start a duplicate). "
                            "Check application setup and the router service journal")
                    await asyncio.sleep(0.1)
            await self.theater_call("Raise")
            if self.tv_output:
                await self.bring_theater_to_tv()

    async def bring_theater_to_tv(self) -> None:
        owner = await self.theater_owner()
        response = json.loads(await run([
            "busctl", "--user", "--auto-start=no", "--json=short", "call",
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetConnectionUnixProcessID", "s", owner,
        ], timeout=3))
        if (not isinstance(response, dict) or response.get("type") != "u"
                or not isinstance(response.get("data"), list) or len(response["data"]) != 1
                or type(response["data"][0]) is not int or response["data"][0] <= 0):
            raise RuntimeError("Home Theater returned an invalid DBus process ID")
        pid = response["data"][0]
        monitors = json.loads(await run(["hyprctl", "monitors", "-j"]))
        if not isinstance(monitors, list) or not all(isinstance(item, dict) for item in monitors):
            raise RuntimeError("Invalid Hyprland monitor response")
        outputs = [item for item in monitors
                   if item.get("name") == self.tv_output and not item.get("disabled", False)]
        if len(outputs) != 1:
            raise RuntimeError(f"TV output {self.tv_output} is not connected or is ambiguous")
        monitor = outputs[0]
        workspace = monitor.get("activeWorkspace")
        if (type(monitor.get("id")) is not int or monitor["id"] < 0
                or not isinstance(workspace, dict)
                or type(workspace.get("id")) is not int or workspace["id"] <= 0):
            raise RuntimeError("TV output has no valid active workspace")
        scale = monitor.get("scale")
        if (not all(type(monitor.get(key)) is int for key in ("x", "y", "width", "height"))
                or monitor["width"] <= 0 or monitor["height"] <= 0
                or type(scale) not in (int, float) or not 0 < scale < float("inf")):
            raise RuntimeError("TV output has invalid geometry")
        width, height = monitor["width"] / scale, monitor["height"] / scale
        if monitor.get("transform", 0) in (1, 3, 5, 7):
            width, height = height, width
        deadline = time.monotonic() + 3
        while True:
            clients = json.loads(await run(["hyprctl", "clients", "-j"]))
            if not isinstance(clients, list) or not all(isinstance(item, dict) for item in clients):
                raise RuntimeError("Invalid Hyprland window response")
            windows = [item for item in clients
                       if item.get("pid") == pid and item.get("class") == "home-theater"
                       and item.get("mapped") is True]
            if len(windows) > 1:
                raise RuntimeError("Home Theater's owned window is ambiguous")
            if windows:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Home Theater's owned window did not map within 3 seconds")
            await asyncio.sleep(0.1)
        address = windows[0].get("address")
        if not isinstance(address, str) or not re.fullmatch(r"0x[0-9A-Fa-f]+", address):
            raise RuntimeError("Home Theater returned an invalid window address")
        selector = json.dumps("address:" + address)
        await dispatch(
            f'hl.dsp.window.move({{window={selector},workspace="{workspace["id"]}",follow=false}})')
        if windows[0].get("floating") is True:
            await dispatch(f"hl.dsp.window.center({{window={selector}}})")
        await dispatch(
            f'hl.dsp.window.fullscreen_state({{window={selector},action="set",internal=2,client=2}})')
        await dispatch(f"hl.dsp.focus({{window={selector}}})")
        deadline = time.monotonic() + 1
        while True:
            active = json.loads(await run(["hyprctl", "activewindow", "-j"]))
            if not isinstance(active, dict):
                raise RuntimeError("Invalid Hyprland active window response")
            active_workspace = active.get("workspace")
            position, size = active.get("at"), active.get("size")
            visible = (
                isinstance(position, list) and len(position) == 2
                and isinstance(size, list) and len(size) == 2
                and all(type(value) is int for value in (*position, *size))
                and all(value > 0 for value in size)
                and monitor["x"] <= position[0] + size[0] / 2 < monitor["x"] + width
                and monitor["y"] <= position[1] + size[1] / 2 < monitor["y"] + height
                and active.get("mapped") is True and active.get("hidden") is False
            )
            if (active.get("address") == address and active.get("monitor") == monitor["id"]
                    and isinstance(active_workspace, dict)
                    and active_workspace.get("id") == workspace["id"]
                    and active.get("fullscreen") == 2 and visible):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Hyprland did not make Home Theater fullscreen and visible on the TV workspace")
            await asyncio.sleep(0.05)

    async def focus(self, action: str, window: dict, repeat: bool = False) -> None:
        if repeat and time.monotonic() - self.last_focus_completed < 0.12:
            return
        response = json.loads(await run([
            "/usr/bin/python", str(PLUGIN / "navigation.py"), action,
        ], timeout=8))
        if not isinstance(response, dict) or type(response.get("activate")) is not bool:
            raise RuntimeError("Invalid Stremio focus navigation response")
        if response["activate"]:
            if action != "select":
                raise RuntimeError("Unexpected activation from Stremio focus navigation")
            await send_key("Return", window)
        elif action == "back":
            key = response.get("key")
            if not isinstance(key, str) or key not in {"Escape", "BackSpace"}:
                raise RuntimeError("Invalid Stremio Back navigation response")
            await send_key(key, window)
        self.last_focus_completed = time.monotonic()

    async def media(self, code: int) -> None:
        names = json.loads(await run([
            "busctl", "--user", "--json=short", "call", "org.freedesktop.DBus",
            "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
        ]))["data"]
        # ListNames has one array argument; some busctl versions preserve that outer array.
        if len(names) == 1 and isinstance(names[0], list):
            names = names[0]
        players = [name for name in names if isinstance(name, str) and name.startswith("org.mpris.MediaPlayer2.")]
        playing = []
        for player in players:
            if await self.property(player, "PlaybackStatus") == "Playing":
                playing.append(player)
        candidates = playing or players
        if len(candidates) != 1:
            raise RuntimeError("No unique media player; start one player or pause the others")
        player = candidates[0]
        if code == 164:
            if not await self.property(player, "CanControl"):
                raise RuntimeError("The media player does not allow playback control")
            arguments = ["PlayPause"]
        else:
            if not await self.property(player, "CanSeek"):
                raise RuntimeError("The media player does not support seeking")
            arguments = ["Seek", "x", str(-10_000_000 if code == 168 else 10_000_000)]
        await run([
            "busctl", "--user", "call", player, "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player", *arguments,
        ])

    async def property(self, player: str, name: str) -> object:
        return json.loads(await run([
            "busctl", "--user", "--json=short", "get-property", player,
            "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", name,
        ]))["data"]

    async def key(self, code: int, repeat: bool = False) -> None:
        if code in THEATER_ACTIONS:
            active = False
            if await self.theater_owned():
                active = await self.theater_boolean([
                    "get-property", THEATER_SERVICE, THEATER_OBJECT, THEATER_SERVICE, "Active",
                ])
            if code in FOCUS_DIRECTIONS:
                if repeat and self.direction_routes.get(code) is not active:
                    await self.release_direction(code, commit=False)
                    self.direction_routes[code] = None
                    return
                if not repeat:
                    self.direction_routes[code] = active
            if active:
                try:
                    if code in HORIZONTAL_DIRECTIONS:
                        owner = await self.theater_owner()
                        if repeat and self.direction_owners.get(code) != owner:
                            await self.release_direction(code, commit=False)
                            self.direction_routes[code] = None
                            return
                        if await self.supports_direction(owner):
                            phase = "repeat" if repeat else "press"
                            await self.theater_call(
                                "Direction", "ss", THEATER_ACTIONS[code], phase, owner=owner)
                            self.theater_directions[code] = owner
                            self.direction_updated[code] = time.monotonic()
                        else:
                            await self.theater_call("Dispatch", "s", THEATER_ACTIONS[code], owner=owner)
                        self.direction_owners[code] = owner
                    else:
                        await self.theater_call("Dispatch", "s", THEATER_ACTIONS[code])
                except (RuntimeError, OSError) as error:
                    if code in FOCUS_DIRECTIONS:
                        try:
                            await self.release_direction(code, commit=False)
                        except (RuntimeError, OSError) as cancellation:
                            self.direction_routes[code] = None
                            raise RuntimeError(
                                f"{error}; held direction cancellation also failed: {cancellation}"
                            ) from error
                        self.direction_routes[code] = None
                    raise
                return
        if code in NAVIGATION:
            window = json.loads(await run(["hyprctl", "activewindow", "-j"]))
            if not isinstance(window, dict):
                raise RuntimeError("Invalid Hyprland active window response")
            if window.get("class", "").casefold() in STREMIO_CLASSES:
                if code in FOCUS_DIRECTIONS or code == 353:
                    await self.focus(FOCUS_DIRECTIONS.get(code, "select"), window, repeat)
                else:
                    await self.focus("back", window)
            else:
                await send_key(NAVIGATION[code])
        elif code == 172:
            await self.home()
        elif code in {164, 168, 208}:
            await self.media(code)
        elif code in {114, 115}:
            await run(["wpctl", "set-volume", "--limit", "1.0", "@DEFAULT_AUDIO_SINK@",
                       "5%+" if code == 115 else "5%-"])
        else:
            raise ValueError(f"Unsupported computer key: {code}")


def rule_text(address: str) -> str:
    if not ADDRESS.fullmatch(address):
        raise ValueError("Select a valid remote before installing input access")
    return (
        "# Managed by fredrik.shield-remote; this remote only.\n"
        f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{name}}=="{REMOTE_NAME}", '
        f'ATTRS{{uniq}}=="{address.lower()}", ENV{{LIBINPUT_IGNORE_DEVICE}}="1", '
        'TAG-="power-switch", TAG+="uaccess"\n'
    )


def rule_installed(address: str) -> bool:
    return bool(address) and RULE.exists() and RULE.read_text() == rule_text(address)


def setup_ready(paths: Paths, address: str) -> bool:
    receipt = paths.config / "input-setup.json"
    if not rule_installed(address) or not receipt.exists():
        return False
    try:
        payload = json.loads(receipt.read_text())
    except json.JSONDecodeError as error:
        LOG.error("Invalid input setup receipt; refusing remote access: %s", error)
        return False
    return isinstance(payload, dict) and payload.get("schema") == 1 and payload.get("address") == address


def input_safe(properties: str) -> bool:
    values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
    return (values.get("LIBINPUT_IGNORE_DEVICE") == "1"
            and "CURRENT_TAGS" in values
            and "power-switch" not in values["CURRENT_TAGS"].split(":"))


def service_text() -> str:
    executable = json.dumps(sys.executable.replace("%", "%%"))
    script = json.dumps(str(PLUGIN / "router.py").replace("%", "%%"))
    return (
        "# Managed by fredrik.shield-remote\n[Unit]\n"
        "Description=Shield remote target router\nAfter=graphical-session.target\n"
        "PartOf=graphical-session.target\n\n[Service]\nType=simple\n"
        f"ExecStart={executable} {script} serve\n"
        "Restart=on-failure\nRestartSec=3\nUMask=0077\n"
        "Environment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=graphical-session.target\n"
    )


def microphone_service_text(paths: Paths, address: str) -> str:
    if not ADDRESS.fullmatch(address):
        raise ValueError("Select a valid remote before installing microphone audio")
    executable = json.dumps(str(paths.microphone_binary).replace("%", "%%"))
    return (
        "# Managed by fredrik.shield-remote\n"
        "[Unit]\nDescription=NVIDIA Shield Remote microphone (ATVVoice)\n"
        "Requires=pipewire.service\nAfter=graphical-session.target pipewire.service\n"
        "PartOf=graphical-session.target\n\n[Service]\nType=simple\n"
        f"ExecStart={executable} --device {address} --name shield-remote "
        '--description "NVIDIA Shield Remote Microphone" --mic-on-demand\n'
        "Restart=on-failure\nRestartSec=5\nKillSignal=SIGINT\nTimeoutStopSec=10\n"
        "UMask=0077\n\n[Install]\nWantedBy=graphical-session.target\n"
    )


def install_microphone(paths: Paths, config: Config) -> None:
    unit = microphone_service_text(paths, config.remote_address)
    service = paths.microphone_service
    if service.parent.is_symlink() or service.is_symlink():
        raise RuntimeError(f"Refusing a symlinked microphone user service: {service}")
    if service.exists() and not service.read_text().startswith("# Managed by fredrik.shield-remote\n"):
        legacy = unit.removeprefix("# Managed by fredrik.shield-remote\n").replace(
            json.dumps(str(paths.microphone_binary).replace("%", "%%")),
            "%h/.local/bin/atvvoice",
        )
        if service.read_text() != legacy:
            raise RuntimeError(f"Refusing to overwrite an unrelated microphone service: {service}")
    for directory in (paths.data, paths.microphone_binary.parent, paths.data / "cargo-target"):
        if directory.is_symlink() or (directory.exists() and directory.stat().st_uid != os.getuid()):
            raise RuntimeError(f"Microphone storage must be owned by you and machine-local: {directory}")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    if paths.microphone_binary.is_symlink():
        raise RuntimeError(f"Refusing to replace a symlinked microphone binary: {paths.microphone_binary}")
    target = paths.data / "cargo-target"
    subprocess.run([
        "cargo", "build", "--manifest-path", str(PLUGIN / "microphone/Cargo.toml"),
        "--locked", "--release", "--target-dir", str(target),
    ], check=True)
    with tempfile.NamedTemporaryFile(dir=paths.microphone_binary.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        shutil.copyfile(target / "release/atvvoice", temporary)
        temporary.chmod(0o755)
        temporary.replace(paths.microphone_binary)
    finally:
        temporary.unlink(missing_ok=True)
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", MICROPHONE_SERVICE], check=True)
    subprocess.run(["systemctl", "--user", "restart", MICROPHONE_SERVICE], check=True)
    print("Plugin microphone built, installed and enabled.")


def setup_complete(paths: Paths, address: str) -> bool:
    enabled = paths.service.parent / "graphical-session.target.wants" / SERVICE
    microphone_enabled = enabled.with_name(MICROPHONE_SERVICE)
    return (setup_ready(paths, address) and not paths.service.is_symlink()
            and paths.service.is_file() and paths.service.read_text() == service_text()
            and enabled.is_symlink() and enabled.resolve() == paths.service.resolve()
            and not paths.microphone_binary.is_symlink() and paths.microphone_binary.is_file()
            and os.access(paths.microphone_binary, os.X_OK)
            and not paths.microphone_service.is_symlink() and paths.microphone_service.is_file()
            and paths.microphone_service.read_text() == microphone_service_text(paths, address)
            and microphone_enabled.is_symlink()
            and microphone_enabled.resolve() == paths.microphone_service.resolve())


class Router:
    def __init__(self, paths: Paths, config: Config) -> None:
        self.paths = paths
        self.config = config
        self.computer = Computer(config.tv_output)
        self.target = saved_target(paths)
        self.client: AndroidTVRemote | None = None
        self.pairing_client: AndroidTVRemote | None = None
        self.pairing_deadline = 0.0
        self.shield_connected = False
        self.auth_invalid = False
        self.input_connected = False
        self.input_captured = False
        self.input_waiting_for_release = False
        self.input_error = ""
        self.remote_pairing = False
        self.remote_pairing_stage = ""
        self.shield_error = ""
        self.last_error = ""
        self.last_action = ""
        self.last_volume_completed = 0.0
        self.last_key = ""
        self.last_notice = ""
        self.last_menu: float | None = None
        self.pressed: set[int] = set()
        self.shield_pressed: dict[int, str] = {}
        self.routing_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.input_changed = asyncio.Event()
        self.voice_jobs: dict[VoiceStream, asyncio.Task[None]] = {}

    @property
    def paired(self) -> bool:
        return bool(self.config.shield_host and self.config.paired_host == self.config.shield_host
                    and not self.auth_invalid)

    def state(self, running: bool = True) -> dict:
        return {
            "schema": 1, "running": running, "target": self.target,
            "remote_address": self.config.remote_address,
            "tv_output": self.config.tv_output,
            "soundbar": {"host": self.config.soundbar_host},
            "setup_complete": setup_complete(self.paths, self.config.remote_address),
            "input": {"connected": self.input_connected, "captured": self.input_captured,
                      "waiting_for_release": self.input_waiting_for_release,
                      "setup": setup_ready(self.paths, self.config.remote_address), "error": self.input_error,
                      "pairing": self.remote_pairing, "pairing_stage": self.remote_pairing_stage},
            "shield": {"host": self.config.shield_host, "paired": self.paired,
                       "connected": self.shield_connected, "pairing": self.pairing_client is not None,
                       "voice_active": bool(self.voice_jobs),
                       "error": self.shield_error},
            "error": self.last_error, "last_key": self.last_key, "last_action": self.last_action,
        }

    async def notice(self, message: str) -> None:
        if message == self.last_notice:
            return
        self.last_notice = message
        try:
            await run(["notify-send", "Shield Remote", message])
        except (OSError, RuntimeError) as error:
            LOG.error("Could not show notification: %s", error)

    async def error(self, message: str) -> None:
        self.last_error = message
        LOG.error("%s", message)
        await self.notice(message)

    async def dictation_state(self) -> str:
        response = json.loads(await run(["voxtype", "status", "--format", "json"]))
        if not isinstance(response, dict) or response.get("alt") not in ("idle", "recording", "transcribing"):
            raise RuntimeError("Voxtype daemon is unavailable or not ready for dictation")
        return response["alt"]

    async def wait_dictation(self, recording: bool) -> None:
        try:
            async with asyncio.timeout(3):
                while (await self.dictation_state() == "recording") != recording:
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise RuntimeError(f"Voxtype did not {'start' if recording else 'stop'} recording") from None

    async def restore_dictation_input(self) -> None:
        file = self.paths.runtime / "dictation-input.json"
        if file.is_symlink():
            raise RuntimeError("Refusing symlinked dictation input state")
        if not file.exists():
            return
        payload = json.loads(file.read_text())
        if (not isinstance(payload, dict) or set(payload) != {"schema", "previous_source"}
                or type(payload["schema"]) is not int or payload["schema"] != 1
                or not isinstance(payload["previous_source"], str) or not payload["previous_source"]):
            raise ValueError("Invalid saved dictation input; refusing to change the default microphone")
        current = await run(["pactl", "get-default-source"])
        if current == REMOTE_MIC:
            await run(["pactl", "set-default-source", payload["previous_source"]])
        else:
            LOG.info("Default microphone changed during dictation; preserving %s", current)
        file.unlink()

    async def require_remote_microphone(self) -> None:
        sources = json.loads(await run(["pactl", "--format=json", "list", "sources"]))
        if not isinstance(sources, list) or not any(
            isinstance(source, dict) and source.get("name") == REMOTE_MIC for source in sources
        ):
            raise RuntimeError("Shield microphone is unavailable; connect the remote and start ATVVoice")

    async def toggle_dictation(self) -> None:
        state = await self.dictation_state()
        if state == "recording":
            await run(["voxtype", "record", "stop"])
            await self.wait_dictation(False)
            await self.restore_dictation_input()
            return
        if state != "idle":
            raise RuntimeError("Voxtype is transcribing; wait before starting another recording")
        await self.restore_dictation_input()
        await self.require_remote_microphone()
        previous = await run(["pactl", "get-default-source"])
        if not previous:
            raise RuntimeError("Cannot save the current default microphone")
        self.paths.prepare()
        write_private_json(self.paths.runtime / "dictation-input.json",
                           {"schema": 1, "previous_source": previous})
        try:
            await run(["pactl", "set-default-source", REMOTE_MIC])
            await run(["voxtype", "record", "start"])
            await self.wait_dictation(True)
        except (OSError, RuntimeError, ValueError, asyncio.CancelledError) as error:
            try:
                await self.close_dictation()
            except (OSError, RuntimeError, ValueError) as recovery:
                raise RuntimeError(f"Dictation failed: {error}; input recovery failed: {recovery}") from error
            raise

    async def dictation_loop(self) -> None:
        while True:
            async with self.routing_lock:
                if (self.paths.runtime / "dictation-input.json").exists():
                    try:
                        if await self.dictation_state() != "recording":
                            await self.restore_dictation_input()
                    except (OSError, RuntimeError, ValueError) as error:
                        await self.error(f"Shield dictation input recovery failed: {error}")
                        await self.restore_dictation_input()
            await asyncio.sleep(0.1)

    async def close_dictation(self) -> None:
        if not (self.paths.runtime / "dictation-input.json").exists():
            return
        try:
            if await self.dictation_state() == "recording":
                await run(["voxtype", "record", "cancel"])
                await self.wait_dictation(False)
        finally:
            await self.restore_dictation_input()

    def new_client(self) -> AndroidTVRemote:
        client = VoiceRemote(
            "Omarchy Shield Remote", str(self.paths.config / "client.crt"),
            str(self.paths.config / "client.key"), self.config.shield_host, enable_ime=False,
            enable_voice=True,
            voice_requested=lambda stream: self.tv_voice_requested(client, stream),
            voice_ended=self.tv_voice_ended,
        )
        return client

    def tv_voice_requested(self, client: AndroidTVRemote, stream: VoiceStream) -> None:
        if self.client is not client or self.target != "shield" or not self.input_captured:
            LOG.info("Declining TV microphone request outside captured Shield mode")
            stream.end()
            return
        job = asyncio.create_task(self.tv_voice_loop(client, stream))
        self.voice_jobs[stream] = job

        def finished(task: asyncio.Task[None]) -> None:
            self.voice_jobs.pop(stream, None)
            stream.end()
            if not task.cancelled() and (error := task.exception()):
                LOG.error("Shield voice worker failed", exc_info=(type(error), error, error.__traceback__))

        job.add_done_callback(finished)

    def tv_voice_ended(self, stream: VoiceStream) -> None:
        job = self.voice_jobs.get(stream)
        if job and not job.done() and not job.cancelling():
            job.cancel()

    def cancel_tv_voice(self) -> None:
        for job in self.voice_jobs.values():
            if not job.done() and not job.cancelling():
                job.cancel()

    async def close_tv_voice(self) -> None:
        jobs = list(self.voice_jobs.values())
        self.cancel_tv_voice()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def tv_voice_loop(self, client: AndroidTVRemote, stream: VoiceStream) -> None:
        try:
            async with asyncio.timeout(TV_VOICE_TIMEOUT):
                async with self.routing_lock:
                    if self.client is not client or self.target != "shield" or not self.input_captured:
                        return
                    await self.require_remote_microphone()
                LOG.info("Forwarding remote microphone to app-requested Shield voice session")
                await stream_microphone(stream, REMOTE_MIC)
        except TimeoutError:
            LOG.warning("Shield app microphone stopped at the %s-second safety limit", TV_VOICE_TIMEOUT)
            await self.notice(f"Shield microphone stopped after {TV_VOICE_TIMEOUT} seconds")
        except (OSError, RuntimeError, ValueError, ConnectionClosed) as error:
            await self.error(f"Shield voice capture failed: {error}")
        finally:
            LOG.info("Shield app microphone session closed")

    async def release_shield(self) -> None:
        await self.close_tv_voice()
        held, self.shield_pressed = self.shield_pressed, {}
        for key in held.values():
            if self.client and self.shield_connected:
                try:
                    self.client.send_key_command(key, "END_LONG")
                except ConnectionClosed as error:
                    self.shield_connected = False
                    await self.error(f"Shield disconnected while releasing a key: {error}")

    async def select(self, target: str) -> None:
        if not isinstance(target, str) or target not in {"computer", "shield"}:
            raise ValueError("Unknown control target")
        if target == "shield" and not self.paired:
            raise ValueError("Pair the Shield TV in this panel before selecting it")
        await self.computer.cancel_directions()
        await self.release_shield()
        await self.switch_soundbar(target)
        try:
            save_target(self.paths, target)
        except (OSError, RuntimeError) as error:
            try:
                await self.switch_soundbar(self.target)
            except RuntimeError as rollback:
                raise RuntimeError(f"Cannot preserve target: {error}; soundbar rollback failed: {rollback}") from error
            raise RuntimeError(f"Cannot preserve target; prior target retained: {error}") from error
        self.target = target
        self.last_error = ""
        self.last_action = f"Target: {target}"
        await self.notice("Target: Shield TV" if target == "shield" else "Target: Computer")

    async def switch_soundbar(self, target: str) -> None:
        if not self.config.soundbar_host:
            return
        port = 1 if target == "shield" else 2
        await self.soundbar_command(
            "avContent", "setPlayContent", {"uri": f"extInput:hdmi?port={port}", "output": ""},
            "1.2", f"HDMI{port} switch",
        )

    async def soundbar_command(
        self, service: str, method: str, values: dict[str, str], version: str, action: str,
    ) -> None:
        host = self.config.soundbar_host
        if ":" in host:
            host = f"[{host}]"
        payload = {
            "method": method, "params": [values], "id": 1, "version": version,
        }
        try:
            response = json.loads(await run([
                "curl", "--noproxy", "*", "--silent", "--show-error", "--fail-with-body",
                "--connect-timeout", "2", "--max-time", "5",
                "--header", "Content-Type: application/json", "--data", json.dumps(payload),
                f"http://{host}:10000/sony/{service}",
            ], timeout=6))
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeError(f"Soundbar {action} failed: {error}") from error
        if not isinstance(response, dict) or response.get("id") != 1:
            raise RuntimeError(f"Soundbar returned an invalid response for {action}")
        if "error" in response:
            raise RuntimeError(f"Soundbar rejected {action}: {json.dumps(response['error'])}")
        if response.get("result") != []:
            raise RuntimeError(f"Soundbar did not confirm the {action}")

    async def key(self, code: int, value: int, timestamp: float) -> None:
        async with self.routing_lock:
            self.last_key = f"{code}:{value}"
            if value == 0:
                self.pressed.discard(code)
                await self.computer.release_direction(code)
                key = self.shield_pressed.pop(code, None)
                if key and self.client and self.shield_connected:
                    self.client.send_key_command(key, "END_LONG")
                return
            if value == 1:
                if code in self.pressed:
                    return
                self.pressed.add(code)
            if code == 139:
                if value == 1:
                    if self.last_menu is not None and 0 <= timestamp - self.last_menu <= 0.5:
                        self.last_menu = None
                        await self.select("shield" if self.target == "computer" else "computer")
                    else:
                        self.last_menu = timestamp
                self.last_action = "Menu reserved for double-press target switching"
                return
            computer_dictation = code == 217 and self.target == "computer"
            if (code in IGNORED and not computer_dictation) or (code == 116 and self.target == "computer"):
                self.last_action = f"Ignored key {code}"
                return
            if code not in SHIELD_KEYS and not computer_dictation:
                raise ValueError(f"Unexpected remote key {code}; it was not forwarded")
            if code in VOLUME_KEYS and self.config.soundbar_host:
                if value == 2 and time.monotonic() - self.last_volume_completed < 0.12:
                    return
                delta = VOLUME_KEYS[code]
                await self.soundbar_command(
                    "audio", "setAudioVolume", {"output": "", "volume": delta},
                    "1.1", f"volume {delta}",
                )
                self.last_volume_completed = time.monotonic()
                self.last_action = f"Soundbar: volume {delta}"
            elif self.target == "shield":
                if value == 2:
                    return
                if not self.shield_connected or not self.client:
                    raise RuntimeError("Shield is disconnected; this key was dropped, not sent to the computer")
                key = SHIELD_KEYS[code]
                if code == 116:
                    self.client.send_key_command(key, "SHORT")
                else:
                    self.client.send_key_command(key, "START_LONG")
                    self.shield_pressed[code] = key
                self.last_action = f"Shield: {key}"
            elif value == 1 or code in REPEAT:
                if value == 2 and code in FOCUS_DIRECTIONS and code not in self.pressed:
                    return
                if 139 in self.pressed and code in FOCUS_DIRECTIONS:
                    self.last_menu = None
                if computer_dictation:
                    await self.toggle_dictation()
                else:
                    await self.computer.key(code, repeat=value == 2)
                self.last_action = "Computer: dictation toggle" if computer_dictation else f"Computer: key {code}"
            self.last_error = ""
            self.last_notice = ""

    def disconnect_control(self) -> None:
        self.cancel_tv_voice()
        if self.client:
            self.client.disconnect()
        self.client = None
        self.shield_connected = False
        self.shield_pressed.clear()

    async def configure(self, values: dict) -> None:
        if not isinstance(values, dict) or set(values) - {
            "remote_address", "shield_host", "tv_output", "soundbar_host",
        }:
            raise ValueError("Invalid configuration update")
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError("Configuration values must be text")
        if values.get("remote_address") == "auto":
            values = {**values, "remote_address": await auto_remote()}
        config = dataclasses.replace(self.config, **values).validate()
        async with self.routing_lock:
            await self.computer.cancel_directions()
            await self.release_shield()
            if config.shield_host != self.config.shield_host:
                self.cancel_pairing()
                self.disconnect_control()
                self.auth_invalid = False
                self.shield_error = ""
            save_config(self.paths, config)
            if config.remote_address != self.config.remote_address:
                self.input_changed.set()
            self.config = config
            self.computer.tv_output = config.tv_output
            self.last_error = ""

    def cancel_pairing(self) -> None:
        if self.pairing_client:
            self.pairing_client.disconnect()
        self.pairing_client = None
        self.pairing_deadline = 0

    async def pair_start(self, host: str) -> None:
        if not host:
            raise ValueError("Enter the Shield IP address or hostname first")
        await self.configure({"shield_host": host})
        self.cancel_pairing()
        self.disconnect_control()
        client = self.new_client()
        self.pairing_client = client
        try:
            await client.async_generate_cert_if_missing()
            for name in ("client.crt", "client.key"):
                (self.paths.config / name).chmod(0o600)
            await asyncio.wait_for(client.async_start_pairing(), 15)
        except (*NETWORK_ERRORS, ValueError):
            self.cancel_pairing()
            raise
        self.pairing_deadline = time.monotonic() + 120
        self.shield_error = ""

    async def pair_finish(self, code: str) -> None:
        if not self.pairing_client:
            raise ValueError("Start pairing before entering the TV code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Fa-f]{6}", code):
            raise ValueError("Enter the six-character hexadecimal code shown on the TV")
        if self.pairing_deadline and time.monotonic() > self.pairing_deadline:
            self.cancel_pairing()
            raise ValueError("Pairing timed out; start again for a fresh TV code")
        self.pairing_deadline = 0
        client = self.pairing_client
        try:
            await asyncio.wait_for(client.async_finish_pairing(code.upper()), 15)
        finally:
            self.cancel_pairing()
        self.config = dataclasses.replace(self.config, paired_host=self.config.shield_host)
        save_config(self.paths, self.config)
        self.auth_invalid = False
        self.shield_error = ""

    async def network_loop(self) -> None:
        while True:
            if self.pairing_client:
                if self.pairing_deadline and time.monotonic() > self.pairing_deadline:
                    self.cancel_pairing()
                    self.shield_error = "Pairing timed out; start again for a fresh TV code"
                    await self.error(self.shield_error)
            elif self.paired and not self.client:
                client = self.new_client()
                self.client = client

                def available(value: bool, current: AndroidTVRemote = client) -> None:
                    if self.client is current:
                        self.shield_connected = value
                        if not value:
                            self.cancel_tv_voice()
                            self.shield_pressed.clear()
                            self.shield_error = "Shield connection lost; reconnecting"
                        else:
                            self.shield_error = ""

                def invalid_auth(current: AndroidTVRemote = client) -> None:
                    if self.client is current:
                        self.auth_invalid = True
                        self.shield_connected = False
                        self.shield_error = "Shield authorization expired; pair again"
                        LOG.error("%s", self.shield_error)

                client.add_is_available_updated_callback(available)
                try:
                    await asyncio.wait_for(client.async_connect(), 15)
                except NETWORK_ERRORS as error:
                    if self.client is client:
                        self.disconnect_control()
                        self.auth_invalid = isinstance(error, InvalidAuth)
                        self.shield_error = f"Cannot connect to Shield: {error}"
                        LOG.warning("%s", self.shield_error)
                else:
                    if self.client is client:
                        self.shield_connected = True
                        self.shield_error = ""
                        client.keep_reconnecting(invalid_auth)
                    else:
                        client.disconnect()
            await asyncio.sleep(3)

    async def connect_bluetooth(self, address: str) -> None:
        async with bluetooth_discovery():
            device = await self.wait_bluez_device(address)
            await bluez_connect(device)

    async def wait_bluez_device(self, address: str) -> str:
        async with asyncio.timeout(10):
            while not (device := await bluez_device(address)):
                await asyncio.sleep(0.2)
        return device

    async def wait_capture(self) -> None:
        try:
            async with asyncio.timeout(7):
                while not self.input_captured:
                    await asyncio.sleep(0.1)
        except TimeoutError:
            raise RuntimeError("Bluetooth connected but safe button capture was unavailable: "
                               + self.input_error) from None

    async def rollback_bluetooth(self, device: str, cancel_pairing: bool = False) -> None:
        failures = []
        operations = []
        if cancel_pairing:
            operations.append(("cancel pairing", lambda: bluez_cancel_pairing(device)))
        operations.extend([
            ("block remote", lambda: bluez_set(device, "Blocked", True)),
            ("disconnect remote", lambda: bluez_disconnect(device)),
        ])
        for label, operation in operations:
            try:
                await operation()
            except (OSError, RuntimeError, ValueError) as error:
                LOG.error("Bluetooth rollback could not %s: %s", label, error)
                failures.append(f"{label}: {error}")
        if failures:
            raise RuntimeError("Bluetooth safety rollback failed: " + "; ".join(failures))

    async def pair_remote(self, repair: bool = False) -> None:
        address = self.config.remote_address
        if not setup_ready(self.paths, address):
            raise ValueError("Apply remote-only input setup before Bluetooth pairing")
        device = None
        self.remote_pairing = True
        self.remote_pairing_stage = "Discovering selected remote; hold Home + Back until it blinks"
        self.last_error = self.last_notice = ""
        self.input_error = ""
        try:
            async with asyncio.timeout(60):
                async with bluetooth_discovery(timeout=75):
                    device = await self.wait_bluez_device(address)
                    await bluez_cancel_pairing(device)
                    await bluez_disconnect(device)
                    await bluez_set(device, "Blocked", False)
                    if repair:
                        self.remote_pairing_stage = "Forgetting only the selected remote's old Linux bond"
                        await run([
                            "busctl", "--system", "call", "org.bluez", device.rsplit("/", 1)[0],
                            "org.bluez.Adapter1", "RemoveDevice", "o", device,
                        ], timeout=5)
                        device = None
                        device = await self.wait_bluez_device(address)
                        await bluez_set(device, "Blocked", False)
                    if not await bluez_property(device, "Paired"):
                        self.remote_pairing_stage = "Pairing: keep Home + Back held until the indicator blinks"
                        await bluez_call(device, "Pair", timeout=35)
                    if not await bluez_property(device, "Paired") or not await bluez_property(device, "Bonded"):
                        raise RuntimeError("BlueZ did not save the remote's Bluetooth bond")
                    await bluez_set(device, "Trusted", True)
                    self.remote_pairing_stage = "Connecting the paired remote"
                    if not await bluez_property(device, "Connected"):
                        await bluez_connect(device, timeout=20)
                    self.remote_pairing_stage = "Verifying safe button capture"
                    await self.wait_capture()
        except (TimeoutError, OSError, RuntimeError, ValueError, asyncio.CancelledError) as error:
            if device:
                try:
                    await self.rollback_bluetooth(device, cancel_pairing=True)
                except RuntimeError as rollback_error:
                    raise RuntimeError(f"{error}; {rollback_error}") from error
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError(
                f"Bluetooth pairing failed: {error or 'discovery timed out'}. "
                "Hold Home + Back until the remote blinks, then use Pair remote. "
                "If its old bond is stale, use Repair pairing and confirm."
            ) from error
        finally:
            self.remote_pairing = False
            self.remote_pairing_stage = ""
        self.last_error = self.last_notice = ""
        self.last_action = "Physical remote paired, trusted and safely captured"

    async def input_loop(self) -> None:
        while True:
            address = self.config.remote_address
            self.input_changed.clear()
            if not setup_ready(self.paths, address):
                self.input_error = "Remote-only input setup is required"
                await asyncio.sleep(2)
                continue
            node = find_remote(address)
            if not node:
                if self.remote_pairing:
                    self.input_error = ""
                else:
                    try:
                        device = await bluez_device(address)
                        if not device or not await bluez_property(device, "Paired"):
                            self.input_error = "Remote is not Bluetooth-paired; hold Home + Back and use Pair remote"
                        else:
                            self.input_error = "Remote is not connected; wake it with an arrow and use Reconnect remote"
                    except (OSError, RuntimeError, ValueError) as error:
                        self.input_error = f"Cannot read selected remote's Bluetooth state: {error}"
                        LOG.warning("%s", self.input_error)
                await asyncio.sleep(2)
                continue
            device = None
            workers: list[asyncio.Task] = []
            try:
                await run(["udevadm", "settle", "--timeout=5"])
                properties = await run(["udevadm", "info", "--query=property", "--name", str(node)])
                if not input_safe(properties):
                    details = "; ".join(
                        line for line in properties.splitlines()
                        if line.startswith(("LIBINPUT_IGNORE_DEVICE=", "CURRENT_TAGS="))
                    )
                    message = ("Input safety check failed; remote blocked. "
                               + (details or "Required udev properties are missing"))
                    await run(["bluetoothctl", "block", address])
                    await self.error(message)
                    raise RuntimeError(message)
                device = InputDevice(str(node))
                if (device.name != REMOTE_NAME or device.uniq.lower() != address.lower()
                        or device.info.vendor != 0x0955 or device.info.product != 0x7217):
                    raise RuntimeError("Refusing input from a different device")
                device.grab()
                held_at_capture = set(device.active_keys())
                self.input_connected = self.input_captured = True
                self.input_waiting_for_release = bool(held_at_capture)
                self.input_error = ""
                async def read_events() -> None:
                    async for event in device.async_read_loop():
                        if event.type == 1:
                            if self.input_waiting_for_release:
                                if event.value == 0:
                                    held_at_capture.discard(event.code)
                                else:
                                    held_at_capture.add(event.code)
                                self.input_waiting_for_release = bool(held_at_capture)
                                continue
                            if (event.value == 2 and time.time() - event.timestamp() > 0.25
                                    and ((event.code in FOCUS_DIRECTIONS and self.target == "computer")
                                         or (event.code in VOLUME_KEYS and self.config.soundbar_host))):
                                continue
                            try:
                                await self.key(event.code, event.value, event.timestamp())
                            except (ValueError, RuntimeError, ConnectionClosed, OSError) as error:
                                await self.error(str(error))

                async def keep_held_directions_live() -> None:
                    while True:
                        await asyncio.sleep(0.5)
                        async with self.routing_lock:
                            if (self.target != "computer" or self.input_waiting_for_release
                                    or not self.computer.theater_directions):
                                continue
                            held = set(device.active_keys())
                            for code in tuple(self.computer.theater_directions):
                                if (code not in self.pressed or code not in held
                                        or time.monotonic() - self.computer.direction_updated[code] < 0.75):
                                    continue
                                try:
                                    await self.computer.key(code, repeat=True)
                                except (ValueError, RuntimeError, ConnectionClosed, OSError) as error:
                                    await self.error(str(error))

                workers = [
                    asyncio.create_task(read_events()),
                    asyncio.create_task(keep_held_directions_live()),
                    asyncio.create_task(self.input_changed.wait()),
                ]
                done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                for worker in done:
                    worker.result()
            except (OSError, RuntimeError) as error:
                self.input_error = str(error)
                LOG.warning("Remote capture: %s", error)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                self.input_connected = self.input_captured = False
                self.input_waiting_for_release = False
                self.pressed.clear()
                self.last_menu = None
                try:
                    await self.computer.cancel_directions()
                except (RuntimeError, OSError) as error:
                    await self.error(str(error))
                await self.release_shield()
                if device:
                    device.close()
            await asyncio.sleep(2)

    async def request(self, payload: dict) -> dict:
        if payload.get("command") == "status":
            return self.state()
        async with self.command_lock:
            return await self.mutate(payload)

    async def mutate(self, payload: dict) -> dict:
        command = payload.get("command")
        if command == "configure":
            await self.configure(payload.get("values"))
        elif command == "target":
            async with self.routing_lock:
                await self.select(payload.get("target"))
        elif command in {"open_theater", "open_stremio"}:
            async with self.routing_lock:
                await self.computer.home()
                await self.select("computer")
                self.last_action = "Raised Home Theater"
        elif command == "pair_start":
            await self.pair_start(payload.get("host", ""))
        elif command == "pair_finish":
            await self.pair_finish(payload.get("code", ""))
        elif command == "pair_cancel":
            self.cancel_pairing()
        elif command in {"pair_remote", "repair_remote"}:
            if command == "repair_remote" and payload.get("confirm") is not True:
                raise ValueError("Confirm forgetting only the selected remote's Linux bond before repair")
            await self.pair_remote(repair=command == "repair_remote")
        elif command == "connect_remote":
            if not setup_ready(self.paths, self.config.remote_address):
                raise ValueError("Apply the remote-only input setup before unblocking this remote")
            address = self.config.remote_address
            device = await bluez_device(address)
            if not device or not await bluez_property(device, "Paired") or not await bluez_property(device, "Bonded"):
                raise ValueError("Physical remote is not paired; hold Home + Back until it blinks and use Pair remote")
            try:
                await bluez_disconnect(device)
                await bluez_set(device, "Blocked", False)
                await self.connect_bluetooth(address)
                await self.wait_capture()
            except (*NETWORK_ERRORS, RuntimeError, ValueError, asyncio.CancelledError) as error:
                try:
                    await self.rollback_bluetooth(device)
                except RuntimeError as rollback_error:
                    raise RuntimeError(f"{error}; {rollback_error}") from error
                raise
            self.last_error = ""
            self.last_notice = ""
        else:
            raise ValueError("Unknown Shield Remote command")
        return self.state()

    async def serve(self) -> None:
        self.paths.prepare()
        if os.geteuid() == 0:
            raise RuntimeError("The routing service must run as your user, not root")
        with (self.paths.runtime / "router.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("A Shield Remote backend is already running") from None
            await self.close_dictation()
            self.paths.socket.unlink(missing_ok=True)

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    raw = await asyncio.wait_for(reader.readline(), 5)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("Request must be an object")
                    response = {"ok": True, "state": await self.request(payload)}
                except (*NETWORK_ERRORS, ValueError, RuntimeError) as error:
                    message = "Shield rejected the pairing code; start pairing again" if isinstance(error, InvalidAuth) else str(error)
                    await self.error(message)
                    response = {"ok": False, "error": message, "state": self.state()}
                writer.write((json.dumps(response) + "\n").encode())
                try:
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    LOG.warning("Plugin client disconnected before receiving its response")
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except (BrokenPipeError, ConnectionResetError):
                        LOG.warning("Plugin client closed its connection during cleanup")

            server = await asyncio.start_unix_server(handle, path=str(self.paths.socket), limit=8192)
            self.paths.socket.chmod(0o600)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
            tasks = [
                asyncio.create_task(self.input_loop()),
                asyncio.create_task(self.network_loop()),
                asyncio.create_task(self.dictation_loop()),
                asyncio.create_task(stop.wait()),
            ]
            try:
                async with server:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if tasks[-1] not in done:
                        raise RuntimeError("A routing worker stopped unexpectedly")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await self.close_dictation()
                finally:
                    try:
                        await self.computer.cancel_directions()
                    except (RuntimeError, OSError) as error:
                        await self.error(str(error))
                    await self.release_shield()
                    self.cancel_pairing()
                    self.disconnect_control()
                    self.paths.socket.unlink(missing_ok=True)


def find_remote(address: str) -> Path | None:
    for entry in Path("/sys/class/input").glob("event*"):
        try:
            name = (entry / "device/name").read_text().strip()
            uniq = (entry / "device/uniq").read_text().strip()
        except FileNotFoundError:
            continue
        if name == REMOTE_NAME and uniq.lower() == address.lower():
            return Path("/dev/input") / entry.name
    return None


async def auto_remote(paired_only: bool = True) -> str:
    command = ["bluetoothctl", "devices"]
    if paired_only:
        command.append("Paired")
    output = await run(command)
    addresses = [
        match.group(1) for line in output.splitlines()
        if (match := re.fullmatch(r"Device ([0-9A-Fa-f:]{17}) NVIDIA SHIELD Remote", line))
    ]
    if len(addresses) != 1:
        kind = "paired" if paired_only else "discovered"
        raise ValueError(f"Exactly one {kind} NVIDIA SHIELD Remote is required; use Omarchy's Bluetooth panel to discover it")
    return addresses[0].upper()


def client_request(paths: Paths, payload: dict) -> dict:
    if payload.get("command") == "status" and not paths.socket.exists():
        state = Router(paths, load_config(paths)).state(running=False)
        state["error"] = "Backend is stopped; complete setup or start the user service"
        return {"ok": True, "state": state}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(90)
        connection.connect(str(paths.socket))
        connection.sendall((json.dumps(payload) + "\n").encode())
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(65536))
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise RuntimeError("Invalid backend response")
        return response


def install(paths: Paths, config: Config) -> None:
    contents = rule_text(config.remote_address)
    if RULE.is_symlink():
        raise RuntimeError(f"Refusing to replace a symlinked input rule: {RULE}")
    if RULE.exists() and not RULE.read_text().startswith("# Managed by fredrik.shield-remote;"):
        raise RuntimeError(f"Refusing to overwrite an unrelated rule: {RULE}")
    service_dir = paths.service.parent
    if service_dir.is_symlink():
        raise RuntimeError("User service directory is symlinked; choose a machine-local installation first")
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = paths.service
    if service_file.is_symlink():
        raise RuntimeError(f"Refusing to overwrite a symlinked user service: {service_file}")
    if service_file.exists() and not service_file.read_text().startswith("# Managed by fredrik.shield-remote"):
        raise RuntimeError(f"Refusing to overwrite an unrelated service: {service_file}")
    install_microphone(paths, config)
    (paths.config / "input-setup.json").unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=paths.runtime) as file:
        file.write(contents)
        file.flush()
        privilege = ["sudo"] if sys.stdin.isatty() else ["pkexec"]
        subprocess.run([*privilege, "/usr/bin/install", "-m", "644", file.name, str(RULE)], check=True)
    subprocess.run([*privilege, "/usr/bin/udevadm", "control", "--reload-rules"], check=True)
    service_file.write_text(service_text())
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", SERVICE], check=True)
    subprocess.run(["systemctl", "--user", "restart", SERVICE], check=True)
    write_private_json(paths.config / "input-setup.json",
                       {"schema": 1, "address": config.remote_address})
    print("Remote-only rule, router and microphone services installed. Use Reconnect remote in the plugin panel.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[
        "serve", "status", "request", "initialize", "install", "install-microphone", "checkpoint-target",
    ])
    arguments = parser.parse_args()
    paths = Paths.local()
    paths.prepare()
    config = load_config(paths)
    if arguments.command == "serve":
        asyncio.run(Router(paths, config).serve())
    elif arguments.command == "initialize":
        if not config.remote_address:
            config = dataclasses.replace(config, remote_address=asyncio.run(auto_remote(paired_only=False)))
        save_config(paths, config)
        print(f"Selected remote: {config.remote_address}\nComputer Home: home-theater")
    elif arguments.command == "install":
        install(paths, config)
    elif arguments.command == "install-microphone":
        install_microphone(paths, config)
    elif arguments.command == "checkpoint-target":
        response = client_request(paths, {"command": "status"})
        state = response.get("state")
        if (response.get("ok") is not True or not isinstance(state, dict)
                or state.get("running") is not True or state.get("target") not in ("computer", "shield")):
            raise RuntimeError("Cannot checkpoint target: a running router with a valid target is required")
        save_target(paths, state["target"])
        print(json.dumps({"ok": True, "target": state["target"], "checkpointed": True}))
    else:
        payload = {"command": "status"} if arguments.command == "status" else json.loads(sys.stdin.readline(8192))
        if not isinstance(payload, dict):
            raise ValueError("Request must be an object")
        response = client_request(paths, payload)
        print(json.dumps(response))
        return 0 if response["ok"] else 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        LOG.error("%s", error)
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
