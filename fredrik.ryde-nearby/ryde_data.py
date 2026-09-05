#!/usr/bin/env python3

import fcntl
import gzip
import json
import math
import os
import statistics
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FALLBACK = {
    "lat": 58.9700,
    "lon": 5.7331,
    "accuracy": 0,
    "source": "Saved location",
    "label": "Stavanger",
    "isFallback": True,
}
ENTUR_BASE = "https://api.entur.io/mobility/v2/gbfs/v3"
ENTUR_HEADERS = {
    "ET-Client-Name": "fredrik-omarchy-ryde-nearby",
    "User-Agent": "fredrik-omarchy-ryde-nearby/1.1",
}
SYSTEMS = (
    ("rydeaalesund", "Ålesund", 62.4722, 6.1495),
    ("rydefredrikstad", "Fredrikstad", 59.2181, 10.9298),
    ("rydeoslo", "Oslo", 59.9139, 10.7522),
    ("rydeporsgrunn", "Porsgrunn", 59.1405, 9.6561),
    ("rydesandefjord", "Sandefjord", 59.1313, 10.2166),
    ("rydesarpsborg", "Sarpsborg", 59.2841, 11.1096),
    ("rydeskien", "Skien", 59.2096, 9.6090),
    ("rydestavanger", "Stavanger", 58.9700, 5.7331),
    ("rydetonsberg", "Tønsberg", 59.2675, 10.4076),
    ("rydetromso", "Tromsø", 69.6492, 18.9553),
    ("rydetrondheim", "Trondheim", 63.4305, 10.3951),
)
RADIUS_KM = 1.5
CACHE_PATH = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "fredrik.ryde-nearby"
    / "data.json"
)
TERRAIN_CACHE_PATH = CACHE_PATH.parent / "terrain"
TERRAIN_BASE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"
TERRAIN_VALID_SIZES = {
    1201 * 1201 * 2,
    3601 * 3601 * 2,
}
TERRAIN_MAX_BYTES = max(TERRAIN_VALID_SIZES)
TERRAIN_SAMPLE_RADIUS_M = 30
TERRAIN_MAX_SPREAD_M = 10
TERRAIN_ALLOWANCE_M = 5
UPHILL_EFFORT_FACTOR = 5000 / 600


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(value))


def precise_location():
    agent = None
    try:
        import gi

        gi.require_version("Geoclue", "2.0")
        from gi.repository import Geoclue, Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def agent_registered():
            reply = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant(
                    "(s)",
                    ("org.freedesktop.GeoClue2.DemoAgent",),
                ),
                GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            return reply.unpack()[0]

        if not agent_registered():
            agent = subprocess.Popen(
                ["/usr/lib/geoclue-2.0/demos/agent"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.8)
            if agent.poll() is not None and not agent_registered():
                raise RuntimeError(
                    "GeoClue authorization agent could not start"
                )

        cancellable = Gio.Cancellable()
        creation_timeout = threading.Timer(8, cancellable.cancel)
        creation_timeout.start()
        try:
            simple = Geoclue.Simple.new_sync(
                "fredrik.ryde-nearby",
                Geoclue.AccuracyLevel.EXACT,
                cancellable,
            )
        except GLib.Error as error:
            raise RuntimeError(
                "GeoClue client creation timed out"
            ) from error
        finally:
            creation_timeout.cancel()

        best = [None]

        def read_location(*_args):
            location = simple.get_location()
            if location is None:
                return
            best[0] = {
                "lat": location.props.latitude,
                "lon": location.props.longitude,
                "accuracy": round(location.props.accuracy),
                "source": location.props.description or "GeoClue",
                "label": "Current location",
                "isFallback": False,
            }

        read_location()
        simple.connect("notify::location", read_location)
        deadline = time.monotonic() + 12
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if best[0] is not None and 0 < best[0]["accuracy"] <= 1000:
                return best[0]
            time.sleep(0.1)
    except (ImportError, OSError, RuntimeError):
        pass
    finally:
        if agent is not None and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=2)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait(timeout=2)
    return FALLBACK.copy()


def fetch_json(url):
    request = Request(url, headers=ENTUR_HEADERS)
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Entur returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Entur: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Entur returned invalid data") from error


def terrain_tile_name(lat, lon):
    south = math.floor(lat)
    west = math.floor(lon)
    latitude = f"{'N' if south >= 0 else 'S'}{abs(south):02d}"
    longitude = f"{'E' if west >= 0 else 'W'}{abs(west):03d}"
    return latitude + longitude, south, west


