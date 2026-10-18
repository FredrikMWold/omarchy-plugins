import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import ryde_data


class LocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.location_path = self.base / "state/location.json"
        self.cache_path = self.base / "cache/data.json"
        for name, value in (
            ("LOCATION_PATH", self.location_path),
            ("CACHE_PATH", self.cache_path),
        ):
            replacement = patch.object(ryde_data, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        environment = patch.dict(
            ryde_data.os.environ, {"XDG_RUNTIME_DIR": str(self.base / "runtime")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.position = ryde_data.manual_location(59.91, 10.75, "Oslo")

    def run_main(self, *args):
        output = io.StringIO()
        with patch.object(ryde_data.sys, "argv", ["ryde_data.py", *args]):
            with contextlib.redirect_stdout(output):
                code = ryde_data.main()
        return code, json.loads(output.getvalue())

    def test_missing_saved_position_is_not_a_default(self):
        self.assertIsNone(ryde_data.saved_location())

    def test_manual_position_survives_reload(self):
        ryde_data.save_location(self.position)
        self.assertEqual(ryde_data.saved_location(), self.position)

    def test_invalid_coordinates_are_rejected(self):
        for lat, lon in ((91, 0), (0, 181), (float("nan"), 0), (0, float("inf"))):
            with self.subTest(lat=lat, lon=lon):
                with self.assertRaisesRegex(RuntimeError, "valid position"):
                    ryde_data.manual_location(lat, lon, "Invalid")

    def test_corrupt_saved_position_is_reported(self):
        self.location_path.parent.mkdir(parents=True)
        self.location_path.write_text("not json")
        with self.assertRaisesRegex(RuntimeError, "Could not read"):
            ryde_data.saved_location()

    def test_unavailable_position_never_fetches_or_caches_default_data(self):
        with patch.object(
            ryde_data, "precise_location",
            side_effect=ryde_data.LocationUnavailable("Search for a place"),
        ), patch.object(ryde_data, "build_data") as build:
            code, payload = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(payload["errorCode"], "location_unavailable")
        self.assertIsNone(payload["location"])
        self.assertFalse(self.cache_path.exists())
        build.assert_not_called()

    def test_manual_selection_is_persisted_and_skips_geoclue(self):
        data = {"location": self.position, "vehicles": []}
        with patch.object(
            ryde_data, "build_data", return_value=data
        ) as build, patch.object(ryde_data, "precise_location") as precise:
            code, payload = self.run_main("--set-location", "59.91", "10.75", "Oslo")
        self.assertEqual(code, 0)
        self.assertEqual(payload, data)
        self.assertEqual(json.loads(self.cache_path.read_text()), data)
        self.assertEqual(ryde_data.saved_location(), self.position)
        build.assert_called_once_with(self.position)
        precise.assert_not_called()

    def test_refresh_uses_manual_position(self):
        ryde_data.save_location(self.position)
        with patch.object(
            ryde_data, "build_data", return_value={"location": self.position}
        ) as build, patch.object(ryde_data, "precise_location") as precise:
            code, _ = self.run_main()
        self.assertEqual(code, 0)
        build.assert_called_once_with(self.position)
        precise.assert_not_called()

    def test_automatic_clears_override_and_uses_geoclue(self):
        ryde_data.save_location(self.position)
        automatic = {**self.position, "isManual": False, "accuracy": 50}
        with patch.object(
            ryde_data, "precise_location", return_value=automatic
        ) as precise, patch.object(
            ryde_data, "build_data", return_value={"location": automatic}
        ) as build:
            code, _ = self.run_main("--automatic")
        self.assertEqual(code, 0)
        self.assertFalse(self.location_path.exists())
        precise.assert_called_once_with()
        build.assert_called_once_with(automatic)

    def test_failed_automatic_does_not_restore_manual_position(self):
        ryde_data.save_location(self.position)
        with patch.object(
            ryde_data, "precise_location",
            side_effect=ryde_data.LocationUnavailable("No accurate location found"),
        ), patch.object(ryde_data, "build_data") as build:
            code, payload = self.run_main("--automatic")
        self.assertEqual(code, 1)
        self.assertIsNone(payload["location"])
        self.assertFalse(self.location_path.exists())
        build.assert_not_called()

    def test_feed_failure_keeps_selected_position_and_reports_error(self):
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_text('{"location": "old position"}')
        with patch.object(
            ryde_data, "build_data", side_effect=RuntimeError("Entur unavailable")
        ):
            code, payload = self.run_main("--set-location", "59.91", "10.75", "Oslo")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "Entur unavailable")
        self.assertEqual(payload["location"], self.position)
        self.assertEqual(ryde_data.saved_location(), self.position)
        self.assertFalse(self.cache_path.exists())

    def test_lost_automatic_location_removes_cached_position(self):
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_text('{"location": "old position"}')
        with patch.object(
            ryde_data, "precise_location",
            side_effect=ryde_data.LocationUnavailable("No accurate location found"),
        ):
            code, payload = self.run_main()
        self.assertEqual(code, 1)
        self.assertIsNone(payload["location"])
        self.assertFalse(self.cache_path.exists())

    def test_outside_service_area_is_explicit_and_retains_position(self):
        with patch.object(
            ryde_data, "precise_location", return_value=self.position
        ), patch.object(
            ryde_data, "build_data",
            side_effect=RuntimeError("No Ryde service area is near Oslo"),
        ):
            code, payload = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(payload["errorCode"], "outside_service_area")
        self.assertEqual(payload["location"], self.position)

    def test_invalid_selection_does_not_overwrite_saved_position(self):
        ryde_data.save_location(self.position)
        for lat in ("nan", "91", "not-a-number"):
            with self.subTest(lat=lat):
                code, payload = self.run_main("--set-location", lat, "10.75", "Bad")
                self.assertEqual(code, 1)
                self.assertIn("valid position", payload["error"])
                self.assertEqual(ryde_data.saved_location(), self.position)


class SearchTests(unittest.TestCase):
    def test_search_encodes_query_and_converts_geojson_coordinates(self):
        response = {
            "features": [{
                "geometry": {"coordinates": [10.753, 59.910]},
                "properties": {"label": "Oslo S, Oslo"},
            }]
        }
        with patch.object(ryde_data, "fetch_json", return_value=response) as fetch:
            result = ryde_data.search_locations("  Oslo & station  ")
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertEqual(query["text"], ["Oslo & station"])
        self.assertEqual(query["size"], ["5"])
        self.assertEqual(result["results"][0]["lat"], 59.910)
        self.assertEqual(result["results"][0]["lon"], 10.753)
        self.assertTrue(result["results"][0]["isManual"])

    def test_empty_search_does_not_send_request(self):
        with patch.object(ryde_data, "fetch_json") as fetch:
            with self.assertRaisesRegex(RuntimeError, "Enter a place"):
                ryde_data.search_locations("  ")
        fetch.assert_not_called()

    def test_empty_results_are_distinct_from_network_failure(self):
        with patch.object(ryde_data, "fetch_json", return_value={"features": []}):
            self.assertEqual(ryde_data.search_locations("Unknown"), {"results": []})
        with patch.object(
            ryde_data, "fetch_json", side_effect=RuntimeError("Could not reach Entur")
        ):
            with self.assertRaisesRegex(RuntimeError, "Could not reach Entur"):
                ryde_data.search_locations("Oslo")

    def test_invalid_search_payload_is_reported(self):
        for response in (
            None, [], {}, {"features": {}}, {"features": [None]},
            {"features": [{"geometry": None, "properties": {}}]},
            {"features": [{"geometry": {"coordinates": None}, "properties": {}}]},
        ):
            with self.subTest(response=response):
                with patch.object(ryde_data, "fetch_json", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, "invalid search"):
                        ryde_data.search_locations("Oslo")

    def test_search_command_never_resolves_or_saves_a_position(self):
        output = io.StringIO()
        with patch.object(
            ryde_data.sys, "argv", ["ryde_data.py", "--search", "Oslo"]
        ), patch.object(
            ryde_data, "search_locations", return_value={"results": []}
        ), patch.object(
            ryde_data, "precise_location"
        ) as precise, patch.object(
            ryde_data, "save_location"
        ) as save, contextlib.redirect_stdout(output):
            code = ryde_data.main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"results": []})
        precise.assert_not_called()
        save.assert_not_called()

    def test_request_timeout_becomes_an_explicit_error(self):
        with patch.object(ryde_data, "urlopen", side_effect=TimeoutError("Timed out")):
            with self.assertRaisesRegex(RuntimeError, "Could not reach Entur"):
                ryde_data.search_locations("Oslo")


