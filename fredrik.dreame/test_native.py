import base64
import json
import unittest
import zlib

from cloud import CloudError, property_matches
import native


def map_entry(map_id, segments, entry_id=1):
    header = bytearray(27)
    header[:2] = map_id.to_bytes(2, "little")
    header[19:21] = (2).to_bytes(2, "little")
    header[21:23] = (1).to_bytes(2, "little")
    blob = header + b"\x01\x02" + json.dumps({"seg_inf": segments}).encode()
    return {"id": entry_id, "map": base64.b64encode(zlib.compress(blob)).decode()}


def snapshot():
    return {
        "state": 13, "error": 0, "battery": 100, "charging": 1, "task": 0,
        "mode": 5123, "custom": 0, "cancelled": 0, "wetness": 16, "water": 2,
        "suction": 1, "switches": {"SmartHost": 0, "CleanRoute": 1, "SuctionMax": 0, "BackWashType": 1},
    }


def job(method="vacuum", rooms=None):
    result = {
        "target": {"scope": "rooms", "rooms": rooms} if rooms is not None else {"scope": "all"},
        "method": method, "route": "standard",
    }
    if rooms is not None:
        result["passes"] = 2
    if method != "mop":
        result.update(suction="quiet", maxSuctionPower=False)
    if method != "vacuum":
        result.update(wetness=32, washing={"by": "area", "value": 15})
    return result


