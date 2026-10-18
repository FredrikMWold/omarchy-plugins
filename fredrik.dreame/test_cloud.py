import base64
import copy
import json
import ssl
import unittest
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from cloud import (
    CloudClient, CloudError, HTTPTransport, NoRedirect, canonical,
    md5, validate_action, validate_properties,
)


PROFILE = {
    "version": 1, "passwordSalt": "fixture-salt-not-a-secret",
    "clientKey": "0123456789abcdef", "basicAuthorization": "Zml4dHVyZQ==",
    "tenantId": "fixture-tenant", "appVersion": "fixture-version",
    "userAgent": "Dart/3.9 (dart:io)",
}
SESSION = {
    "version": 1, "username": "fixture@example.invalid", "region": "eu",
    "fingerprint": "a" * 32, "accessToken": "fixture-access",
    "refreshToken": "fixture-refresh", "expiresAt": 10000,
}
DEVICE = {"did": "123", "name": "Fixture vacuum", "model": "dreame.vacuum.fixture", "binding": "10000.mt.example.invalid:123"}


def success(data):
    return 200, {"code": 0, "data": data}


def command_result(result):
    return success({"result": result})


def property_result(value=13, siid=2, piid=1, code=0):
    return {"siid": siid, "piid": piid, "code": code, "value": value}


