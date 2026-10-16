import asyncio
from functools import partial
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

from androidtvremote2 import AndroidTVRemote
from androidtvremote2.remotemessage_pb2 import RemoteMessage

from tv_voice import VoiceRemote
import tv_voice
import router


async def read_message(reader):
    length = shift = 0
    while True:
        value = (await reader.readexactly(1))[0]
        length |= (value & 0x7f) << shift
        if value < 0x80:
            break
        shift += 7
        if shift > 28:
            raise ValueError("Invalid fixture message length")
    return RemoteMessage.FromString(await reader.readexactly(length))


async def write_message(writer, message):
    payload = message.SerializeToString()
    length = len(payload)
    prefix = bytearray()
    while length > 0x7f:
        prefix.append((length & 0x7f) | 0x80)
        length >>= 7
    writer.write(bytes([*prefix, length]) + payload)
    await writer.drain()


class AppVoiceProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.base = base
        certificate, key = base / "client.crt", base / "client.key"
        generator = AndroidTVRemote("voice fixture", str(certificate), str(key), "127.0.0.1")
        await generator.async_generate_cert_if_missing()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.received = []
        self.messages = asyncio.Queue()
        self.sessions = asyncio.Queue()
        self.ended = asyncio.Queue()
        self.writer = None
        self.connections = set()

        async def connected(reader, writer):
            self.connections.add(asyncio.current_task())
            self.writer = writer
            try:
                await write_message(writer, RemoteMessage(remote_configure={"code1": 623}))
                while True:
                    message = await read_message(reader)
                    self.received.append(message)
                    await self.messages.put(message)
                    if message.HasField("remote_configure"):
                        await write_message(writer, RemoteMessage(remote_start={"started": True}))
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_server(connected, "127.0.0.1", 0, ssl=context)
        self.port = self.server.sockets[0].getsockname()[1]
        self.certificate, self.key = certificate, key
        self.client = VoiceRemote(
            "voice fixture", str(certificate), str(key), "127.0.0.1",
            api_port=self.port, enable_ime=False, enable_voice=True,
            voice_requested=self.sessions.put_nowait, voice_ended=self.ended.put_nowait,
        )
        await asyncio.wait_for(self.client.async_connect(), 2)

    def capture_fixture(self):
        tools = self.base / "tools"
        tools.mkdir()
        executable = tools / "pw-record"
        executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""
            import json, os, signal, sys, time
            from pathlib import Path
            base = Path(os.environ["VOICE_FIXTURE"])
            (base / "arguments.json").write_text(json.dumps(sys.argv[1:]))
            if os.environ.get("VOICE_CAPTURE_FAIL"):
                sys.exit("fixture microphone capture failed")
            def stop(*args):
                (base / "capture-stopped").write_text("stopped")
                sys.exit(0)
            signal.signal(signal.SIGINT, stop)
            audio = b"\\x01\\x00" * 10240
            for start in range(0, len(audio), 137):
                os.write(1, audio[start:start + 137])
            while True:
                time.sleep(1)
        """))
        executable.chmod(0o755)
        pactl = tools / "pactl"
        pactl.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""
            import json, os, sys
            from pathlib import Path
            base = Path(os.environ["VOICE_FIXTURE"])
            (base / "pactl-arguments.json").write_text(json.dumps(sys.argv[1:]))
            if sys.argv[1:] != ["--format=json", "list", "sources"]:
                sys.exit("Voice forwarding must not change the default microphone")
            print(json.dumps([] if os.environ.get("VOICE_NO_MIC") else [
                {"name": "atvvoice-shield-remote"},
            ]))
        """))
        pactl.chmod(0o755)
        environment = patch.dict(os.environ, {
            "PATH": f"{tools}:{os.environ['PATH']}", "VOICE_FIXTURE": str(self.base),
        })
        environment.start()
        self.addCleanup(environment.stop)

    async def connect_router(self, target="shield"):
        self.client.disconnect()
        paths = router.Paths(self.base / "config", self.base / "runtime")
        paths.prepare()
        (paths.config / "client.crt").write_bytes(self.certificate.read_bytes())
        (paths.config / "client.key").write_bytes(self.key.read_bytes())
        backend = router.Router(paths, router.Config(
            remote_address="00:11:22:33:44:55", shield_host="127.0.0.1", paired_host="127.0.0.1",
        ))
        backend.target = target
        backend.input_captured = True
        backend.notice = AsyncMock()
        with patch.object(router, "VoiceRemote", partial(VoiceRemote, api_port=self.port)):
            self.client = backend.new_client()
        backend.client = self.client
        await asyncio.wait_for(self.client.async_connect(), 2)
        backend.shield_connected = True
        self.addAsyncCleanup(backend.close_tv_voice)
        return backend

    async def asyncTearDown(self):
        self.client.disconnect()
        self.server.close()
        await self.server.wait_closed()
        if self.writer:
            self.writer.close()
        await asyncio.wait_for(asyncio.gather(*self.connections), 2)

    async def next_message(self, field):
        async with asyncio.timeout(2):
            while True:
                message = await self.messages.get()
                if message.HasField(field):
                    return getattr(message, field)

    async def test_app_requested_voice_streams_audio_without_search_or_assistant_key(self):
        self.assertTrue(self.client.is_voice_enabled)
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        session = await asyncio.wait_for(self.sessions.get(), 2)
        acknowledgement = await self.next_message("remote_voice_begin")
        self.assertEqual(acknowledgement.session_id, 41)
        audio = b"\x01\x00" * 4096
        self.assertTrue(session.send_chunk(audio))
        payload = await self.next_message("remote_voice_payload")
        self.assertEqual(payload.session_id, 41)
        self.assertEqual(payload.samples, audio)
        self.assertFalse(any(message.HasField("remote_key_inject") for message in self.received))
        session.end()

    async def test_tv_end_closes_only_its_session_and_allows_a_fresh_request(self):
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        first = await asyncio.wait_for(self.sessions.get(), 2)
        await self.next_message("remote_voice_begin")
        await write_message(self.writer, RemoteMessage(remote_voice_end={"session_id": 99}))
        await asyncio.sleep(0.01)
        self.assertTrue(self.ended.empty())
        await write_message(self.writer, RemoteMessage(remote_voice_end={"session_id": 41}))
        self.assertIs(await asyncio.wait_for(self.ended.get(), 2), first)
        self.assertFalse(first.send_chunk(b"\x01\x00" * 4096))
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        second = await asyncio.wait_for(self.sessions.get(), 2)
        self.assertIsNot(first, second)
        second.end()

    async def test_local_end_allows_a_new_app_request_even_with_the_same_session_id(self):
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        first = await asyncio.wait_for(self.sessions.get(), 2)
        first.end()
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        second = await asyncio.wait_for(self.sessions.get(), 2)
        self.assertIsNot(first, second)
        second.end()

    async def test_named_microphone_uses_full_20k_pcm_packets_and_closes_its_process(self):
        self.capture_fixture()
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        session = await asyncio.wait_for(self.sessions.get(), 2)
        capture = asyncio.create_task(tv_voice.stream_microphone(session, "atvvoice-shield-remote"))
        try:
            payload = await self.next_message("remote_voice_payload")
            self.assertEqual(payload.samples, b"\x01\x00" * 10240)
            arguments = json.loads((self.base / "arguments.json").read_text())
            self.assertIn("--target=atvvoice-shield-remote", arguments)
            self.assertIn("--rate=8000", arguments)
            self.assertIn("--channels=1", arguments)
            self.assertIn("--format=s16", arguments)
            self.assertIn("--raw", arguments)
            properties = json.loads(arguments[arguments.index("--properties") + 1])
            self.assertIs(properties["node.dont-fallback"], True)
            self.assertIs(properties["node.dont-reconnect"], True)
        finally:
            capture.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await capture
            session.end()
        self.assertTrue((self.base / "capture-stopped").exists())

    async def test_router_feeds_the_app_prompt_and_stops_when_the_tv_ends_it(self):
        self.capture_fixture()
        backend = await self.connect_router()
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        payload = await self.next_message("remote_voice_payload")
        self.assertEqual(payload.samples, b"\x01\x00" * 10240)
        self.assertTrue(backend.state()["shield"]["voice_active"])
        self.assertFalse(any(message.HasField("remote_key_inject") for message in self.received))
        await write_message(self.writer, RemoteMessage(remote_voice_end={"session_id": 41}))
        async with asyncio.timeout(2):
            while backend.state()["shield"]["voice_active"]:
                await asyncio.sleep(0.01)
        self.assertTrue((self.base / "capture-stopped").exists())
        backend.notice.assert_not_awaited()

    async def test_computer_mode_rejects_tv_listening_without_opening_any_microphone(self):
        self.capture_fixture()
        backend = await self.connect_router("computer")
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        ended = await self.next_message("remote_voice_end")
        self.assertEqual(ended.session_id, 41)
        self.assertFalse(backend.state()["shield"]["voice_active"])
        self.assertFalse((self.base / "arguments.json").exists())
        self.assertFalse((self.base / "pactl-arguments.json").exists())
        self.assertFalse(any(message.HasField("remote_key_inject") for message in self.received))

    async def test_uncaptured_remote_cannot_start_tv_audio(self):
        self.capture_fixture()
        backend = await self.connect_router()
        backend.input_captured = False
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        await self.next_message("remote_voice_end")
        self.assertFalse((self.base / "arguments.json").exists())
        self.assertFalse(backend.state()["shield"]["voice_active"])

    async def test_missing_remote_source_is_an_error_not_a_headset_fallback(self):
        self.capture_fixture()
        backend = await self.connect_router()
        with patch.dict(os.environ, {"VOICE_NO_MIC": "1"}):
            await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
            await self.next_message("remote_voice_end")
        self.assertFalse((self.base / "arguments.json").exists())
        self.assertIn("Shield microphone is unavailable", backend.state()["error"])
        backend.notice.assert_awaited_once()

    async def test_target_change_waits_for_capture_cleanup(self):
        self.capture_fixture()
        backend = await self.connect_router()
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        await self.next_message("remote_voice_payload")
        state = await backend.request({"command": "target", "target": "computer"})
        self.assertEqual(state["target"], "computer")
        self.assertFalse(state["shield"]["voice_active"])
        self.assertTrue((self.base / "capture-stopped").exists())
        await self.next_message("remote_voice_end")

    async def test_tv_disconnect_stops_the_capture_process(self):
        self.capture_fixture()
        backend = await self.connect_router()
        await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
        await self.next_message("remote_voice_payload")
        self.writer.close()
        await self.writer.wait_closed()
        async with asyncio.timeout(2):
            while backend.state()["shield"]["voice_active"]:
                await asyncio.sleep(0.01)
        self.assertTrue((self.base / "capture-stopped").exists())

    async def test_capture_error_ends_the_session_and_reports_the_failure(self):
        self.capture_fixture()
        backend = await self.connect_router()
        with patch.dict(os.environ, {"VOICE_CAPTURE_FAIL": "1"}):
            await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
            await self.next_message("remote_voice_end")
        self.assertIn("fixture microphone capture failed", backend.state()["error"])
        self.assertFalse(backend.state()["shield"]["voice_active"])
        backend.notice.assert_awaited_once()

    async def test_tv_listening_has_a_thirty_second_safety_limit(self):
        self.assertEqual(router.TV_VOICE_TIMEOUT, 30)
        self.capture_fixture()
        backend = await self.connect_router()
        with patch.object(router, "TV_VOICE_TIMEOUT", 0.5):
            await write_message(self.writer, RemoteMessage(remote_voice_begin={"session_id": 41}))
            await self.next_message("remote_voice_payload")
            await self.next_message("remote_voice_end")
        self.assertFalse(backend.state()["shield"]["voice_active"])
        self.assertTrue((self.base / "capture-stopped").exists())
        backend.notice.assert_awaited_once_with("Shield microphone stopped after 0.5 seconds")


if __name__ == "__main__":
    unittest.main()
