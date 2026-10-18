"""X50 cloud bindings, based on the pinned MIT dreame-vacuum reference."""

import base64
import hashlib
import urllib.error
import urllib.parse
import urllib.request
import zlib

from cloud import CloudError, HTTPTransport, MAX_RESPONSE, compact, property_matches, require, strict_json


MODEL = "dreame.vacuum.r2532h"
PROPERTIES = {
    "state": (2, 1), "error": (2, 2), "battery": (3, 1), "charging": (3, 2),
    "area": (4, 2), "minutes": (4, 3), "suction": (4, 4), "water": (4, 5),
    "task": (4, 7), "mode": (4, 23), "custom": (4, 26), "cancelled": (4, 30),
    "switches": (4, 50), "wetness": (28, 1),
}
SUCTION = {"quiet": 0, "standard": 1, "intense": 2, "max": 3}
ROUTES = {"standard": 1, "intensive": 2, "deep": 3, "quick": 4}
METHODS = {"vacuum": 2, "mop": 1, "mop-after-vac": 3}
ROOM_TYPES = {
    1: "Living Room", 2: "Master Bedroom", 3: "Study", 4: "Kitchen",
    5: "Dining Hall", 6: "Bathroom", 7: "Balcony", 8: "Corridor",
    9: "Utility Room", 10: "Closet", 11: "Meeting Room", 12: "Office",
    13: "Fitness Area", 14: "Recreation Area", 15: "Secondary Bedroom",
}
CHARGING = {1: "Charging", 2: "Not charging", 3: "Fully charged", 5: "Returning to charge"}
METHOD_LABELS = {0: "Vacuum and mop", 1: "Mop", 2: "Vacuum", 3: "Mop after vacuum"}


def switch_values(value):
    try:
        values = strict_json(value) if isinstance(value, str) else value
    except (ValueError, UnicodeError) as error:
        raise CloudError("INVALID_RESPONSE", "The robot returned invalid cleaning switches.") from error
    if not isinstance(values, list):
        raise CloudError("INVALID_RESPONSE", "The robot returned invalid cleaning switches.")
    result = {}
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("k"), str) or type(item.get("v")) is not int:
            raise CloudError("INVALID_RESPONSE", "The robot returned invalid cleaning switches.")
        if item["k"] in result:
            raise CloudError("INVALID_RESPONSE", "The robot returned duplicate cleaning switches.")
        result[item["k"]] = item["v"]
    return result


def status_display(values):
    by_id = {(p["siid"], p["piid"]): p for p in values}
    fields = []
    for name, label in (
        ("battery", "Battery"), ("charging", "Dock / charging"), ("mode", "Cleaning method"),
        ("suction", "Suction"), ("wetness", "Mop wetness"), ("area", "Last / current area"),
        ("minutes", "Last / current duration"), ("error", "Robot fault"),
    ):
        prop = by_id.get(PROPERTIES[name])
        text, available = "Unavailable", False
        value = prop.get("value") if prop else None
        if prop and prop["code"] == 0 and type(value) is int:
            available = True
            if name == "battery" and 0 <= value <= 100:
                text = f"{value}%"
            elif name == "charging":
                text = CHARGING.get(value, f"Unknown ({value})")
            elif name == "mode":
                text = METHOD_LABELS[value & 3] if value >= 0 else f"Unknown ({value})"
            elif name == "suction":
                text = {0: "Quiet", 1: "Standard", 2: "Intense", 3: "Max"}.get(value, f"Unknown ({value})")
            elif name == "wetness" and 1 <= value <= 32:
                text = f"{value} / 32"
            elif name == "area" and value >= 0:
                text = f"{value} m2"
            elif name == "minutes" and value >= 0:
                text = f"{value} min"
            elif name == "error":
                text = "None" if value == 0 else f"Fault {value}"
            else:
                available = False
        elif prop and prop["code"] != 0:
            text = f"Unsupported (code {prop['code']})"
        fields.append({"key": name, "label": label, "text": text, "available": available})
    return fields


