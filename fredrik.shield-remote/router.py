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
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth
from evdev import InputDevice


LOG = logging.getLogger("shield-remote")
PLUGIN = Path(__file__).resolve().parent
SERVICE = "omarchy-shield-remote.service"
RULE = Path("/etc/udev/rules.d/71-fredrik-shield-remote.rules")
REMOTE_NAME = "NVIDIA SHIELD Remote"
ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
NAVIGATION = {103: "Up", 105: "Left", 106: "Right", 108: "Down", 353: "Return", 158: "Escape"}
SHIELD_KEYS = {
    103: "DPAD_UP", 105: "DPAD_LEFT", 106: "DPAD_RIGHT", 108: "DPAD_DOWN",
    353: "DPAD_CENTER", 158: "BACK", 172: "HOME", 164: "MEDIA_PLAY_PAUSE",
    168: "MEDIA_REWIND", 208: "MEDIA_FAST_FORWARD", 114: "VOLUME_DOWN", 115: "VOLUME_UP",
}
IGNORED = {116, 217, 393}
REPEAT = {103, 105, 106, 108, 114, 115}
NETWORK_ERRORS = (CannotConnect, ConnectionClosed, InvalidAuth, OSError, TimeoutError)


@dataclasses.dataclass(frozen=True)
class Config:
    remote_address: str = ""
    shield_host: str = ""
    paired_host: str = ""

    def validate(self) -> Config:
        if self.remote_address and not ADDRESS.fullmatch(self.remote_address):
            raise ValueError("Remote address must be a Bluetooth address")
        for host in (self.shield_host, self.paired_host):
            validate_host(host)
        return dataclasses.replace(self, remote_address=self.remote_address.upper())


def validate_host(host: str) -> None:
    if not isinstance(host, str):
        raise ValueError("Shield host must be text")
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("Enter a Shield IP address or hostname, without a URL or port") from None


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


