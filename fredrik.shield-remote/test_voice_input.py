import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

import router
import voice_input


class VoiceInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.paths = router.Paths(self.base / "config", self.base / "runtime")
        self.paths.prepare()
        self.tools = self.base / "tools"
        self.tools.mkdir()
        capture = self.tools / "pw-record"
        capture.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""
            import json, os, signal, sys, time, wave
            from pathlib import Path
            base = Path(os.environ["AUDIO_FIXTURE"])
            (base / "capture.json").write_text(json.dumps(sys.argv[1:]))
            if os.environ.get("CAPTURE_FAIL"):
                sys.exit(1)
            print(sys.argv[-1], file=sys.stderr, flush=True)
            audio = wave.open(sys.argv[-1], "wb")
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\\x64\\x00" * 16000)
            audio._file.flush()
            def stop(*args):
                audio.close()
                if os.environ.get("CAPTURE_TRUNCATE"):
                    with open(sys.argv[-1], "r+b") as file:
                        file.truncate(file.seek(0, 2) - 100)
                if os.environ.get("CAPTURE_STOP_ERROR"):
                    print("error: failed to write recording", file=sys.stderr)
                (base / "stopped").write_text("yes")
                sys.exit(int(os.environ.get("CAPTURE_STOP_CODE", "0")))
            signal.signal(signal.SIGINT, stop)
            while True:
                time.sleep(1)
        """))
        capture.chmod(0o755)
        self.receiver = self.base / "receiver.py"
        self.receiver.write_text(textwrap.dedent("""
            import json, os, sys, time, wave
            from pathlib import Path
            base = Path(os.environ["AUDIO_FIXTURE"])
            audio = Path(sys.argv[-1])
            assert audio.stat().st_mode & 0o777 == 0o600
            with wave.open(str(audio)) as file:
                assert (file.getframerate(), file.getnchannels(), file.getsampwidth()) == (16000, 1, 2)
            (base / "received.json").write_text(json.dumps({"path": str(audio), "args": sys.argv[1:-1]}))
            if os.environ.get("RECEIVER_WAIT"):
                while True:
                    time.sleep(1)
            print(os.environ.get("RECEIVER_RESPONSE", '{"ok":true,"message":"Fixture accepted"}'))
        """))
        self.command = [sys.executable, str(self.receiver), "--generic-fixture"]
        self.config = self.paths.config / "voice-receiver.json"
        router.write_private_json(self.config, {"version": 1, "command": self.command})
        self.audio = voice_input.AudioInput(self.config, self.paths.runtime, AsyncMock(), AsyncMock())
        self.addAsyncCleanup(self.audio.close)
        environment = patch.dict(os.environ, {
            "PATH": f"{self.tools}:{os.environ['PATH']}", "AUDIO_FIXTURE": str(self.base),
        })
        environment.start()
        self.addCleanup(environment.stop)

    async def test_recording_uses_named_source_and_one_private_finalized_wav(self):
        await self.audio.start("atvvoice-shield-remote")
        self.assertEqual(self.audio.phase, "recording")
        self.assertTrue(self.audio.busy)
        with self.assertRaisesRegex(RuntimeError, "already recording"):
            await self.audio.start("atvvoice-shield-remote")
        args = json.loads((self.base / "capture.json").read_text())
        self.assertIn("--target=atvvoice-shield-remote", args)
        self.assertIn("--rate=16000", args)
        self.assertIn('{"node.dont-fallback":true,"node.dont-reconnect":true}', args)
        self.assertFalse((self.base / "received.json").exists())
        self.audio.finish()
        await asyncio.wait_for(self.audio.task, 3)
        self.assertEqual(self.audio.phase, "accepted")
        self.audio.error.assert_not_awaited()
        received = json.loads((self.base / "received.json").read_text())
        self.assertEqual(received["args"], ["--generic-fixture"])
        self.assertFalse(Path(received["path"]).exists())
        self.audio.notice.assert_awaited_with("Fixture accepted")
        self.assertEqual(list(self.paths.runtime.glob("voice-*")), [])

    async def test_timeout_cancels_instead_of_submitting_truncated_request(self):
        wait = asyncio.wait
        async def expire(tasks, **kwargs):
            return await wait(tasks, **{**kwargs, "timeout": 0.01})
        with patch.object(voice_input.asyncio, "wait", side_effect=expire):
            await self.audio.start("atvvoice-shield-remote")
            await asyncio.wait_for(self.audio.task, 3)
        self.assertEqual(self.audio.phase, "failed")
        self.assertFalse((self.base / "received.json").exists())
        self.assertIn("30-second limit", self.audio.error.await_args.args[0])
        self.assertEqual(list(self.paths.runtime.glob("voice-*")), [])

    async def test_pipewire_sigint_exit_one_with_complete_wav_is_delivered(self):
        with patch.dict(os.environ, {"CAPTURE_STOP_CODE": "1"}):
            await self.audio.start("atvvoice-shield-remote")
            self.audio.finish()
            await asyncio.wait_for(self.audio.task, 3)
        self.assertEqual(self.audio.phase, "accepted")
        self.audio.error.assert_not_awaited()
        self.assertTrue((self.base / "received.json").exists())

    async def test_recorder_errors_are_not_treated_as_clean_sigint(self):
        for environment in (
            {"CAPTURE_STOP_CODE": "1", "CAPTURE_STOP_ERROR": "1"},
            {"CAPTURE_STOP_CODE": "2"},
        ):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                await self.audio.start("atvvoice-shield-remote")
                self.audio.finish()
                await asyncio.wait_for(self.audio.task, 3)
                self.assertEqual(self.audio.phase, "failed")
                self.assertFalse((self.base / "received.json").exists())

    async def test_truncated_wav_is_never_sent_after_an_explicit_stop(self):
        for code in ("0", "1"):
            with self.subTest(code=code), patch.dict(os.environ, {"CAPTURE_STOP_CODE": code, "CAPTURE_TRUNCATE": "1"}):
                (self.base / "received.json").unlink(missing_ok=True)
                await self.audio.start("atvvoice-shield-remote")
                self.audio.finish()
                await asyncio.wait_for(self.audio.task, 3)
                self.assertEqual(self.audio.phase, "failed")
                self.assertFalse((self.base / "received.json").exists())

    async def test_disconnect_or_shutdown_cleans_recording_without_delivery(self):
        await self.audio.start("atvvoice-shield-remote")
        await self.audio.close()
        self.assertFalse(self.audio.busy)
        self.assertFalse((self.base / "received.json").exists())
        self.assertEqual(list(self.paths.runtime.glob("voice-*")), [])

    async def test_capture_failure_is_explicit_and_does_not_fall_back(self):
        with patch.dict(os.environ, {"CAPTURE_FAIL": "1"}):
            with self.assertRaises(RuntimeError):
                await self.audio.start("atvvoice-shield-remote")
            await self.audio.task
        self.audio.error.assert_awaited_once()
        self.assertFalse((self.base / "received.json").exists())

    async def test_ambiguous_acknowledgement_is_not_retried(self):
        with patch.dict(os.environ, {"RECEIVER_RESPONSE": "not json"}):
            await self.audio.start("atvvoice-shield-remote")
            self.audio.finish()
            await asyncio.wait_for(self.audio.task, 3)
        self.assertEqual(self.audio.phase, "failed")
        self.assertIn("not replayed", self.audio.error.await_args.args[0])
        self.assertTrue((self.base / "received.json").exists())

    async def test_receiver_rejection_is_visible(self):
        with patch.dict(os.environ, {"RECEIVER_RESPONSE": '{"ok":false,"error":{"message":"Choose a vacuum first"}}'}):
            await self.audio.start("atvvoice-shield-remote")
            self.audio.finish()
            await asyncio.wait_for(self.audio.task, 3)
        self.assertEqual(self.audio.phase, "failed")
        self.assertIn("Choose a vacuum first", self.audio.error.await_args.args[0])

    async def test_malformed_success_message_does_not_look_like_an_accepted_request(self):
        with patch.dict(os.environ, {"RECEIVER_RESPONSE": '{"ok":true,"message":7}'}):
            await self.audio.start("atvvoice-shield-remote")
            self.audio.finish()
            await asyncio.wait_for(self.audio.task, 3)
        self.assertEqual(self.audio.phase, "failed")
        self.assertIn("not replayed", self.audio.error.await_args.args[0])

    async def test_shutdown_reaps_an_owned_receiver_process(self):
        with patch.dict(os.environ, {"RECEIVER_WAIT": "1"}):
            await self.audio.start("atvvoice-shield-remote")
            self.audio.finish()
            async with asyncio.timeout(3):
                while not (self.base / "received.json").exists():
                    await asyncio.sleep(0.01)
            await self.audio.close()
        self.assertFalse(self.audio.busy)
        self.assertEqual(list(self.paths.runtime.glob("voice-*")), [])

    async def test_receiver_configuration_is_separate_from_target_configuration(self):
        backend = router.Router(self.paths, router.Config())
        original = backend.config
        backend.notice = AsyncMock()
        await backend.mutate({"command": "configure_voice", "receiver": self.command})
        self.assertEqual(backend.config, original)
        self.assertEqual(voice_input.receiver_command(self.config), self.command)
        for invalid in ("bash -c command", [], ["relative-path"], [sys.executable, 7]):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, RuntimeError)):
                await backend.mutate({"command": "configure_voice", "receiver": invalid})
        self.assertEqual(voice_input.receiver_command(self.config), self.command)