def token_response(**extra):
    return 200, {
        "access_token": "fixture-access-new", "refresh_token": "fixture-refresh-new",
        "expires_in": 3600, **extra,
    }


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, copy.deepcopy(headers), body))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class CloudTests(unittest.TestCase):
    def make_client(self, *responses, session=None):
        self.transport = Transport(*responses)
        self.saved = []
        return CloudClient(PROFILE, session or SESSION, self.saved.append, self.transport, lambda: 1000)

    def test_canonical_reference_rules(self):
        self.assertEqual(
            canonical({"z": [3, {"é": "水", "a": 2}], "a": {"y": "hello", "z": [1], "b": True}, "empty": {}, "n": None}),
            'a=[b=true&y="hello"]&empty=]&n=null&z=[3,{"a":2,"é":"水"}]',
        )
        self.assertEqual(canonical({"v": False, "count": 2}), "count=2&v=false")

    def test_password_login_has_correct_form_and_no_password_persistence(self):
        client = self.make_client(token_response())
        client.login("fixture password")
        url, headers, body = self.transport.calls[0]
        self.assertEqual(url, "https://eu.iot.dreame.tech:13267/dreame-auth/oauth/token")
        form = parse_qs(body.decode())
        self.assertEqual(form["grant_type"], ["password"])
        self.assertEqual(form["username"], ["fixture@example.invalid"])
        self.assertEqual(form["password"], [md5("fixture password" + PROFILE["passwordSalt"])])
        self.assertNotIn("dreame-auth", headers)
        self.assertNotIn("password", json.dumps(self.saved))
        self.assertEqual(self.saved[-1]["expiresAt"], 4600)
        self.assertEqual(self.saved[-1]["refreshToken"], "fixture-refresh-new")

    def test_fingerprint_and_region_header(self):
        session = {**SESSION, "accountRegion": "EU", "language": "en", "country": "SE"}
        client = self.make_client(session=session)
        headers = client.headers("application/json", True)
        self.assertIn("canvasHash=" + md5(SESSION["username"] + "c")[:8], headers["dreame-meta"])
        decrypt = Cipher(algorithms.AES(PROFILE["clientKey"].encode()), modes.ECB()).decryptor()
        value = decrypt.update(base64.b64decode(headers["dreame-rlc"])) + decrypt.finalize()
        unpad = PKCS7(128).unpadder()
        self.assertEqual(unpad.update(value) + unpad.finalize(), b"EU|en|SE")

    def test_expired_token_refreshes_and_uses_rotated_access_token(self):
        client = self.make_client(token_response(), success({"value": 1}), session={**SESSION, "expiresAt": 1020})
        self.assertEqual(client.api("/fixture", {"x": 1}), {"value": 1})
        self.assertEqual(self.transport.calls[1][1]["dreame-auth"], "fixture-access-new")
        form = parse_qs(self.transport.calls[0][2].decode())
        self.assertEqual(form["refresh_token"], ["fixture-refresh"])
        self.assertEqual(self.saved[-1]["refreshToken"], "fixture-refresh-new")

    def test_signature_uses_canonical_parameters(self):
        client = self.make_client(success({}))
        client.api("/fixture", {"b": "value", "a": 2})
        body = json.loads(self.transport.calls[0][2])
        self.assertEqual(body["timestamp"], 1000000)
        self.assertEqual(body["sign"], md5("a=2&b=value1000000" + PROFILE["clientKey"]))

    def test_read_401_refreshes_and_retries_only_once(self):
        client = self.make_client((401, {}), token_response(), success({"read": True}))
        self.assertEqual(client.api("/fixture"), {"read": True})
        self.assertEqual(len(self.transport.calls), 3)

    def test_mutation_401_is_never_replayed(self):
        client = self.make_client((401, {}))
        with self.assertRaisesRegex(CloudError, "No device-changing command was replayed"):
            client.api("/fixture", {"x": 1}, read_only=False)
        self.assertEqual(len(self.transport.calls), 1)

    def test_network_failure_and_rate_limit_do_not_retry_mutation(self):
        for response in (CloudError("NETWORK_ERROR", "fixture timeout"), (429, {})):
            client = self.make_client(response)
            with self.assertRaises(CloudError):
                client.api("/fixture", {"x": 1}, read_only=False)
            self.assertEqual(len(self.transport.calls), 1)

    def test_refresh_rejection_clears_tokens_but_temporary_failure_does_not(self):
        client = self.make_client((401, {}))
        with self.assertRaises(CloudError):
            client.refresh()
        self.assertNotIn("refreshToken", self.saved[-1])
        client = self.make_client((503, {}))
        with self.assertRaises(CloudError):
            client.refresh()
        self.assertEqual(self.saved, [])
        self.assertEqual(client.session["refreshToken"], SESSION["refreshToken"])

    def test_refresh_can_reuse_existing_refresh_token_when_not_rotated(self):
        status, response = token_response()
        del response["refresh_token"]
        client = self.make_client((status, response))
        client.refresh()
        self.assertEqual(self.saved[-1]["refreshToken"], SESSION["refreshToken"])

    def test_invalid_token_lifetime_does_not_persist(self):
        client = self.make_client(token_response(expires_in=0))
        with self.assertRaises(CloudError):
            client.refresh()
        self.assertEqual(self.saved, [])

    def test_invalid_token_header_characters_do_not_persist(self):
        for key in ("access_token", "refresh_token"):
            for character in "\r\n\0":
                client = self.make_client(token_response(**{key: "fixture" + character}))
                with self.assertRaises(CloudError):
                    client.login("fixture password")
                self.assertEqual(self.saved, [])

    def test_device_discovery_and_partial_page_refusal(self):
        record = {"did": 123, "model": DEVICE["model"], "bindDomain": DEVICE["binding"], "customName": DEVICE["name"]}
        client = self.make_client(success({"page": {"records": [record], "total": 1}}))
        self.assertEqual(client.devices(), [DEVICE])
        client = self.make_client(success({"page": {"records": [record], "total": 2}}))
        with self.assertRaisesRegex(CloudError, "partial device page"):
            client.devices()

    def test_device_discovery_accepts_decimal_string_counts(self):
        record = {"did": 123, "model": DEVICE["model"], "bindDomain": DEVICE["binding"], "customName": DEVICE["name"]}
        client = self.make_client(success({"page": {"records": [record], "total": "1"}}))
        self.assertEqual(client.devices(), [DEVICE])
        client = self.make_client(success({"page": {"records": [], "total": "0"}}))
        self.assertEqual(client.devices(), [])

    def test_decimal_string_partial_page_is_still_refused(self):
        record = {"did": 123, "model": DEVICE["model"]}
        client = self.make_client(success({"page": {"records": [record], "total": "2"}}))
        with self.assertRaisesRegex(CloudError, "partial device page"):
            client.devices()

    def test_invalid_or_inconsistent_device_count_is_rejected(self):
        record = {"did": 123, "model": DEVICE["model"]}
        for total in (True, None, 1.0, -1, 0, "0", "1.0", " 1", "+1", "-1", "one", "\u0661", "9" * 10000):
            with self.subTest(total=repr(total)[:30]):
                client = self.make_client(success({"page": {"records": [record], "total": total}}))
                with self.assertRaises(CloudError) as caught:
                    client.devices()
                self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    def test_command_uses_bound_shard_and_device_id(self):
        client = self.make_client(command_result([property_result()]))
        self.assertEqual(client.properties(DEVICE, [{"siid": 2, "piid": 1}])[0]["value"], 13)
        url, _, raw = self.transport.calls[0]
        body = json.loads(raw)
        self.assertEqual(url, "https://eu.iot.dreame.tech:13267/dreame-iot-com-10000/device/sendCommand")
        self.assertEqual(body["id"], body["data"]["id"])
        self.assertEqual(body["data"]["params"], [{"did": "123", "siid": 2, "piid": 1}])

    def test_unsafe_binding_does_not_send(self):
        client = self.make_client()
        with self.assertRaises(CloudError):
            client.properties({**DEVICE, "binding": "../other"}, [{"siid": 2, "piid": 1}])
        self.assertEqual(self.transport.calls, [])

    def test_property_result_must_cover_exact_requested_identifiers(self):
        for result in ([], [property_result(), property_result()], [property_result(siid=4)], [property_result(siid=True)]):
            client = self.make_client(command_result(result))
            with self.assertRaises(CloudError):
                client.properties(DEVICE, [{"siid": 2, "piid": 1}])

    def test_unsupported_property_code_is_preserved_not_fabricated(self):
        client = self.make_client(command_result([property_result(code=-4004, value=None)]))
        self.assertEqual(client.properties(DEVICE, [{"siid": 2, "piid": 1}])[0]["code"], -4004)

    def test_write_requires_live_idle_and_verifies_readback(self):
        write = [{"siid": 4, "piid": 4, "value": 1}]
        client = self.make_client(
            command_result([property_result()]),
            command_result([property_result(siid=4, piid=4, value=1)]),
            command_result([property_result(siid=4, piid=4, value=1)]),
        )
        result = client.mutate(DEVICE, "set_properties", write)
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(json.loads(self.transport.calls[1][2])["data"]["method"], "set_properties")

    def test_active_vacuum_blocks_mutation(self):
        client = self.make_client(command_result([property_result(value=1)]))
        with self.assertRaisesRegex(CloudError, "must report standby"):
            client.mutate(DEVICE, "action", {"siid": 4, "aiid": 1, "in": []})
        self.assertEqual(len(self.transport.calls), 1)

    def test_mismatched_readback_never_retries_or_rolls_back(self):
        client = self.make_client(
            command_result([property_result()]),
            command_result([property_result(siid=4, piid=4, value=1)]),
            command_result([property_result(siid=4, piid=4, value=2)]),
        )
        with self.assertRaisesRegex(CloudError, "readback did not match"):
            client.mutate(DEVICE, "set_properties", [{"siid": 4, "piid": 4, "value": 1}])
        self.assertEqual(len(self.transport.calls), 3)

    def test_incomplete_write_ack_is_not_success(self):
        client = self.make_client(command_result([property_result()]), command_result([]))
        with self.assertRaisesRegex(CloudError, "acknowledgement was incomplete"):
            client.mutate(DEVICE, "set_properties", [{"siid": 4, "piid": 4, "value": 1}])
        self.assertEqual(len(self.transport.calls), 2)

    def test_invalid_write_ack_identifiers_do_not_read_back_or_retry(self):
        for value in (True, [], {}, "4"):
            client = self.make_client(
                command_result([property_result()]),
                command_result([property_result(siid=value, piid=4)]),
            )
            with self.assertRaisesRegex(CloudError, "invalid property identifiers"):
                client.mutate(DEVICE, "set_properties", [{"siid": 4, "piid": 4, "value": 1}])
            self.assertEqual(len(self.transport.calls), 2)

    def test_action_ack_does_not_claim_physical_completion(self):
        client = self.make_client(command_result([property_result()]), command_result({"code": 0}))
        result = client.mutate(DEVICE, "action", {"siid": 4, "aiid": 1, "in": []})
        self.assertFalse(result["physicalOutcomeVerified"])

    def test_invalid_commands_and_boolean_identifiers(self):
        for value in ([], [{"siid": True, "piid": 1}], [{"siid": 2, "piid": 1, "extra": 0}]):
            with self.assertRaises(CloudError):
                validate_properties(value)
        with self.assertRaises(CloudError):
            validate_action({"siid": 4, "aiid": 1, "in": [], "did": "other"})

    def test_transport_verifies_tls_and_refuses_redirects(self):
        transport = HTTPTransport()
        handler = next(h for h in transport.opener.handlers if hasattr(h, "_context"))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)
        with self.assertRaisesRegex(CloudError, "credentials were not forwarded"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")


if __name__ == "__main__":
    unittest.main()