def ensure_terrain_tile(name):
    TERRAIN_CACHE_PATH.mkdir(parents=True, exist_ok=True)
    path = TERRAIN_CACHE_PATH / f"{name}.hgt"
    if path.exists() and path.stat().st_size in TERRAIN_VALID_SIZES:
        return path
    if path.exists():
        path.unlink()

    temporary_path = path.with_suffix(".tmp")
    url = f"{TERRAIN_BASE_URL}/{name[:3]}/{name}.hgt.gz"
    request = Request(
        url,
        headers={"User-Agent": ENTUR_HEADERS["User-Agent"]},
    )
    try:
        with urlopen(request, timeout=20) as response:
            with gzip.GzipFile(fileobj=response) as compressed:
                with temporary_path.open("wb") as output:
                    written = 0
                    while True:
                        chunk = compressed.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > TERRAIN_MAX_BYTES:
                            raise RuntimeError("Terrain tile exceeded the expected size")
                        output.write(chunk)
        if temporary_path.stat().st_size not in TERRAIN_VALID_SIZES:
            raise RuntimeError("Terrain tile had an unexpected size")
        temporary_path.replace(path)
        return path
    except (HTTPError, URLError, OSError, EOFError, gzip.BadGzipFile) as error:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"Local terrain data unavailable: {error}") from error
    except RuntimeError:
        temporary_path.unlink(missing_ok=True)
        raise


