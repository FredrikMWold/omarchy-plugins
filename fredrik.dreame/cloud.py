"""Dreamehome cloud protocol; no Android, Home Assistant, or target execution."""

import base64
import gzip
import hashlib
import http.client
import io
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


REGIONS = {"cn", "eu", "us", "sg", "ru", "kr"}
MAX_RESPONSE = 2 * 1024 * 1024
READ_ONLY_METHOD = "get_properties"


class CloudError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message):
    if not condition:
        raise CloudError("INVALID_INPUT", message)


def strict_json(text):
    def invalid_constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(text, parse_constant=invalid_constant)


def canonical(params, top=True):
    """Match the reference protocol's distinct top-level and nested rules."""
    parts = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, dict):
            inner = canonical(value, False)
            parts.append(f"{key}=[{inner}]" if inner else f"{key}=]")
        elif isinstance(value, list):
            if top:
                parts.append(f"{key}={compact(value, sort_keys=True)}")
        elif isinstance(value, bool):
            parts.append(f"{key}={'true' if value else 'false'}")
        elif value is None:
            parts.append(f"{key}=null")
        else:
            parts.append(f"{key}={value if top else compact(value)}")
    return "&".join(parts)


def compact(value, **kwargs):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False, **kwargs)


def md5(value):
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def property_matches(item, expected):
    value = item.get("value")
    if item["code"] != 0:
        return False
    if type(value) is type(expected) and value == expected:
        return True
    if (item["siid"], item["piid"]) != (4, 50) or not isinstance(expected, str):
        return False
    try:
        selected = strict_json(expected)
        actual = strict_json(value) if isinstance(value, str) else value
    except (ValueError, UnicodeError):
        return False
    if (
        not isinstance(selected, dict) or set(selected) != {"k", "v"}
        or not isinstance(selected["k"], str) or type(selected["v"]) is not int
        or not isinstance(actual, list)
    ):
        return False
    matches = [p for p in actual if isinstance(p, dict) and p.get("k") == selected["k"]]
    return len(matches) == 1 and type(matches[0].get("v")) is int and matches[0]["v"] == selected["v"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CloudError("HTTP_REDIRECT", "Dreame redirected a request; credentials were not forwarded.")


class HTTPTransport:
    def __init__(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), NoRedirect()
        )

    def __call__(self, url, headers, body):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise CloudError(
                "NETWORK_ERROR",
                "Dreame could not be reached with verified TLS. No automatic command retry was sent.",
            ) from error
        try:
            with response:
                status = response.code
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise CloudError("INVALID_RESPONSE", "Dreame response exceeded the size limit.")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                        raw = stream.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise CloudError("INVALID_RESPONSE", "Decompressed Dreame response exceeded the size limit.")
                try:
                    payload = strict_json(raw)
                except (ValueError, UnicodeError) as error:
                    raise CloudError(
                        "INVALID_RESPONSE", f"Dreame returned non-JSON data (HTTP {status})."
                    ) from error
        except (TimeoutError, OSError, EOFError, http.client.HTTPException) as error:
            raise CloudError("NETWORK_ERROR", "Dreame response was interrupted; no automatic command retry was sent.") from error
        if not isinstance(payload, dict):
            raise CloudError("INVALID_RESPONSE", "Dreame returned an unexpected response shape.")
        return status, payload


def validate_profile(profile):
    require(isinstance(profile, dict) and profile.get("version") == 1, "A version-1 app profile is required.")
    for key in ("passwordSalt", "clientKey", "basicAuthorization", "tenantId", "appVersion", "userAgent"):
        require(isinstance(profile.get(key), str) and 0 < len(profile[key]) <= 1024, f"Invalid app profile field: {key}.")
        require(not any(c in profile[key] for c in "\r\n\0"), f"Invalid app profile field: {key}.")
    require(len(profile["clientKey"].encode()) in (16, 24, 32), "The app profile has an invalid AES key length.")
    return profile


def validate_properties(properties, write=False):
    require(isinstance(properties, list) and 1 <= len(properties) <= 32, "Supply 1-32 property objects.")
    seen = set()
    for item in properties:
        required = {"siid", "piid", "value"} if write else {"siid", "piid"}
        require(isinstance(item, dict) and set(item) == required, "Property fields must be siid/piid, plus value for writes.")
        for key in ("siid", "piid"):
            require(type(item[key]) is int and 1 <= item[key] <= 65535, f"{key} must be an integer from 1 to 65535.")
        pair = (item["siid"], item["piid"])
        require(pair not in seen, "Duplicate properties are not allowed.")
        seen.add(pair)
        if write:
            require(item["value"] is None or type(item["value"]) in (bool, int, float, str), "Property values must be JSON scalars.")
            require(len(compact(item["value"]).encode()) <= 8192, "Property value is too large.")
    return properties