class GeoClueTests(unittest.TestCase):
    def setUp(self):
        location = Mock()
        location.props.latitude = 59.91
        location.props.longitude = 10.75
        location.props.description = "Wi-Fi"
        location.props.accuracy = 100
        self.location = location
        self.simple = Mock()
        self.simple.get_location.return_value = location
        bus = Mock()
        bus.call_sync.return_value.unpack.return_value = (True,)
        self.gio = Mock()
        self.gio.bus_get_sync.return_value = bus
        self.geoclue = Mock()
        self.geoclue.Simple.new_sync.return_value = self.simple
        self.glib = Mock()
        self.glib.Error = type("GeoClueError", (Exception,), {})
        self.glib.MainContext.default.return_value.pending.return_value = False
        repository = SimpleNamespace(
            Geoclue=self.geoclue, Gio=self.gio, GLib=self.glib
        )
        patches = [
            patch.dict(
                ryde_data.sys.modules,
                {"gi": Mock(), "gi.repository": repository},
            ),
            patch.object(ryde_data.threading, "Timer"),
            patch.object(ryde_data.time, "sleep"),
            patch.object(ryde_data.time, "monotonic", side_effect=[0, 1, 13]),
        ]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_acceptable_accuracy_returns_real_position(self):
        for accuracy in (1, 1000):
            self.location.props.accuracy = accuracy
            with patch.object(ryde_data.time, "monotonic", side_effect=[0, 1]):
                position = ryde_data.precise_location()
            self.assertEqual(position["lat"], 59.91)
            self.assertEqual(position["accuracy"], accuracy)
            self.assertFalse(position["isManual"])

    def test_poor_or_unknown_accuracy_requires_search(self):
        for accuracy in (0, 1001):
            self.location.props.accuracy = accuracy
            with patch.object(ryde_data.time, "monotonic", side_effect=[0, 1, 13]):
                with self.assertRaisesRegex(ryde_data.LocationUnavailable, "Search"):
                    ryde_data.precise_location()

    def test_no_fix_requires_search(self):
        self.simple.get_location.return_value = None
        with self.assertRaisesRegex(ryde_data.LocationUnavailable, "Search"):
            ryde_data.precise_location()

    def test_dbus_error_requires_search_instead_of_crashing(self):
        self.gio.bus_get_sync.side_effect = self.glib.Error("No session bus")
        with self.assertRaisesRegex(ryde_data.LocationUnavailable, "No session bus"):
            ryde_data.precise_location()


