"""Private WAV intake and local Voxtype transcription; no microphone ownership."""

import array
import io
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import wave

from cloud import CloudError
from programs import ProgramService, read_program


def is_stop_request(text):
    words = " ".join(re.sub(r"[.!?,;:]", " ", text.casefold()).split())
    words = words.removeprefix("please ").removesuffix(" please")
    words = re.sub(r" and (?:return to (?:the )?(?:dock|base|charging station)|go home)$", "", words)
    return words in {
        "stop", "stop it", "stop cleaning", "stop the cleaning", "stop vacuuming",
        "stop mopping", "stop the vacuum", "stop the vacuum cleaner", "stop the robot",
        "end cleaning", "end the cleaning", "end task", "end the task",
        "cancel cleaning", "cancel the cleaning", "cancel task", "cancel the task",
    }


def public_voice(store, now):
    value = store.read("voice.json")
    if value is None:
        return None
    if (value.get("phase") not in ("transcribing", "stopping", "accepted", "failed")
            or type(value.get("updatedAt")) not in (int, float) or value["updatedAt"] < 0):
        raise CloudError("INVALID_STORAGE", "The voice-input state is invalid.")
    result = {key: value[key] for key in ("phase", "updatedAt", "error") if key in value}
    if value["phase"] in ("transcribing", "stopping") and now - value["updatedAt"] > 300:
        result.update(phase="failed", error={"message": "Voice input was interrupted. Check the current program before trying again; audio is never replayed."})
    return result


def read_audio(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise CloudError("INVALID_AUDIO", "Audio must be a private, user-owned regular file without links.")
            raw = source.read(1_100_001)
        if len(raw) > 1_100_000:
            raise CloudError("INVALID_AUDIO", "Voice input exceeds the 30-second WAV limit.")
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, "NONE"):
                raise CloudError("INVALID_AUDIO", "Supply 16 kHz mono signed-16-bit PCM WAV audio.")
            frames = audio.getnframes()
            if not 8000 <= frames <= 480000:
                raise CloudError("INVALID_AUDIO", "Record between half a second and 30 seconds.")
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2:
                raise CloudError("INVALID_AUDIO", "The audio recording is truncated.")
        samples = array.array("h", pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        if math.sqrt(sum(sample * sample for sample in samples) / len(samples)) < 15:
            raise CloudError("NO_SPEECH", "No usable speech was recorded. Nothing was sent to Luna.")
        return raw
    except (OSError, ValueError, EOFError, wave.Error) as error:
        raise CloudError("INVALID_AUDIO", "The private WAV recording could not be read or validated.") from error


def transcribe(path):
    config_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxtype/config.toml"
    try:
        config = tomllib.loads(config_path.read_text())
    except (OSError, ValueError) as error:
        raise CloudError("VOICE_UNAVAILABLE", "Configure local Voxtype before submitting audio.") from error
    engine = config.get("engine", "whisper")
    if engine != "whisper":
        raise CloudError("VOICE_NOT_LOCAL", "Voice commands currently require Voxtype's local Whisper engine.")
    command = ["voxtype", "--config", str(config_path), "--quiet", "--whisper-mode", "local", "transcribe", str(path)]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=90,
                                    env={**os.environ, "RUST_LOG": "error", "NO_COLOR": "1"})
        except subprocess.TimeoutExpired as error:
            raise CloudError("VOICE_TIMEOUT", "Local transcription timed out. No cleaning request was sent.") from error
        except OSError as error:
            raise CloudError("VOICE_UNAVAILABLE", "Voxtype could not be started. No cleaning request was sent.") from error
        if result.returncode:
            raise CloudError("TRANSCRIPTION_FAILED", "Local Voxtype transcription failed; check the configured local model.")
        if stdout.tell() > 65536:
            raise CloudError("TRANSCRIPTION_FAILED", "Voxtype returned an oversized response.")
        stdout.seek(0)
        try:
            output = stdout.read().decode("utf-8")
        except UnicodeError as error:
            raise CloudError("TRANSCRIPTION_FAILED", "Voxtype returned an invalid text response.") from error
    # Voxtype 1.1 prints file metadata before a blank line, then the transcript.
    metadata, separator, text = output.partition("\n\n")
    text = text.strip()
    if not output.startswith("Loading audio file:") or "Processing " not in metadata:
        raise CloudError("TRANSCRIPTION_FAILED", "Voxtype returned an unsupported file-transcription response.")
    if not separator or not text:
        raise CloudError("NO_SPEECH", "No speech was recognized. Nothing was sent to Luna.")
    if len(text) > 4000:
        raise CloudError("INVALID_AUDIO", "The spoken request exceeds 4000 characters.")
    return text


def receive_audio(backend, path, *, transcriber=transcribe):
    store = backend.store
    with store.lock(name="voice.lock"):
        with store.lock(wait_seconds=30):
            client = backend.client()
            fingerprint = client.session["fingerprint"]
            did = backend.settings()["selectedDid"]
            if not did:
                raise CloudError("DEVICE_REQUIRED", "Choose a vacuum in Dreame before sending a voice command.")
            store.write("voice.json", {"phase": "transcribing", "updatedAt": backend.now()})
        try:
            raw = read_audio(path)
            with tempfile.TemporaryDirectory(prefix="voice-", dir=store.root) as directory:
                recording = Path(directory) / "request.wav"
                with recording.open("xb") as target:
                    os.fchmod(target.fileno(), 0o600)
                    target.write(raw)
                text = transcriber(recording)
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise CloudError("TRANSCRIPTION_FAILED", "Local transcription did not return a usable request.")
            program_id = None
            with store.lock(wait_seconds=30):
                session = store.read("session.json")
                if not session or session.get("fingerprint") != fingerprint or backend.settings()["selectedDid"] != did:
                    raise CloudError("VOICE_CONTEXT_CHANGED", "The account or selected vacuum changed during transcription. Nothing was submitted.")
                if is_stop_request(text):
                    program = read_program(store)
                    if program is None:
                        raise CloudError("PROGRAM_NOT_ACTIVE", "There is no cleaning program to end. No robot command was sent.")
                    program_id = program["id"]
                    store.write("voice.json", {"phase": "stopping", "updatedAt": backend.now()})
                else:
                    data = backend.request({"op": "start-program", "deviceId": did, "prompt": text})
                    message = "Dreame received your voice request. Luna is planning the cleaning."
            # Cancellation owns the operation lock after terminating the worker.
            if program_id is not None:
                data = ProgramService(backend).cancel(program_id, expected_context={
                    "fingerprint": fingerprint, "deviceId": did,
                })
                if data["dockAcknowledged"]:
                    message = ("Return to dock was already requested for this program. No robot command was replayed."
                               if program.get("dockAcknowledged")
                               else "Cleaning queue cancelled; return to dock requested.")
                else:
                    message = "Cleaning request cancelled before dispatch. No robot movement was requested."
            store.write("voice.json", {"phase": "accepted", "updatedAt": backend.now()})
            return {"ok": True, "message": message, "data": data, "state": backend.state()}
        except (CloudError, OSError) as error:
            failure = error if isinstance(error, CloudError) else CloudError(
                "VOICE_IO", "Voice processing could not complete. Check the current program before retrying; nothing was replayed.",
            )
            store.write("voice.json", {"phase": "failed", "updatedAt": backend.now(),
                                       "error": {"code": failure.code, "message": str(failure)}})
            if failure is error:
                raise
            raise failure from error