def read_snapshot(client, device):
    if device["model"] != MODEL:
        raise CloudError("MODEL_UNSUPPORTED", "Automatic programs are currently mapped only for this X50 model.")
    values = client.properties(device, [{"siid": a, "piid": b} for a, b in PROPERTIES.values()])
    indexed = {(p["siid"], p["piid"]): p for p in values}
    result = {}
    for name, pair in PROPERTIES.items():
        prop = indexed[pair]
        if prop["code"] != 0:
            raise CloudError("CAPABILITY_UNAVAILABLE", f"The robot does not expose required program setting {name}.")
        value = prop.get("value")
        if name == "switches":
            result[name] = switch_values(value)
        elif type(value) is not int:
            raise CloudError("INVALID_RESPONSE", f"The robot returned an invalid {name} setting.")
        else:
            result[name] = value
    if not 0 <= result["battery"] <= 100 or not 0 <= result["mode"] <= 0xFFFFFF:
        raise CloudError("INVALID_RESPONSE", "The robot returned invalid battery or packed cleaning settings.")
    return result


def require_idle(snapshot):
    if snapshot["state"] not in (2, 6, 13) or snapshot["task"] != 0 or snapshot["error"] != 0:
        raise CloudError("NOT_IDLE", "The robot must be idle, fault-free and have no active task before a program starts.")


def download_map(client, device, object_name):
    require(isinstance(object_name, str) and 1 <= len(object_name) <= 2048, "Invalid map object reference.")
    url = client.api("/dreame-user-iot/iotfile/getDownloadUrl", {
        "did": device["did"], "model": device["model"], "filename": object_name,
        "region": client.session["region"],
    })
    if not isinstance(url, str):
        raise CloudError("INVALID_RESPONSE", "Dreame returned no map download URL.")
    try:
        parsed = urllib.parse.urlsplit(url)
        safe = (
            parsed.scheme == "https"
            and parsed.hostname == f"dreame-{client.session['region']}.iot.dreame.tech"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and not parsed.fragment
        )
    except ValueError as error:
        raise CloudError("INVALID_RESPONSE", "Dreame returned an invalid map download URL.") from error
    if not safe:
        raise CloudError("MAP_HOST_UNSUPPORTED", "The map URL is outside the verified Dreame download host.")
    try:
        with HTTPTransport().opener.open(urllib.request.Request(url), timeout=15) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CloudError("MAP_DOWNLOAD_FAILED", "The map could not be downloaded with verified TLS.") from error
    if len(raw) > MAX_RESPONSE:
        raise CloudError("INVALID_RESPONSE", "The map list exceeded the size limit.")
    try:
        result = strict_json(raw)
    except (ValueError, UnicodeError) as error:
        raise CloudError("INVALID_RESPONSE", "Dreame returned invalid map-list JSON.") from error
    return result