class NativeTests(unittest.TestCase):
    def test_map_selection_uses_binary_id_not_catalogue_entry_index(self):
        info = {"curr_id": 765, "mapstr": [
            map_entry(111, {"1": {"type": 6}}, entry_id=765),
            map_entry(765, {"1": {"type": 4}, "2": {}, "3": {"type": 2}}, entry_id=1),
        ]}
        decoded = native.decode_rooms(info)
        self.assertEqual(decoded["rooms"], {"Kitchen": 1, "Room 2": 2, "Master Bedroom": 3})
        self.assertEqual(decoded["mapId"], "765")
        self.assertEqual(len(decoded["signature"]), 64)

    def test_ambiguous_map_and_room_names_are_rejected(self):
        entry = map_entry(765, {"1": {"type": 4}, "2": {"type": 4}})
        with self.assertRaises(CloudError):
            native.decode_rooms({"curr_id": 765, "mapstr": [entry]})
        entry = map_entry(765, {"1": {"type": 4}})
        for entries in ([], [entry, entry]):
            with self.assertRaises(CloudError):
                native.decode_rooms({"curr_id": 765, "mapstr": entries})

    def test_map_decompression_is_bounded_and_encrypted_maps_are_explicit(self):
        for encoded in ("data,key", "invalid base64", base64.b64encode(zlib.compress(b"x" * (8 * 1024 * 1024 + 1))).decode()):
            with self.assertRaises(CloudError):
                native.decode_map_blob(encoded)

    def test_custom_room_names_are_preserved_without_guessing(self):
        name = base64.b64encode(b"Kitchen").decode()
        result = native.decode_rooms({"curr_id": 765, "mapstr": [map_entry(765, {"7": {"name": name, "type": 6}})]})
        self.assertEqual(result["rooms"], {"Kitchen": 7})

    def test_zero_battery_and_packed_mode_are_displayed_correctly(self):
        values = [
            {"siid": 3, "piid": 1, "code": 0, "value": 0},
            {"siid": 4, "piid": 23, "code": 0, "value": 5123},
            {"siid": 4, "piid": 4, "code": -4004},
        ]
        fields = {v["key"]: v for v in native.status_display(values)}
        self.assertEqual(fields["battery"]["text"], "0%")
        self.assertEqual(fields["mode"]["text"], "Mop after vacuum")
        self.assertFalse(fields["suction"]["available"])
        self.assertIn("-4004", fields["suction"]["text"])
        self.assertEqual(fields["wetness"]["text"], "Unavailable")

    def test_queue_targets_preserve_job_room_order_and_native_passes(self):
        rooms = {"rooms": {"Kitchen": 7, "Bathroom": 9}}
        packet = {"jobs": [job(rooms=["Bathroom", "Kitchen"]), job("mop", rooms=["Kitchen"])]}
        steps = native.compile_steps(packet, rooms, snapshot())
        self.assertEqual([s["segment"] for s in steps], [9, 7, 7])
        self.assertEqual([s["jobIndex"] for s in steps], [0, 0, 1])
        action = native.start_action(steps[0], snapshot())
        self.assertEqual(action["siid"], 4)
        self.assertEqual(json.loads(action["in"][1]["value"])["selects"], [[9, 2, 0, 2, 1]])

    def test_whole_home_action_has_no_invented_pass_count(self):
        steps = native.compile_steps({"jobs": [job()]}, {"rooms": {}}, snapshot())
        self.assertEqual(native.start_action(steps[0], snapshot()), {"siid": 2, "aiid": 1, "in": []})

    def test_unmapped_later_room_prevents_entire_program_compilation(self):
        with self.assertRaisesRegex(CloudError, "no unique room"):
            native.compile_steps({"jobs": [job(), job(rooms=["Office"])]}, {"rooms": {}}, snapshot())

    def test_mop_settings_order_and_packed_field_preservation(self):
        settings = native.settings_for(job("mop"), snapshot())
        self.assertEqual(settings[2]["value"], 5121)
        self.assertEqual((settings[3]["siid"], settings[3]["piid"], settings[3]["value"]), (28, 1, 32))
        self.assertEqual(json.loads(settings[4]["value"]), {"k": "BackWashType", "v": 1})
        self.assertEqual(settings[5]["value"], 3841)
        self.assertEqual(json.loads(settings[-1]["value"]), {"k": "CleanRoute", "v": 1})

    def test_idle_requires_no_task_and_no_fault(self):
        for field, value in (("task", 1), ("error", 2), ("state", 1)):
            with self.assertRaises(CloudError):
                native.require_idle({**snapshot(), field: value})

    def test_max_suction_boost_requires_full_charge(self):
        boosted = {**job(), "maxSuctionPower": True}
        with self.assertRaises(CloudError):
            native.compile_steps({"jobs": [boosted]}, {"rooms": {}}, {**snapshot(), "battery": 99})

    def test_switch_readback_checks_exact_key_type_and_uniqueness(self):
        expected = '{"k":"CleanRoute","v":1}'
        item = {"siid": 4, "piid": 50, "code": 0, "value": '[{"k":"CleanRoute","v":1},{"k":"SuctionMax","v":0}]'}
        self.assertTrue(property_matches(item, expected))
        for value in (
            '[{"k":"CleanRoute","v":true}]', '[{"k":"CleanRoute","v":2}]',
            '[{"k":"CleanRoute","v":1},{"k":"CleanRoute","v":1}]', "invalid",
        ):
            self.assertFalse(property_matches({**item, "value": value}, expected))
        self.assertFalse(property_matches({"siid": 4, "piid": 4, "code": 0, "value": True}, 1))

    def test_settings_error_identifies_every_changed_controlled_value(self):
        settings = [
            {"siid": 4, "piid": 50, "value": '{"k":"CleanRoute","v":2}'},
            {"siid": 28, "piid": 1, "value": 28},
        ]
        with self.assertRaises(CloudError) as failure:
            native.verify_settings(settings, snapshot(), active=True)
        self.assertEqual(failure.exception.code, "SETTINGS_CHANGED")
        self.assertIn("after start", str(failure.exception))
        self.assertIn("CleanRoute: expected 2, read 1", str(failure.exception))
        self.assertIn("wetness: expected 28, read 16", str(failure.exception))

    def test_captured_vacuum_first_snapshot_accepts_standard_only_for_its_vacuum_phase(self):
        planned = {
            **job("mop-after-vac", rooms=["Kitchen"]),
            "passes": 1, "suction": "max", "wetness": 28,
            "washing": {"by": "area", "value": 10}, "route": "intensive",
        }
        observed = {
            **snapshot(), "state": 1, "task": 3, "mode": 2563,
            "suction": 3, "water": 1, "wetness": 28,
        }
        settings = native.settings_for(planned, observed)
        native.verify_settings(settings, observed, active=True)
        native.verify_settings(native.settings_for({**planned, "route": "deep"}, observed), observed, active=True)
        for fields, active in (
            ({}, False),
            ({"state": 7}, True),
            ({"state": 12}, True),
            ({"wetness": 30}, True),
            ({"switches": {**observed["switches"], "CleanRoute": 4}}, True),
        ):
            with self.subTest(fields=fields, active=active):
                with self.assertRaises(CloudError):
                    native.verify_settings(settings, {**observed, **fields}, active=active)
        for method, route in (("mop", "intensive"), ("mop-after-vac", "quick")):
            with self.subTest(method=method, route=route):
                other_job = {
                    **job(method, rooms=["Kitchen"]),
                    "wetness": 28, "washing": {"by": "area", "value": 10}, "route": route,
                }
                if method != "mop":
                    other_job["suction"] = "max"
                settings = native.settings_for(other_job, observed)
                with self.assertRaises(CloudError):
                    native.verify_settings(settings, {**observed, "mode": settings[2]["value"]}, active=True)


if __name__ == "__main__":
    unittest.main()