def validate_action(action):
    require(isinstance(action, dict) and set(action) == {"siid", "aiid", "in"}, "An action needs exactly siid, aiid and in.")
    for key in ("siid", "aiid"):
        require(type(action[key]) is int and 1 <= action[key] <= 65535, f"{key} must be an integer from 1 to 65535.")
    require(isinstance(action["in"], list) and len(compact(action).encode()) <= 16384, "Action input must be a bounded JSON array.")
    return action


class CloudClient:
    def __init__(self, profile, session, persist, transport=None, now=time.time):
        self.profile = validate_profile(profile)
        self.session = dict(session)
        self.persist = persist
        self.transport = transport if transport is not None else HTTPTransport()
        self.now = now
        require(self.session.get("region") in REGIONS, "Select a supported Dreame account region.")
        require(isinstance(self.session.get("username"), str) and 0 < len(self.session["username"]) <= 320, "A Dreame username is required.")
        fingerprint = self.session.get("fingerprint")
        require(isinstance(fingerprint, str) and re.fullmatch("[a-f0-9]{32}", fingerprint), "Invalid session fingerprint.")

    @property
    def base_url(self):
        return f"https://{self.session['region']}.iot.dreame.tech:13267"

    def headers(self, content_type, authenticated=False):
        p, s = self.profile, self.session
        headers = {
            "User-Agent": p["userAgent"],
            "dreame-meta": (
                f"cv=a_{p['appVersion']};canvasHash={md5(s['username'] + 'c')[:8]}"
                f";webglHash={md5(s['username'] + 'w')[:8]};visitorIdHash={s['fingerprint']}"
            ),
            "tenant-id": s.get("tenantId") or p["tenantId"],
            "Authorization": "Basic " + p["basicAuthorization"],
            "Content-Type": content_type,
            "Accept-Encoding": "gzip",
        }
        if all(s.get(k) for k in ("accountRegion", "language", "country")):
            value = f"{s['accountRegion']}|{s['language']}|{s['country']}".encode()
            padder = PKCS7(128).padder()
            padded = padder.update(value) + padder.finalize()
            cipher = Cipher(algorithms.AES(p["clientKey"].encode()), modes.ECB()).encryptor()
            headers["dreame-rlc"] = base64.b64encode(cipher.update(padded) + cipher.finalize()).decode()
        if authenticated:
            headers["dreame-auth"] = s["accessToken"]
        return headers

    def _token(self, form):
        status, data = self.transport(
            self.base_url + "/dreame-auth/oauth/token",
            self.headers("application/x-www-form-urlencoded"),
            urllib.parse.urlencode(form).encode(),
        )
        if status in (400, 401, 403):
            raise CloudError("AUTH_REJECTED", "Dreame rejected authentication. Check account region/password or sign in again; an interactive challenge may be required.")
        if status != 200:
            raise CloudError("CLOUD_HTTP_ERROR", f"Dreame authentication returned HTTP {status}; no automatic retry was sent.")
        for key in ("access_token",):
            if not isinstance(data.get(key), str) or not data[key] or len(data[key]) > 16384 or any(c in data[key] for c in "\r\n\0"):
                raise CloudError("AUTH_REJECTED", "Dreame did not return an access token; an interactive challenge or a different account login method may be required.")
        refresh = data.get("refresh_token")
        if refresh is None and form["grant_type"] == "refresh_token":
            refresh = self.session.get("refreshToken")
        if not isinstance(refresh, str) or not refresh or len(refresh) > 16384 or any(c in refresh for c in "\r\n\0"):
            raise CloudError("INVALID_RESPONSE", "Dreame did not return a renewable session.")
        expires = data.get("expires_in")
        if type(expires) is not int or not 0 < expires <= 366 * 24 * 3600:
            raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid token lifetime.")
        next_session = {
            **self.session,
            "accessToken": data["access_token"],
            "refreshToken": refresh,
            "expiresAt": self.now() + expires,
        }
        for source, target in (
            ("tenant_id", "tenantId"), ("region", "accountRegion"),
            ("lang", "language"), ("country", "country"),
        ):
            if source in data:
                value = data[source]
                if not isinstance(value, str) or len(value) > 256 or any(c in value for c in "\r\n\0"):
                    raise CloudError("INVALID_RESPONSE", "Dreame returned invalid account metadata.")
                next_session[target] = value
        self.persist(next_session)
        self.session = next_session

    def login(self, password):
        require(isinstance(password, str) and 0 < len(password) <= 4096, "A nonempty password is required.")
        form = {
            "grant_type": "password", "scope": "all", "platform": "ANDROID",
            "type": "account", "username": self.session["username"],
            "password": md5(password + self.profile["passwordSalt"]),
        }
        self._token(form)

    def refresh(self):
        if not self.session.get("refreshToken"):
            raise CloudError("AUTH_REQUIRED", "Sign in from the Dreame plugin first.")
        try:
            self._token({
                "grant_type": "refresh_token", "scope": "all", "platform": "ANDROID",
                "type": "account", "refresh_token": self.session["refreshToken"],
            })
        except CloudError as error:
            if error.code == "AUTH_REJECTED":
                self.session = {key: value for key, value in self.session.items() if key not in ("accessToken", "refreshToken", "expiresAt")}
                self.persist(self.session)
            raise

    def api(self, path, params=None, read_only=True):
        if not self.session.get("accessToken"):
            raise CloudError("AUTH_REQUIRED", "Sign in from the Dreame plugin first.")
        if self.session.get("expiresAt", 0) <= self.now() + 60:
            self.refresh()
        for attempt in range(2 if read_only else 1):
            body = None
            if params is not None:
                timestamp = int(self.now() * 1000)
                signature = md5(canonical(params) + str(timestamp) + self.profile["clientKey"])
                body = compact({**params, "sign": signature, "timestamp": timestamp}).encode()
            status, data = self.transport(
                self.base_url + path, self.headers("application/json", authenticated=True), body
            )
            if status == 401 or data.get("code") == 401:
                if read_only and attempt == 0:
                    self.refresh()
                    continue
                raise CloudError("AUTH_REJECTED", "Dreame rejected the session. No device-changing command was replayed.")
            if status != 200:
                raise CloudError("CLOUD_HTTP_ERROR", f"Dreame returned HTTP {status}; no automatic command retry was sent.")
            if type(data.get("code")) is not int or data["code"] != 0 or data.get("success") is False:
                raise CloudError("CLOUD_REJECTED", "Dreame rejected the API request. Inspect account compatibility before retrying.")
            if "data" not in data or data["data"] is None:
                raise CloudError("INVALID_RESPONSE", "Dreame returned no API result.")
            return data["data"]
        raise CloudError("AUTH_REJECTED", "Dreame session renewal did not authorize the read.")

    def devices(self):
        data = self.api("/dreame-user-iot/iotuserbind/device/listV2")
        page = data.get("page") if isinstance(data, dict) else None
        records = page.get("records") if isinstance(page, dict) else None
        if not isinstance(records, list):
            raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid device list.")
        total = page.get("total", len(records))
        if isinstance(total, str) and re.fullmatch(r"[0-9]{1,20}", total):
            total = int(total)
        if type(total) is not int or total < len(records):
            raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid device count.")
        if total > len(records):
            raise CloudError("PAGINATION_UNSUPPORTED", "Dreame returned a partial device page; this client will not silently omit devices.")
        devices = []
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get("model"), str):
                raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid device record.")
            if ".vacuum." not in item["model"]:
                continue
            raw_did = item.get("did")
            if type(raw_did) not in (str, int):
                raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid device identifier.")
            did = str(raw_did)
            if not re.fullmatch("[A-Za-z0-9_-]{1,80}", did):
                raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid device identifier.")
            info = item.get("deviceInfo")
            name = item.get("customName") or (info.get("displayName") if isinstance(info, dict) else None) or item["model"]
            binding = item.get("bindDomain", "")
            if not isinstance(name, str) or not isinstance(binding, str):
                raise CloudError("INVALID_RESPONSE", "Dreame returned invalid device metadata.")
            devices.append({"did": did, "name": name, "model": item["model"], "binding": binding})
        return devices

    def device(self, did):
        require(isinstance(did, str) and re.fullmatch("[A-Za-z0-9_-]{1,80}", did), "Select a valid bound device.")
        matches = [item for item in self.devices() if item["did"] == did]
        if len(matches) != 1:
            raise CloudError("DEVICE_NOT_FOUND", "The selected vacuum is not uniquely bound to this account.")
        return matches[0]

    def command(self, device, method, params, read_only):
        binding = device["binding"]
        if binding and not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::[0-9]{1,5})?", binding):
            raise CloudError("INVALID_RESPONSE", "Dreame returned an unsafe command binding.")
        shard = binding.split(".", 1)[0] if binding else ""
        if shard and not re.fullmatch("[A-Za-z0-9-]{1,64}", shard):
            raise CloudError("INVALID_RESPONSE", "Dreame returned an unsafe command binding.")
        route = "/dreame-iot-com" + ("-" + shard if shard else "") + "/device/sendCommand"
        request_id = secrets.randbelow(500000) + 100
        did = device["did"]
        data = self.api(route, {
            "did": did, "id": request_id,
            "data": {"did": did, "id": request_id, "method": method, "params": params},
        }, read_only=read_only)
        if not isinstance(data, dict) or "result" not in data or data["result"] is None:
            raise CloudError("INVALID_RESPONSE", "Dreame returned no device-command result; no command was replayed.")
        return data["result"]

    def properties(self, device, properties):
        validate_properties(properties)
        requested = {(item["siid"], item["piid"]) for item in properties}
        result = self.command(device, READ_ONLY_METHOD, [
            {"did": device["did"], **item} for item in properties
        ], True)
        if not isinstance(result, list) or len(result) != len(requested):
            raise CloudError("INVALID_RESPONSE", "The vacuum returned an incomplete property result.")
        observed = set()
        for item in result:
            if not isinstance(item, dict) or type(item.get("code")) is not int:
                raise CloudError("INVALID_RESPONSE", "The vacuum returned an invalid property result.")
            if type(item.get("siid")) is not int or type(item.get("piid")) is not int:
                raise CloudError("INVALID_RESPONSE", "The vacuum returned invalid property identifiers.")
            pair = (item.get("siid"), item.get("piid"))
            if pair not in requested or pair in observed or (item["code"] == 0 and "value" not in item):
                raise CloudError("INVALID_RESPONSE", "The vacuum returned unexpected property identifiers or values.")
            observed.add(pair)
        return [{key: item[key] for key in ("siid", "piid", "code", "value") if key in item} for item in result]

    def require_idle(self, device):
        state = self.properties(device, [{"siid": 2, "piid": 1}])[0]
        if state["code"] != 0 or type(state.get("value")) is not int or state["value"] not in (2, 6, 13):
            raise CloudError("NOT_IDLE", "The vacuum must report standby, charging, or charging completed before an advanced command.")

    def mutate(self, device, method, params):
        if method == "set_properties":
            validate_properties(params, write=True)
        elif method == "action":
            validate_action(params)
        else:
            raise CloudError("INVALID_INPUT", "Only set_properties and action support confirmation.")
        self.require_idle(device)
        wire_params = (
            [{"did": device["did"], **item} for item in params]
            if method == "set_properties" else {"did": device["did"], **params}
        )
        result = self.command(device, method, wire_params, False)
        if method == "set_properties":
            if not isinstance(result, list) or len(result) != len(params):
                raise CloudError("DEVICE_REJECTED", "The write acknowledgement was incomplete. No retry or rollback was sent.")
            if any(not isinstance(item, dict) or type(item.get("siid")) is not int or type(item.get("piid")) is not int for item in result):
                raise CloudError("DEVICE_REJECTED", "The write acknowledgement contained invalid property identifiers. Inspect live state before retrying.")
            pairs = [(item.get("siid"), item.get("piid")) for item in result if isinstance(item, dict)]
            if len(pairs) != len(params) or len(set(pairs)) != len(params) or set(pairs) != {(item["siid"], item["piid"]) for item in params}:
                raise CloudError("DEVICE_REJECTED", "The write acknowledgement identified unexpected properties. Inspect live state before retrying.")
        elif not isinstance(result, dict):
            raise CloudError("DEVICE_REJECTED", "The action acknowledgement had an unexpected shape. Its physical outcome is unverified.")
        entries = result if isinstance(result, list) else [result]
        if not entries or any(not isinstance(item, dict) or type(item.get("code")) is not int or item["code"] != 0 for item in entries):
            raise CloudError("DEVICE_REJECTED", "The vacuum did not accept every command item. No retry or rollback was sent; inspect live state.")
        if method == "set_properties":
            expected = {(item["siid"], item["piid"]): item["value"] for item in params}
            actual = self.properties(device, [{"siid": a, "piid": b} for a, b in expected])
            if any(not property_matches(item, expected[(item["siid"], item["piid"])]) for item in actual):
                raise CloudError("READBACK_MISMATCH", "The write response was accepted but readback did not match. No retry or rollback was sent.")
            return {"accepted": True, "readback": actual}
        return {"accepted": True, "result": result, "physicalOutcomeVerified": False}