def decode_rooms(info):
    if not isinstance(info, dict) or not isinstance(info.get("mapstr"), list):
        raise CloudError("MAP_UNAVAILABLE", "Dreame returned no saved-map catalogue.")
    current = info.get("curr_id")
    if type(current) is not int or not 0 <= current <= 65535:
        raise CloudError("MAP_UNAVAILABLE", "The active saved-map identifier is invalid.")
    selected = None
    for entry in info["mapstr"]:
        if not isinstance(entry, dict):
            raise CloudError("MAP_FORMAT_UNSUPPORTED", "The saved-map catalogue contains an invalid entry.")
        blob = decode_map_blob(entry.get("map"))
        if int.from_bytes(blob[:2], "little") == current:
            if selected is not None:
                raise CloudError("MAP_UNAVAILABLE", "The active saved map is not uniquely identified by its binary header.")
            selected = blob
    if selected is None:
        raise CloudError("MAP_UNAVAILABLE", "The active saved map is not uniquely identified by its binary header.")
    blob = selected
    try:
        width, height = int.from_bytes(blob[19:21], "little"), int.from_bytes(blob[21:23], "little")
        end = 27 + width * height
        if not width or not height or end >= len(blob):
            raise ValueError("Invalid map dimensions")
        metadata = strict_json(blob[end:])
        segments = metadata.get("seg_inf") if isinstance(metadata, dict) else None
        if not isinstance(segments, dict) or not segments:
            raise ValueError("Missing segment metadata")
        rooms = {}
        for key, value in segments.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or len(key) > 3 or not isinstance(value, dict):
                raise ValueError("Invalid segment")
            segment = int(key)
            if not 1 <= segment <= 255 or str(segment) != key:
                raise ValueError("Invalid segment identifier")
            room_type = value.get("type")
            if room_type is not None and type(room_type) is not int:
                raise ValueError("Invalid room type")
            if value.get("name"):
                name = base64.b64decode(value["name"], validate=True).decode("utf-8")
            else:
                name = ROOM_TYPES.get(room_type, f"Room {segment}")
                index = value.get("index", 0)
                if type(index) is not int or index < 0:
                    raise ValueError("Invalid room index")
                if index and room_type in ROOM_TYPES:
                    name += f" {index + 1}"
            if not name.strip() or len(name) > 120 or name in rooms:
                raise ValueError("Ambiguous room names")
            rooms[name] = segment
    except (ValueError, UnicodeError, TypeError) as error:
        raise CloudError("MAP_FORMAT_UNSUPPORTED", "The active map's room metadata could not be validated.") from error
    identity = {"mapId": str(current), "rooms": rooms}
    return {**identity, "signature": hashlib.sha256(compact(identity, sort_keys=True).encode()).hexdigest()}


def decode_map_blob(encoded):
    if not isinstance(encoded, str) or "," in encoded:
        raise CloudError("MAP_FORMAT_UNSUPPORTED", "The active map is encrypted or uses an unmapped format.")
    try:
        compressed = base64.b64decode(encoded, altchars=b"-_", validate=True)
        decoder = zlib.decompressobj()
        blob = decoder.decompress(compressed, 8 * 1024 * 1024 + 1)
        if len(blob) > 8 * 1024 * 1024 or not decoder.eof or decoder.unused_data or len(blob) < 27:
            raise ValueError("Invalid map size")
    except (ValueError, UnicodeError, TypeError, zlib.error) as error:
        raise CloudError("MAP_FORMAT_UNSUPPORTED", "The saved map could not be decompressed safely.") from error
    return blob


def read_rooms(client, device):
    prop = client.properties(device, [{"siid": 6, "piid": 8}])[0]
    try:
        reference = strict_json(prop["value"]) if prop["code"] == 0 else None
    except (ValueError, UnicodeError, TypeError) as error:
        raise CloudError("MAP_UNAVAILABLE", "The robot returned an invalid map reference.") from error
    if not isinstance(reference, dict) or not isinstance(reference.get("object_name"), str):
        raise CloudError("MAP_UNAVAILABLE", "The robot returned no saved-map reference.")
    return decode_rooms(download_map(client, device, reference["object_name"]))


def compile_steps(packet, rooms, snapshot):
    jobs = packet["jobs"]
    require(len(jobs) <= 32, "A program supports at most 32 jobs.")
    for key in ("SmartHost", "CleanRoute", "BackWashType", "SuctionMax"):
        if key not in snapshot["switches"]:
            raise CloudError("CAPABILITY_UNAVAILABLE", f"Required native switch {key} is unavailable.")
    steps = []
    for index, job in enumerate(jobs):
        settings_for(job, snapshot)
        targets = job["target"].get("rooms", [None])
        for name in targets:
            if name is not None and name not in rooms["rooms"]:
                raise CloudError("ROOM_UNMAPPED", f"The active map has no unique room named {name}. No job was started.")
            steps.append({
                "jobIndex": index, "job": job, "room": name,
                "segment": rooms["rooms"][name] if name is not None else None,
            })
    require(len(steps) <= 64, "A program supports at most 64 ordered room/whole-home tasks.")
    return steps