class TerrainGrid:
    def __init__(self, path, south, west):
        self.data = path.read_bytes()
        self.side = math.isqrt(len(self.data) // 2)
        if self.side * self.side * 2 != len(self.data):
            raise RuntimeError("Terrain tile grid was invalid")
        self.south = south
        self.west = west

    def elevation(self, lat, lon):
        row = (self.south + 1 - lat) * (self.side - 1)
        column = (lon - self.west) * (self.side - 1)
        row = max(0, min(self.side - 1, row))
        column = max(0, min(self.side - 1, column))
        row0 = math.floor(row)
        column0 = math.floor(column)
        row1 = min(self.side - 1, row0 + 1)
        column1 = min(self.side - 1, column0 + 1)
        row_fraction = row - row0
        column_fraction = column - column0
        samples = (
            (row0, column0, (1 - row_fraction) * (1 - column_fraction)),
            (row0, column1, (1 - row_fraction) * column_fraction),
            (row1, column0, row_fraction * (1 - column_fraction)),
            (row1, column1, row_fraction * column_fraction),
        )
        weighted = []
        for sample_row, sample_column, weight in samples:
            value = struct.unpack_from(
                ">h",
                self.data,
                2 * (sample_row * self.side + sample_column),
            )[0]
            if value != -32768 and weight > 0:
                weighted.append((value, weight))
        if not weighted:
            return None
        total_weight = sum(weight for _, weight in weighted)
        return sum(value * weight for value, weight in weighted) / total_weight


class TerrainSampler:
    def __init__(self):
        self.grids = {}
        self.errors = {}

    def elevation(self, lat, lon):
        name, south, west = terrain_tile_name(lat, lon)
        if name not in self.grids and name not in self.errors:
            try:
                self.grids[name] = TerrainGrid(
                    ensure_terrain_tile(name),
                    south,
                    west,
                )
            except RuntimeError as error:
                self.errors[name] = str(error)
        grid = self.grids.get(name)
        return grid.elevation(lat, lon) if grid else None


def offset_coordinate(lat, lon, north_meters, east_meters):
    latitude = lat + north_meters / 111320
    longitude_scale = 111320 * math.cos(math.radians(lat))
    longitude = lon + east_meters / longitude_scale
    return latitude, longitude


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def elevation_profile(sampler, lat, lon, radius_meters):
    coordinates = [(lat, lon)]
    for index in range(8):
        angle = 2 * math.pi * index / 8
        coordinates.append(
            offset_coordinate(
                lat,
                lon,
                radius_meters * math.cos(angle),
                radius_meters * math.sin(angle),
            )
        )
    values = [
        elevation
        for sample_lat, sample_lon in coordinates
        if (elevation := sampler.elevation(sample_lat, sample_lon)) is not None
    ]
    if len(values) < 7:
        return None
    return {
        "median": statistics.median(values),
        "spread": percentile(values, 0.9) - percentile(values, 0.1),
        "samples": len(values),
    }


def add_elevation_data(location, vehicles):
    for vehicle in vehicles:
        vehicle.update(
            {
                "elevationM": None,
                "endpointGainM": None,
                "effectiveUphillM": None,
                "uphillAdjustedDistanceKm": vehicle["distanceKm"],
            }
        )

    sampler = TerrainSampler()
    accuracy = float(location.get("accuracy") or 0)
    usable_location = (
        not location.get("isFallback")
        and 0 < accuracy <= 100
    )
    location_profile = (
        elevation_profile(sampler, location["lat"], location["lon"], accuracy)
        if usable_location
        else None
    )
    if location_profile:
        location["elevationM"] = round(location_profile["median"])
        location["elevationSampleSpreadM"] = round(location_profile["spread"], 1)

    location_confident = (
        location_profile is not None
        and location_profile["spread"] <= TERRAIN_MAX_SPREAD_M
    )
    adjusted_count = 0
    if location_confident:
        for vehicle in vehicles:
            vehicle_profile = elevation_profile(
                sampler,
                vehicle["lat"],
                vehicle["lon"],
                TERRAIN_SAMPLE_RADIUS_M,
            )
            if (
                vehicle_profile is None
                or vehicle_profile["spread"] > TERRAIN_MAX_SPREAD_M
            ):
                continue
            endpoint_gain = max(
                0,
                vehicle_profile["median"] - location_profile["median"],
            )
            effective_uphill = max(0, endpoint_gain - TERRAIN_ALLOWANCE_M)
            effort_meters = (
                vehicle["distanceKm"] * 1000
                + effective_uphill * UPHILL_EFFORT_FACTOR
            )
            vehicle.update(
                {
                    "elevationM": round(vehicle_profile["median"]),
                    "elevationSampleSpreadM": round(
                        vehicle_profile["spread"], 1
                    ),
                    "endpointGainM": round(endpoint_gain),
                    "effectiveUphillM": round(effective_uphill),
                    "uphillAdjustedDistanceKm": round(effort_meters / 1000, 4),
                }
            )
            adjusted_count += 1

    return {
        "available": bool(sampler.grids),
        "confidence": "usable" if location_confident else "unavailable",
        "source": "SRTM1 local terrain",
        "resolutionM": 30,
        "method": "endpoint-only",
        "allowanceM": TERRAIN_ALLOWANCE_M,
        "effortFactor": round(UPHILL_EFFORT_FACTOR, 2),
        "adjustedVehicles": adjusted_count,
        "errors": list(sampler.errors.values()),
    }


def build_data():
    location = precise_location()
    lat = location["lat"]
    lon = location["lon"]
    system = min(SYSTEMS, key=lambda item: haversine_km(lat, lon, item[2], item[3]))
    system_id, system_name, city_lat, city_lon = system
    if haversine_km(lat, lon, city_lat, city_lon) > 80:
        raise RuntimeError(f"No Ryde service area is near {location['label']}")

    types_payload = fetch_json(f"{ENTUR_BASE}/{system_id}/vehicle_types")
    scooter_types = {
        vehicle_type["vehicle_type_id"]: vehicle_type.get("max_range_meters")
        for vehicle_type in types_payload.get("data", {}).get("vehicle_types", [])
        if vehicle_type.get("form_factor") == "scooter_standing"
    }
    status_payload = fetch_json(f"{ENTUR_BASE}/{system_id}/vehicle_status")

    vehicles = []
    for vehicle in status_payload.get("data", {}).get("vehicles", []):
        if vehicle.get("vehicle_type_id") not in scooter_types:
            continue
        if vehicle.get("is_reserved") or vehicle.get("is_disabled"):
            continue
        vehicle_lat = vehicle.get("lat")
        vehicle_lon = vehicle.get("lon")
        if not isinstance(vehicle_lat, (int, float)) or not isinstance(vehicle_lon, (int, float)):
            continue
        distance = haversine_km(lat, lon, vehicle_lat, vehicle_lon)
        if distance > RADIUS_KM:
            continue
        current_range = vehicle.get("current_range_meters")
        maximum_range = scooter_types.get(vehicle.get("vehicle_type_id"))
        vehicles.append(
            {
                "vehicleId": vehicle.get("vehicle_id"),
                "lat": vehicle_lat,
                "lon": vehicle_lon,
                "distanceKm": round(distance, 4),
                "rangeKm": (
                    round(current_range / 1000, 1)
                    if isinstance(current_range, (int, float))
                    else None
                ),
                "batteryPercent": (
                    max(0, min(100, round(current_range / maximum_range * 100)))
                    if isinstance(current_range, (int, float))
                    and isinstance(maximum_range, (int, float))
                    and maximum_range > 0
                    else None
                ),
            }
        )

    vehicles.sort(key=lambda item: item["distanceKm"])
    terrain = add_elevation_data(location, vehicles)
    return {
        "location": location,
        "systemName": system_name,
        "radiusKm": RADIUS_KM,
        "count": len(vehicles),
        "nearestDistanceKm": vehicles[0]["distanceKm"] if vehicles else None,
        "nearestProximityDistanceKm": (
            vehicles[0]["uphillAdjustedDistanceKm"] if vehicles else None
        ),
        "terrain": terrain,
        "rangeScale": {
            "emptyKm": 0,
            "fullKm": max(scooter_types.values()) / 1000,
        },
        "vehicles": vehicles[:250],
        "lastUpdated": status_payload.get("last_updated"),
        "fetchedAt": int(time.time()),
    }


def main():
    runtime_dir = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    )
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with (runtime_dir / "fredrik.ryde-nearby.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            encoded = json.dumps(
                build_data(),
                ensure_ascii=True,
                separators=(",", ":"),
            )
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = CACHE_PATH.with_suffix(".tmp")
            temporary_path.write_text(encoded + "\n")
            temporary_path.replace(CACHE_PATH)
            print(encoded)
        except RuntimeError as error:
            message = str(error)
            code = (
                "outside_service_area"
                if message.startswith("No Ryde service area")
                else "fetch_failed"
            )
            print(json.dumps({"error": message, "errorCode": code}))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