class DistanceTests(unittest.TestCase):
    def test_manual_position_is_used_for_service_area_and_distance(self):
        position = ryde_data.manual_location(59.91, 10.75, "Selected position")
        types = {
            "data": {
                "vehicle_types": [{
                    "vehicle_type_id": "scooter",
                    "form_factor": "scooter_standing",
                    "max_range_meters": 56300,
                }]
            }
        }
        status = {
            "data": {
                "vehicles": [
                    {
                        "vehicle_id": "nearby",
                        "vehicle_type_id": "scooter",
                        "lat": 59.911,
                        "lon": 10.75,
                        "current_range_meters": 28150,
                    },
                    {
                        "vehicle_id": "far-away",
                        "vehicle_type_id": "scooter",
                        "lat": 59.95,
                        "lon": 10.75,
                    },
                ]
            }
        }
        with patch.object(
            ryde_data, "fetch_json", side_effect=[types, status]
        ) as fetch:
            data = ryde_data.build_data(position)
        self.assertTrue(all("/rydeoslo/" in call.args[0] for call in fetch.call_args_list))
        self.assertEqual(data["count"], 1)
        self.assertAlmostEqual(data["nearestDistanceKm"], 0.1112, places=4)
        self.assertEqual(data["vehicles"][0]["batteryPercent"], 50)
        self.assertEqual(data["terrain"]["confidence"], "unavailable")
        self.assertEqual(data["location"], position)


if __name__ == "__main__":
    unittest.main()