def load_config(paths: Paths) -> Config:
    file = paths.config / "config.json"
    if not file.exists():
        return Config()
    payload = json.loads(file.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Unsupported Shield Remote configuration")
    schema = payload.pop("schema", None)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError("Unsupported Shield Remote configuration")
    if schema == 1 and payload.pop("home_app", None) not in ("demo", "stremio"):
        raise ValueError("Invalid legacy Computer Home setting")
    if set(payload) != {field.name for field in dataclasses.fields(Config)}:
        raise ValueError("Invalid Shield Remote configuration fields")
    if not all(isinstance(value, str) for value in payload.values()):
        raise ValueError("Shield Remote configuration values must be text")
    config = Config(**payload).validate()
    if schema == 1:
        LOG.info("Migrating configuration: Computer Home now always opens Stremio")
        save_config(paths, config)
    return config


def save_config(paths: Paths, config: Config) -> None:
    paths.prepare()
    payload = {"schema": 2, **dataclasses.asdict(config.validate())}
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
    async def window(self) -> dict | None:
        clients = json.loads(await run(["hyprctl", "clients", "-j"]))
        if not isinstance(clients, list):
            raise RuntimeError("Invalid Hyprland window response")
        matches = [
            item for item in clients
            if item.get("class", "").casefold() in {"stremio", "com.stremio.stremio"}
        ]
        if len(matches) > 1:
            raise RuntimeError("Multiple Stremio windows exist; close duplicates first")
        return matches[0] if matches else None

    async def home(self) -> None:
        window = await self.window()
        if not window:
            executable = shutil.which("stremio")
            if executable:
                command = shlex.join([executable])
            elif (flatpak := shutil.which("flatpak")) and "com.stremio.Stremio" in (
                await run([flatpak, "list", "--app", "--columns=application"])
            ).splitlines():
                command = shlex.join([flatpak, "run", "com.stremio.Stremio"])
            else:
                raise RuntimeError("Stremio is not installed; install it on this Omarchy computer before using Home")
            await dispatch(f"hl.dsp.exec_cmd({json.dumps(command)})")
            deadline = time.monotonic() + 12
            while not window and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                window = await self.window()
            if not window:
                raise RuntimeError("Stremio did not open with a recognized window class")
        await dispatch(f"hl.dsp.focus({{window={json.dumps('address:' + window['address'])}}})")

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

    async def key(self, code: int) -> None:
        if code in NAVIGATION:
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


def setup_complete(paths: Paths, address: str) -> bool:
    enabled = paths.service.parent / "graphical-session.target.wants" / SERVICE
    return (setup_ready(paths, address) and not paths.service.is_symlink()
            and paths.service.is_file() and paths.service.read_text() == service_text()
            and enabled.is_symlink() and enabled.resolve() == paths.service.resolve())


class Router:
    def __init__(self, paths: Paths, config: Config) -> None:
        self.paths = paths
        self.config = config
        self.computer = Computer()
        self.target = "computer"
        self.client: AndroidTVRemote | None = None
        self.pairing_client: AndroidTVRemote | None = None
        self.pairing_deadline = 0.0
        self.shield_connected = False
        self.auth_invalid = False
        self.input_connected = False
        self.input_captured = False
        self.input_error = ""
        self.remote_pairing = False
        self.remote_pairing_stage = ""
        self.shield_error = ""
        self.last_error = ""
        self.last_action = ""
        self.last_key = ""
        self.last_notice = ""
        self.last_menu: float | None = None
        self.pressed: set[int] = set()
        self.shield_pressed: dict[int, str] = {}
        self.routing_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.input_changed = asyncio.Event()

    @property
    def paired(self) -> bool:
        return bool(self.config.shield_host and self.config.paired_host == self.config.shield_host
                    and not self.auth_invalid)

    def state(self, running: bool = True) -> dict:
        return {
            "schema": 1, "running": running, "target": self.target,
            "remote_address": self.config.remote_address,
            "setup_complete": setup_complete(self.paths, self.config.remote_address),
            "input": {"connected": self.input_connected, "captured": self.input_captured,
                      "setup": setup_ready(self.paths, self.config.remote_address), "error": self.input_error,
                      "pairing": self.remote_pairing, "pairing_stage": self.remote_pairing_stage},
            "shield": {"host": self.config.shield_host, "paired": self.paired,
                       "connected": self.shield_connected, "pairing": self.pairing_client is not None,
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

    def new_client(self) -> AndroidTVRemote:
        return AndroidTVRemote(
            "Omarchy Shield Remote", str(self.paths.config / "client.crt"),
            str(self.paths.config / "client.key"), self.config.shield_host, enable_ime=False,
        )

    async def release_shield(self) -> None:
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
        await self.release_shield()
        self.target = target
        self.last_action = f"Target: {target}"
        await self.notice("Target: Shield TV" if target == "shield" else "Target: Computer")

    async def key(self, code: int, value: int, timestamp: float) -> None:
        async with self.routing_lock:
            self.last_key = f"{code}:{value}"
            if value == 0:
                self.pressed.discard(code)
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
            if code in IGNORED:
                self.last_action = f"Ignored key {code}"
                return
            if code not in SHIELD_KEYS:
                raise ValueError(f"Unexpected remote key {code}; it was not forwarded")
            if self.target == "shield":
                if value == 2:
                    return
                if not self.shield_connected or not self.client:
                    raise RuntimeError("Shield is disconnected; this key was dropped, not sent to the computer")
                key = SHIELD_KEYS[code]
                self.client.send_key_command(key, "START_LONG")
                self.shield_pressed[code] = key
                self.last_action = f"Shield: {key}"
            elif value == 1 or code in REPEAT:
                await self.computer.key(code)
                self.last_action = f"Computer: key {code}"
            self.last_error = ""
            self.last_notice = ""

    def disconnect_control(self) -> None:
        if self.client:
            self.client.disconnect()
        self.client = None
        self.shield_connected = False
        self.shield_pressed.clear()

    async def configure(self, values: dict) -> None:
        if not isinstance(values, dict) or set(values) - {"remote_address", "shield_host"}:
            raise ValueError("Invalid configuration update")
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError("Configuration values must be text")
        if values.get("remote_address") == "auto":
            values = {**values, "remote_address": await auto_remote()}
        config = dataclasses.replace(self.config, **values).validate()
        async with self.routing_lock:
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
                self.input_connected = self.input_captured = True
                self.input_error = ""
                async def read_events() -> None:
                    async for event in device.async_read_loop():
                        if event.type == 1:
                            try:
                                await self.key(event.code, event.value, event.timestamp())
                            except (ValueError, RuntimeError, ConnectionClosed, OSError) as error:
                                await self.error(str(error))

                workers = [
                    asyncio.create_task(read_events()),
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
                self.pressed.clear()
                self.last_menu = None
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
    print("Remote-only rule and user service installed. Use Reconnect remote in the plugin panel.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "status", "request", "initialize", "install"])
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
        print(f"Selected remote: {config.remote_address}\nComputer Home: Stremio")
    elif arguments.command == "install":
        install(paths, config)
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