def settings_for(job, snapshot):
    if job.get("maxSuctionPower") and snapshot["battery"] != 100:
        raise CloudError("BOOST_REQUIRES_CHARGE", "One-shot maximum suction requires a fully charged battery.")

    def prop(name, value):
        a, b = PROPERTIES[name]
        return {"siid": a, "piid": b, "value": value}

    def switch(key, value):
        return prop("switches", compact({"k": key, "v": value}))

    mode = (snapshot["mode"] & ~3) | METHODS[job["method"]]
    settings = [switch("SmartHost", 0), prop("custom", 0), prop("mode", mode)]
    if "wetness" in job:
        settings.append(prop("wetness", job["wetness"]))
        washing = job["washing"]
        settings.append(switch("BackWashType", {"area": 1, "time": 2, "room": 3}[washing["by"]]))
        mode = (mode & ~(255 << 8)) | (washing.get("value", 0) << 8)
        settings.append(prop("mode", mode))
    if "suction" in job:
        settings.append(prop("suction", SUCTION[job["suction"]]))
        settings.append(switch("SuctionMax", int(job["maxSuctionPower"])))
    settings.append(switch("CleanRoute", ROUTES[job["route"]]))
    return settings


def verify_settings(settings, snapshot, *, active=False):
    expected = {}
    for prop in settings:
        pair = prop["siid"], prop["piid"]
        switch = strict_json(prop["value"]) if pair == PROPERTIES["switches"] else None
        expected[pair + (switch["k"] if switch else None,)] = prop
    names = {pair: name for name, pair in PROPERTIES.items()}
    mode = expected.get(PROPERTIES["mode"] + (None,))
    vacuum_first = (
        active and mode is not None and mode["value"] & 3 == METHODS["mop-after-vac"]
        and snapshot["state"] == 1
    )
    differences = []
    for (siid, piid, key), prop in expected.items():
        if active and key == "SuctionMax":
            continue
        value = snapshot[names[siid, piid]]
        wanted = prop["value"]
        if key is not None:
            # Intensive/Deep describe the mop route; vacuum-first can report Standard.
            if (vacuum_first and key == "CleanRoute"
                    and strict_json(wanted)["v"] in (ROUTES["intensive"], ROUTES["deep"])
                    and value.get(key) == ROUTES["standard"]):
                wanted = compact({"k": key, "v": ROUTES["standard"]})
            value = compact([{"k": k, "v": v} for k, v in value.items()])
        elif active and (siid, piid) == PROPERTIES["mode"]:
            value &= 0xFF03
            wanted &= 0xFF03
        if not property_matches({"siid": siid, "piid": piid, "code": 0, "value": value}, wanted):
            label = key or names[siid, piid]
            observed = snapshot["switches"].get(key) if key is not None else value
            requested = strict_json(wanted)["v"] if key is not None else wanted
            differences.append(f"{label}: expected {requested}, read {observed}")
    if differences:
        phase = "after start" if active else "before start"
        raise CloudError(
            "SETTINGS_CHANGED",
            f"The robot's settings differ from the validated plan {phase} ({'; '.join(differences)}). "
            "No following task will start.",
        )


def start_action(step, snapshot):
    if step["segment"] is None:
        return {"siid": 2, "aiid": 1, "in": []}
    job = step["job"]
    water = snapshot["water"]
    if "wetness" in job:
        water = 1 if job["wetness"] < 6 else 3 if job["wetness"] > 26 else 2
    fan = SUCTION[job["suction"]] if "suction" in job else snapshot["suction"]
    return {"siid": 4, "aiid": 1, "in": [
        {"piid": 1, "value": 18},
        {"piid": 10, "value": compact({"selects": [[step["segment"], job["passes"], fan, water, 1]]})},
    ]}
