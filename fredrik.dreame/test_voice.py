import array
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from backend import Backend, Store
from cloud import CloudError
from test_cloud import PROFILE, SESSION
from test_programs import Client, PROGRAM_ID
import voice


class VoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "private")
        self.store.write("profile.json", PROFILE)
        self.store.write("session.json", SESSION)
        self.store.write("settings.json", {"selectedDid": "123", "mutationsEnabled": False})
        self.backend = Backend(self.store, now=lambda: 1000)
        self.backend.client = Mock(return_value=Mock(session=dict(SESSION)))
        self.backend.request = Mock(return_value={"program": {"phase": "queued"}})
        self.audio = self.root / "request.wav"
        self.wav()

    def wav(self, frames=16000, rate=16000, sample=100):
        with wave.open(str(self.audio), "wb") as file:
            file.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            file.writeframes(array.array("h", [sample] * frames).tobytes())
        self.audio.chmod(0o600)

    def test_audio_is_private_and_handed_to_existing_program_route_once(self):
        def transcribe(recording):
            self.assertNotEqual(recording, self.audio)
            self.assertEqual(recording.stat().st_mode & 0o777, 0o600)
            self.assertEqual(recording.read_bytes(), self.audio.read_bytes())
            self.assertEqual(self.store.read("voice.json")["phase"], "transcribing")
            self.recording = recording
            return "I just cooked, clean up."
        result = voice.receive_audio(self.backend, self.audio, transcriber=transcribe)
        self.assertTrue(result["ok"])
        self.backend.request.assert_called_once_with({
            "op": "start-program", "deviceId": "123", "prompt": "I just cooked, clean up.",
        })
        self.assertFalse(self.recording.exists())
        self.assertEqual(self.store.read("voice.json")["phase"], "accepted")

    def test_invalid_format_duration_truncation_and_silence_never_reach_planner(self):
        for kwargs in ({"frames": 7999}, {"frames": 480001}, {"rate": 8000}, {"sample": 0}):
            with self.subTest(kwargs=kwargs):
                self.wav(**kwargs)
                transcriber = Mock()
                with self.assertRaises(CloudError):
                    voice.receive_audio(self.backend, self.audio, transcriber=transcriber)
                transcriber.assert_not_called()
                self.backend.request.assert_not_called()
                self.assertEqual(self.store.read("voice.json")["phase"], "failed")
        self.wav()
        self.audio.write_bytes(self.audio.read_bytes()[:-10])
        with self.assertRaisesRegex(CloudError, "truncated"):
            voice.read_audio(self.audio)

    def test_links_public_permissions_fifo_and_oversize_are_rejected(self):
        link = self.root / "linked.wav"
        link.symlink_to(self.audio)
        with self.assertRaises(CloudError):
            voice.read_audio(link)
        link.unlink()
        os.link(self.audio, link)
        with self.assertRaises(CloudError):
            voice.read_audio(self.audio)
        link.unlink()
        self.audio.chmod(0o644)
        with self.assertRaises(CloudError):
            voice.read_audio(self.audio)
        self.audio.unlink()
        os.mkfifo(self.audio, 0o600)
        with self.assertRaises(CloudError):
            voice.read_audio(self.audio)
        self.audio.unlink()
        self.audio.write_bytes(b"x" * 1_100_001)
        self.audio.chmod(0o600)
        with self.assertRaises(CloudError):
            voice.read_audio(self.audio)

    def test_account_or_device_change_during_transcription_prevents_submission(self):
        for change in ("account", "device"):
            for prompt in ("Clean the kitchen", "Stop"):
                with self.subTest(change=change, prompt=prompt):
                    self.store.write("session.json", SESSION)
                    self.store.write("settings.json", {"selectedDid": "123", "mutationsEnabled": False})
                    def transcribe(_):
                        if change == "account":
                            self.store.write("session.json", {**SESSION, "fingerprint": "another-session"})
                        else:
                            self.store.write("settings.json", {"selectedDid": "456", "mutationsEnabled": False})
                        return prompt
                    with self.assertRaisesRegex(CloudError, "changed during transcription"):
                        voice.receive_audio(self.backend, self.audio, transcriber=transcribe)
                    self.backend.request.assert_not_called()

    def test_invalid_or_failed_transcript_never_submits(self):
        for value in (None, "", " ", "x" * 4001, ["not text"]):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(CloudError):
                    voice.receive_audio(self.backend, self.audio, transcriber=lambda _: value)
                self.backend.request.assert_not_called()
        with self.assertRaises(CloudError):
            voice.receive_audio(self.backend, self.audio, transcriber=Mock(side_effect=CloudError("NO_SPEECH", "No speech")))
        self.assertEqual(self.store.read("voice.json")["phase"], "failed")

    def test_duplicate_intake_is_not_queued_or_retried(self):
        with self.store.lock(name="voice.lock"):
            with self.assertRaises(CloudError):
                voice.receive_audio(self.backend, self.audio, transcriber=Mock())
        self.backend.request.assert_not_called()

    def transcribe_output(self, output, code=0, engine="whisper", error=None):
        config = self.root / "voxtype"
        config.mkdir(exist_ok=True)
        (config / "config.toml").write_text(f'engine = "{engine}"\n')
        def run(command, **options):
            self.assertNotIn("--vad", command)
            self.assertEqual(command[command.index("--config") + 1], str(config / "config.toml"))
            self.assertEqual(command[command.index("--whisper-mode") + 1], "local")
            options["stdout"].write(output)
            return Mock(returncode=code)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}), patch.object(
            voice.subprocess, "run", side_effect=error or run,
        ) as process:
            result = voice.transcribe(self.audio)
        return result, process

    def test_voxtype_metadata_is_not_part_of_request(self):
        result, process = self.transcribe_output(
            b'Loading audio file: "private.wav"\nAudio format: 16000 Hz, 1 channel(s), Int\nProcessing 16000 samples (1.0s)...\n\nClean the kitchen.\n',
        )
        self.assertEqual(result, "Clean the kitchen.")
        self.assertEqual(process.call_count, 1)

    def test_voxtype_errors_no_speech_and_cloud_engines_are_explicit(self):
        for options in (
            {"output": b"Loading audio file: a.wav\nProcessing samples\nNo speech detected, skipping transcription.\n"},
            {"output": b"not the expected format"},
            {"output": b"Loading audio file: x\nProcessing x\n\n\xff"},
            {"output": b"", "code": 1},
            {"output": b"", "error": subprocess.TimeoutExpired("voxtype", 90)},
            {"output": b"", "engine": "soniox"},
        ):
            with self.subTest(options=options.keys()), self.assertRaises(CloudError):
                self.transcribe_output(**options)
        self.backend.request.assert_not_called()

    def test_abandoned_transcription_is_reported_without_replay(self):
        for phase in ("transcribing", "stopping"):
            with self.subTest(phase=phase):
                self.store.write("voice.json", {"phase": phase, "updatedAt": 0})
                self.assertEqual(voice.public_voice(self.store, 1000)["phase"], "failed")
        self.backend.request.assert_not_called()

    def test_spoken_request_uses_real_program_preflight_and_queues_without_mutations(self):
        from test_native import snapshot
        from test_programs import Client
        client = Client()
        self.backend.client = lambda: client
        self.backend.request = Backend.request.__get__(self.backend)
        with patch("programs.native.read_snapshot", return_value=snapshot()), patch(
            "programs.subprocess.run", return_value=Mock(returncode=0),
        ) as launch, patch("programs.unit_running", return_value=True):
            result = voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Clean the kitchen")
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.read("program.json")["inputText"], "Clean the kitchen")
        self.assertEqual(self.store.read("program.json")["phase"], "queued")
        self.assertEqual(client.calls, [])
        launch.assert_called_once()

    def current_program(self, phase="running", dispatched=True):
        self.client = Client()
        self.client.state.update(state=1, task=3)
        self.backend.client = lambda: self.client
        self.backend.request = Mock(wraps=Backend.request.__get__(self.backend))
        self.store.write("program.json", {
            "id": PROGRAM_ID, "phase": phase, "deviceId": "123",
            "fingerprint": SESSION["fingerprint"], "createdAt": 900, "updatedAt": 1000,
            "stepIndex": 0, "stepCount": 1, "dispatched": dispatched,
            "inputText": "Clean the kitchen",
        })

    def test_spoken_stop_ends_active_cleaning_and_requests_dock_without_planning(self):
        self.current_program()
        def stop_worker(*args, **kwargs):
            self.assertEqual(voice.public_voice(self.store, 1000)["phase"], "stopping")
            return Mock(returncode=0)
        with patch("programs.subprocess.run", side_effect=stop_worker) as worker, patch(
            "programs.unit_running", return_value=True,
        ), patch("programs.native.read_snapshot") as read_status:
            result = voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop.")
        self.assertTrue(result["ok"])
        self.assertIn("return to dock", result["message"])
        self.assertEqual(self.client.control_calls, [(4, 2), (3, 1)])
        self.assertEqual(self.client.calls, [])
        self.backend.request.assert_not_called()
        read_status.assert_not_called()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0], [
            "systemctl", "--user", "stop", "fredrik-dreame-" + PROGRAM_ID,
        ])
        self.assertEqual(self.store.read("program.json")["phase"], "cancelled")
        self.assertEqual(self.store.read("voice.json")["phase"], "accepted")
        self.assertFalse(result["data"]["physicalOutcomeVerified"])

    def test_repeated_spoken_stop_does_not_replay_robot_actions(self):
        self.current_program()
        with patch("programs.subprocess.run", return_value=Mock(returncode=0)):
            for attempt in range(2):
                result = voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop")
                self.assertTrue(result["ok"])
                if attempt:
                    self.assertIn("already requested", result["message"])
        self.assertEqual(self.client.control_calls, [(4, 2), (3, 1)])
        self.backend.request.assert_not_called()

    def test_spoken_stop_before_dispatch_cancels_without_movement(self):
        self.current_program(phase="planning", dispatched=False)
        with patch("programs.subprocess.run", return_value=Mock(returncode=0)):
            result = voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Please stop cleaning.")
        self.assertTrue(result["ok"])
        self.assertIn("before dispatch", result["message"])
        self.assertEqual(self.client.control_calls, [])
        self.backend.request.assert_not_called()
        self.assertEqual(self.store.read("program.json")["phase"], "cancelled")

    def test_spoken_stop_without_a_program_is_an_explicit_error(self):
        with patch("programs.subprocess.run") as worker:
            with self.assertRaises(CloudError) as error:
                voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop")
        self.assertEqual(error.exception.code, "PROGRAM_NOT_ACTIVE")
        self.backend.request.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(self.store.read("voice.json")["phase"], "failed")

    def test_spoken_stop_of_a_finished_program_does_not_move_the_robot(self):
        for phase in ("completed", "clarification"):
            with self.subTest(phase=phase):
                self.current_program(phase=phase)
                with patch("programs.subprocess.run", return_value=Mock(returncode=0)):
                    with self.assertRaises(CloudError) as error:
                        voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop")
                self.assertEqual(error.exception.code, "PROGRAM_NOT_ACTIVE")
                self.assertEqual(self.client.control_calls, [])
                self.backend.request.assert_not_called()

    def test_spoken_stop_does_not_control_another_program_context(self):
        for field, value in (("deviceId", "456"), ("fingerprint", "another-session")):
            with self.subTest(field=field):
                self.current_program()
                program = self.store.read("program.json")
                self.store.write("program.json", {**program, field: value})
                with patch("programs.subprocess.run") as worker:
                    with self.assertRaises(CloudError) as error:
                        voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop")
                self.assertEqual(error.exception.code, "PROGRAM_CONTEXT_CHANGED")
                self.assertEqual(self.client.control_calls, [])
                self.backend.request.assert_not_called()
                worker.assert_not_called()
                self.assertIsNone(self.store.read("program-cancel.json"))

    def test_context_change_while_stopping_worker_prevents_robot_commands(self):
        for change in ("account", "device"):
            with self.subTest(change=change):
                self.store.write("session.json", SESSION)
                self.store.write("settings.json", {"selectedDid": "123", "mutationsEnabled": False})
                self.current_program()
                def stop_worker(*args, **kwargs):
                    if change == "account":
                        self.store.write("session.json", {**SESSION, "fingerprint": "another-session"})
                    else:
                        self.store.write("settings.json", {"selectedDid": "456", "mutationsEnabled": False})
                    return Mock(returncode=0)
                with patch("programs.subprocess.run", side_effect=stop_worker):
                    with self.assertRaises(CloudError) as error:
                        voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop")
                self.assertEqual(error.exception.code, "PROGRAM_CONTEXT_CHANGED")
                self.assertEqual(self.client.control_calls, [])
                self.backend.request.assert_not_called()

    def test_ambiguous_voice_controls_are_not_replayed(self):
        for control in ("stop", "dock"):
            with self.subTest(control=control):
                self.current_program()
                setattr(self.client, control + "_result", {"code": 5})
                with patch("programs.subprocess.run", return_value=Mock(returncode=0)):
                    for _ in range(2):
                        with self.assertRaises(CloudError) as error:
                            voice.receive_audio(self.backend, self.audio, transcriber=lambda _: "Stop")
                        self.assertEqual(error.exception.code, control.upper() + "_UNVERIFIED")
                self.assertEqual(self.client.stop_calls, 1)
                self.assertEqual(self.client.dock_calls, 1 if control == "dock" else 0)
                self.backend.request.assert_not_called()

    def test_only_complete_explicit_stop_commands_are_control_requests(self):
        for text in (
            "stop", "STOP!", "Stop.", "  please stop, please. ", "Stop cleaning",
            "Stop the vacuum.", "Stop vacuuming", "End the task", "Cancel cleaning",
            "Stop and return to dock.",
        ):
            with self.subTest(text=text):
                self.assertTrue(voice.is_stop_request(text))
        for text in (
            "Don't stop", "Do not stop cleaning", "Never stop", "Stop after cleaning the kitchen",
            "Clean the kitchen, then stop", "Stop cleaning and vacuum the bedroom instead",
            "What happens when I say stop?", "Stop but don't dock", "Clean the kitchen",
        ):
            with self.subTest(text=text):
                self.assertFalse(voice.is_stop_request(text))
