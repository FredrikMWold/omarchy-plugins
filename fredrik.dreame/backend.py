#!/usr/bin/env python3
"""Private storage and bounded JSON IPC for the Omarchy Dreame plugin."""

import argparse
import ast
import base64
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

from cloud import (
    CloudClient, CloudError, compact, require, strict_json,
    validate_action, validate_profile, validate_properties,
)
from native import PROPERTIES, status_display
from programs import ACTIVE, ProgramService, public_program
from voice import public_voice, receive_audio


SOURCE_URL = (
    "https://raw.githubusercontent.com/Tasshack/dreame-vacuum/"
    "f5b8472b8e2e4bb3dff7bc2a81a5fd594eca9585/"
    "custom_components/dreame_vacuum/dreame/protocol.py"
)
SOURCE_SHA256 = "2dbfd3384ef3fb76ecd040279ce108cf7f355b3abdb1823c0cff37b13d2312ed"
MAX_REQUEST = 32768
DEFAULT_PROPERTIES = [{"siid": a, "piid": b} for a, b in PROPERTIES.values()]
STATE_NAMES = {
    1: "Vacuuming", 2: "Standby", 3: "Paused", 4: "Error",
    5: "Returning to charge", 6: "Charging", 7: "Mopping",
    8: "Mop drying", 9: "Mop washing", 10: "Returning to wash",
    11: "Mapping", 12: "Vacuuming and mopping", 13: "Charging completed",
    14: "Upgrading", 15: "Summoned cleaning", 16: "Station reset",
    17: "Returning to install mop", 18: "Returning to remove mop",
}


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else (
            Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "fredrik.dreame"
        )
        if self.root.is_symlink():
            raise CloudError("UNSAFE_STORAGE", "Dreame storage must not be a symlink.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise CloudError("UNSAFE_STORAGE", "Dreame storage must be an owned private directory.")
        self.root.chmod(0o700)

    @staticmethod
    def _check(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise CloudError("UNSAFE_STORAGE", "Dreame state must be an owned, mode-0600 regular file with no extra hard links.")

    def read(self, name, default=None):
        try:
            fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return default
        except OSError as error:
            raise CloudError("UNSAFE_STORAGE", "Dreame state could not be opened safely.") from error
        with os.fdopen(fd, "rb") as stream:
            self._check(os.fstat(stream.fileno()))
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CloudError("INVALID_STORAGE", "Dreame state exceeded the size limit.")
        try:
            result = strict_json(raw)
        except (ValueError, UnicodeError) as error:
            raise CloudError("INVALID_STORAGE", "Dreame state contains invalid JSON; it was not replaced.") from error
        if not isinstance(result, dict):
            raise CloudError("INVALID_STORAGE", "Dreame state must be a JSON object.")
        return result

    def write(self, name, value):
        destination = self.root / name
        if destination.exists() or destination.is_symlink():
            self._check(destination.lstat())
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(compact(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def remove(self, name):
        path = self.root / name
        if path.exists() or path.is_symlink():
            self._check(path.lstat())
            path.unlink()

    @contextmanager
    def lock(self, wait_seconds=0, name="operation.lock"):
        try:
            fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise CloudError("UNSAFE_STORAGE", "Could not safely open the Dreame operation lock.") from error
        with os.fdopen(fd, "r+") as stream:
            self._check(os.fstat(stream.fileno()))
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        raise CloudError("BUSY", "Another Dreame request or login is still running; wait before trying again.") from error
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def import_profile(store, reference=None):
    if reference is None:
        local = Path.home() / ".local/share/dreame-local/reverse-engineering/reference/protocol.py"
        if local.is_file():
            source = local.read_bytes()
        else:
            try:
                with urllib.request.urlopen(SOURCE_URL, timeout=20) as response:
                    source = response.read(2 * 1024 * 1024 + 1)
            except (OSError, urllib.error.URLError) as error:
                raise CloudError("PROFILE_DOWNLOAD_FAILED", "The pinned public protocol reference could not be downloaded with verified TLS.") from error
    else:
        source = Path(reference).read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise CloudError("PROFILE_CHECKSUM_FAILED", "Protocol reference checksum mismatch; no app profile was installed.")
    value = None
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "DREAME_STRINGS":
            value = ast.literal_eval(node.value)
            break
    if not isinstance(value, str):
        raise CloudError("PROFILE_INVALID", "The pinned reference has no expected protocol constants.")
    compressed = base64.b64decode(value, validate=True)
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)
    decoded = decoder.decompress(compressed, 65537)
    if len(decoded) > 65536 or not decoder.eof or decoder.unused_data:
        raise CloudError("PROFILE_INVALID", "Protocol constants have an invalid compressed representation.")
    values = strict_json(decoded)
    require(isinstance(values, list) and len(values) > 79, "The reference constants are incomplete.")
    profile = validate_profile({
        "version": 1, "passwordSalt": values[2], "clientKey": values[55],
        "basicAuthorization": values[4].split()[-1], "tenantId": values[5],
        "appVersion": values[65], "userAgent": values[79],
        "source": SOURCE_URL, "sourceSha256": SOURCE_SHA256,
    })
    store.write("profile.json", profile)


def validate_session(session):
    if not isinstance(session, dict) or session.get("version") != 1:
        raise CloudError("INVALID_STORAGE", "The saved Dreame session has an unsupported format.")
    for key in ("accessToken", "refreshToken"):
        if key in session:
            value = session[key]
            if not isinstance(value, str) or not 0 < len(value) <= 16384 or any(c in value for c in "\r\n\0"):
                raise CloudError("INVALID_STORAGE", "The saved Dreame session contains an invalid token.")
    for key in ("tenantId", "accountRegion", "language", "country"):
        if key in session and (
            not isinstance(session[key], str) or len(session[key]) > 256
            or any(c in session[key] for c in "\r\n\0")
        ):
            raise CloudError("INVALID_STORAGE", "The saved Dreame session contains invalid account metadata.")
    if "expiresAt" in session and (type(session["expiresAt"]) not in (int, float) or session["expiresAt"] <= 0):
        raise CloudError("INVALID_STORAGE", "The saved Dreame session has an invalid expiry.")
    return session


class Backend:
    def __init__(self, store, transport=None, now=time.time):
        self.store = store
        self.transport = transport
        self.now = now

    def settings(self):
        value = self.store.read("settings.json", {"mutationsEnabled": False, "selectedDid": ""})
        if set(value) != {"mutationsEnabled", "selectedDid"} or type(value.get("mutationsEnabled")) is not bool or not isinstance(value.get("selectedDid"), str):
            raise CloudError("INVALID_STORAGE", "Dreame plugin settings have an invalid format.")
        return value

    def state(self):
        session = self.store.read("session.json")
        if session is not None:
            validate_session(session)
        profile = self.store.read("profile.json")
        if profile is not None:
            validate_profile(profile)
        return {
            "schema": 1, "profileReady": profile is not None,
            "sessionAvailable": bool(session and session.get("refreshToken")),
            "region": session.get("region") if session else None,
            "expiresAt": session.get("expiresAt") if session else None,
            "program": public_program(self.store),
            "voice": public_voice(self.store, self.now()),
            **self.settings(),
        }

    def client(self):
        profile = self.store.read("profile.json")
        if profile is None:
            raise CloudError("SETUP_REQUIRED", "Run Dreame setup to install its pinned dependencies and app profile.")
        session = self.store.read("session.json")
        if session is None:
            raise CloudError("AUTH_REQUIRED", "Sign in from the Dreame plugin first.")
        validate_session(session)
        return CloudClient(profile, session, lambda value: self.store.write("session.json", value), self.transport, self.now)

    def login(self, region, username, password):
        profile = self.store.read("profile.json")
        if profile is None:
            raise CloudError("SETUP_REQUIRED", "Run setup before signing in.")
        candidate = {
            "version": 1, "region": region, "username": username,
            "fingerprint": secrets.token_hex(16),
        }
        client = CloudClient(profile, candidate, lambda value: self.store.write("session.json", value), self.transport, self.now)
        client.login(password)
        self.store.remove("confirmation.json")
        self.store.write("settings.json", {"mutationsEnabled": False, "selectedDid": ""})
        ProgramService(self).invalidate()
        return {"sessionSaved": True}

    def request(self, request):
        require(isinstance(request, dict) and isinstance(request.get("op"), str), "Supply a JSON request with an op.")
        op = request["op"]
        allowed = {
            "state": {"op"}, "devices": {"op"}, "refresh-session": {"op"},
            "select": {"op", "deviceId"}, "status": {"op", "deviceId"},
            "read-properties": {"op", "deviceId", "properties"},
            "enable-mutations": {"op", "enabled"},
            "prepare": {"op", "deviceId", "method", "params"},
            "confirm": {"op", "token"}, "cancel": {"op"}, "logout": {"op"},
            "start-program": {"op", "prompt", "deviceId"},
            "cancel-program": {"op", "programId"},
        }
        require(op in allowed and set(request) <= allowed[op], "Unknown operation or request fields.")
        if op == "state":
            return {}
        program = public_program(self.store)
        if program and program["phase"] in ACTIVE and op in ("prepare", "confirm", "enable-mutations"):
            raise CloudError("PROGRAM_ACTIVE", "Manual API mutations are disabled while an automatic program is active.")
        if op == "start-program":
            return ProgramService(self).start(request.get("prompt"), request.get("deviceId", self.settings()["selectedDid"]))
        if op == "cancel-program":
            return ProgramService(self).cancel(request.get("programId"))
        if op == "enable-mutations":
            require(type(request.get("enabled")) is bool, "enabled must be a boolean.")
            self.store.remove("confirmation.json")
            self.store.write("settings.json", {**self.settings(), "mutationsEnabled": request["enabled"]})
            return {}
        if op == "cancel":
            self.store.remove("confirmation.json")
            return {"cancelled": True}
        if op == "logout":
            try:
                ProgramService(self).invalidate()
            finally:
                self.store.remove("session.json")
                self.store.remove("confirmation.json")
                self.store.write("settings.json", {"mutationsEnabled": False, "selectedDid": ""})
            return {"localSessionRemoved": True, "cloudSessionRevoked": False}
        client = self.client()
        if op == "refresh-session":
            client.refresh()
            return {"renewed": True}
        if op == "devices":
            devices = client.devices()
            return {"devices": [{k: d[k] for k in ("did", "name", "model")} for d in devices]}
        if op == "confirm":
            return self.confirm(client, request.get("token"))
        did = request.get("deviceId", self.settings()["selectedDid"])
        device = client.device(did)
        if op == "select":
            self.store.remove("confirmation.json")
            self.store.write("settings.json", {**self.settings(), "selectedDid": device["did"]})
            return {"selected": device["did"]}
        if op == "prepare":
            require(self.settings()["mutationsEnabled"], "Enable advanced commands before preparing a device-changing request.")
            method, params = request.get("method"), request.get("params")
            if method == "set_properties":
                validate_properties(params, write=True)
            elif method == "action":
                validate_action(params)
            else:
                raise CloudError("INVALID_INPUT", "Only set_properties and action can be prepared.")
            confirmation = {
                "token": secrets.token_urlsafe(24), "expiresAt": self.now() + 90,
                "deviceId": device["did"], "method": method, "params": params,
            }
            self.store.write("confirmation.json", confirmation)
            return {"confirmation": {**confirmation, "deviceName": device["name"]}}
        properties = request.get("properties") if op == "read-properties" else DEFAULT_PROPERTIES
        validate_properties(properties)
        values = client.properties(device, properties)
        data = {
            "device": {key: device[key] for key in ("did", "name", "model")},
            "properties": values, "complete": all(value["code"] == 0 for value in values),
            "observedAt": self.now(),
        }
        if op == "status":
            data["display"] = status_display(values)
            state = next(value for value in values if value["siid"] == 2 and value["piid"] == 1)
            data["stateLabel"] = (
                STATE_NAMES.get(state.get("value"), f"Unknown state ({state.get('value')})")
                if state["code"] == 0 and type(state.get("value")) is int
                else "State unavailable"
            )
        return data

    def confirm(self, client, token):
        require(isinstance(token, str) and 1 <= len(token) <= 128, "A prepared confirmation token is required.")
        pending = self.store.read("confirmation.json")
        if not pending or not isinstance(pending.get("token"), str) or not secrets.compare_digest(token, pending["token"]):
            raise CloudError("CONFIRMATION_REQUIRED", "This confirmation is missing, replaced, or already consumed.")
        self.store.remove("confirmation.json")
        if not self.settings()["mutationsEnabled"]:
            raise CloudError("CONFIRMATION_REQUIRED", "Advanced commands are disabled.")
        expiry = pending.get("expiresAt")
        if type(expiry) not in (int, float) or not self.now() < expiry <= self.now() + 90:
            raise CloudError("CONFIRMATION_EXPIRED", "The preview expired; prepare and inspect a new request.")
        device = client.device(pending.get("deviceId"))
        return client.mutate(device, pending.get("method"), pending.get("params"))

    def execute(self, request):
        if request == {"op": "state"}:
            return {"ok": True, "data": {}, "state": self.state()}
        op = request.get("op") if isinstance(request, dict) else None
        wait_seconds = 30 if op in ("devices", "status", "read-properties", "refresh-session") else 0
        context = nullcontext() if op == "cancel-program" else self.store.lock(wait_seconds=wait_seconds)
        with context:
            try:
                data = self.request(request)
                return {"ok": True, "data": data, "state": self.state()}
            except CloudError as error:
                return {"ok": False, "error": {"code": error.code, "message": str(error)}, "state": self.state()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initialize", "state", "request", "login", "run-program", "audio"))
    parser.add_argument("--reference", help="Pinned public protocol source, used only by initialize")
    parser.add_argument("--program-id", help="Claim one queued cleaning program without resuming previous dispatches")
    parser.add_argument("--audio-file", help="Private 16 kHz mono PCM WAV, at most 30 seconds")
    args = parser.parse_args()
    store = Store()
    backend = Backend(store)
    if args.command == "audio":
        require(bool(args.audio_file), "Supply --audio-file with a private WAV recording.")
        response = receive_audio(backend, args.audio_file)
    elif args.command == "run-program":
        ProgramService(backend).run(args.program_id)
        response = {"ok": True, "data": {}, "state": backend.state()}
    elif args.command == "initialize":
        with store.lock():
            import_profile(store, args.reference)
        response = {"ok": True, "data": {"profileInstalled": True}, "state": backend.state()}
    elif args.command == "login":
        require(not sys.stdin.isatty(), "Use login.sh for private terminal entry, not command arguments.")
        raw = sys.stdin.buffer.read(8193)
        require(len(raw) <= 8192, "Login input exceeded the size limit.")
        fields = raw.decode().split("\0")
        require(len(fields) == 4 and fields[-1] == "", "Login input must contain region, username and password as three NUL-terminated values.")
        with store.lock():
            result = backend.login(*fields[:3])
        response = {"ok": True, "data": result, "state": backend.state()}
    elif args.command == "state":
        response = backend.execute({"op": "state"})
    else:
        raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        require(0 < len(raw) <= MAX_REQUEST and raw.endswith(b"\n"), "Provide one newline-terminated JSON request, at most 32 KiB.")
        try:
            request = strict_json(raw)
        except (ValueError, UnicodeError) as error:
            raise CloudError("INVALID_INPUT", "Request is not valid finite JSON.") from error
        response = backend.execute(request)
    print(compact(response))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CloudError as error:
        print(compact({"ok": False, "error": {"code": error.code, "message": str(error)}}))
        sys.exit(1)
    except Exception as error:
        print(f"Dreame backend failed: {type(error).__name__}", file=sys.stderr)
        print(compact({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "The backend failed; review its setup and private storage. No automatic command retry was sent."}}))
        sys.exit(1)
