"""Mode-independent recording with one configurable, non-retrying WAV receiver."""

import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import wave


def receiver_command(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError("The audio receiver configuration must be a private, user-owned regular file")
    if info.st_size > 8192:
        raise ValueError("Audio receiver configuration is too large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {"version", "command"} or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported audio receiver configuration")
    command = value["command"]
    if (not isinstance(command, list) or not 1 <= len(command) <= 16
            or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)
            or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()
            or not os.access(command[0], os.X_OK)):
        raise ValueError("Configure an executable audio receiver command, not a shell expression")
    return command


def validate_recording(path):
    try:
        with wave.open(str(path), "rb") as recording:
            if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate(), recording.getcomptype()) != (1, 2, 16000, "NONE"):
                raise RuntimeError("The microphone returned an unsupported WAV format; nothing was sent")
            frames = recording.getnframes()
            if not 8000 <= frames <= 480000:
                raise RuntimeError("Record between half a second and 30 seconds; nothing was sent")
            if len(recording.readframes(frames)) != frames * 2:
                raise RuntimeError("The microphone recording is incomplete; nothing was sent")
    except (EOFError, wave.Error) as error:
        raise RuntimeError("The microphone WAV could not be finalized; nothing was sent") from error


class AudioInput:
    def __init__(self, config, runtime, notice, error):
        self.config, self.runtime = config, runtime
        self.notice, self.error = notice, error
        self.phase = "idle"
        self.failure = ""
        self.task = None
        self.finish_requested = asyncio.Event()
        self.ready = asyncio.Event()

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    async def start(self, source):
        if self.busy:
            raise RuntimeError("A voice command is already recording or being sent")
        try:
            command = receiver_command(self.config)
        except FileNotFoundError as error:
            raise RuntimeError("Configure an audio receiver before using double-press Speak") from error
        self.finish_requested = asyncio.Event()
        self.ready = asyncio.Event()
        self.phase = "starting"
        self.failure = ""
        self.task = asyncio.create_task(self.run(source, command))
        await self.ready.wait()
        if self.phase != "recording":
            raise RuntimeError(self.failure or "The remote microphone could not start recording")

    def finish(self):
        if self.phase != "recording":
            raise RuntimeError("No voice command is recording")
        self.phase = "sending"
        self.finish_requested.set()

    async def close(self):
        if self.busy:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.phase = "idle"

    async def run(self, source, command):
        process = None
        reader = None
        stop = None
        try:
            with tempfile.TemporaryDirectory(prefix="voice-", dir=self.runtime) as directory:
                audio = Path(directory) / "request.wav"
                with audio.open("xb") as file:
                    os.fchmod(file.fileno(), 0o600)
                started = asyncio.get_running_loop().time()
                process = await asyncio.create_subprocess_exec(
                    "pw-record", f"--target={source}", "--rate=16000", "--channels=1", "--format=s16",
                    "--properties", '{"node.dont-fallback":true,"node.dont-reconnect":true}', str(audio),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                reader = asyncio.create_task(process.communicate())
                for _ in range(75):
                    if reader.done():
                        raise RuntimeError("The remote microphone stopped before recording began")
                    if audio.stat().st_size > 44:
                        break
                    await asyncio.sleep(0.04)
                else:
                    raise RuntimeError("The remote microphone did not deliver audio")
                self.phase = "recording"
                self.ready.set()
                await self.notice("Listening for a voice command. Press Speak once to finish and send.")
                stop = asyncio.create_task(self.finish_requested.wait())
                remaining = max(0, 30 - (asyncio.get_running_loop().time() - started))
                done, _ = await asyncio.wait((reader, stop), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    raise RuntimeError("Microphone capture ended unexpectedly; no audio was sent")
                if stop not in done:
                    raise RuntimeError("Voice recording cancelled at the 30-second limit; nothing was sent")
                process.send_signal(signal.SIGINT)
                _, stderr = await asyncio.wait_for(asyncio.shield(reader), 3)
                # pw-record 1.6 closes the WAV on SIGINT but exits 1 without a drained stream.
                clean_sigint = process.returncode == 1 and stderr.strip() == os.fsencode(audio)
                if process.returncode not in (0, -signal.SIGINT) and not clean_sigint:
                    raise RuntimeError("The microphone recording could not be finalized; nothing was sent")
                validate_recording(audio)
                await self.notice("Sending the voice command for local transcription.")
                await self.deliver(command, audio)
                self.phase = "accepted"
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.phase = "failed"
            self.failure = f"Voice command failed: {error}"
            await self.error(self.failure)
        finally:
            self.ready.set()
            if stop is not None:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill()
            if reader is not None:
                await reader

    async def deliver(self, command, audio):
        process = None
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, str(audio), stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
                await asyncio.wait_for(process.wait(), 240)
                if stdout.tell() > 131072:
                    raise RuntimeError("Audio receiver returned an oversized response; the request will not be replayed")
                stdout.seek(0)
                response = json.loads(stdout.read())
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise RuntimeError("Audio receiver returned an invalid acknowledgement; inspect its state before retrying")
                if process.returncode and response["ok"]:
                    raise RuntimeError("Audio receiver exited unsuccessfully after acknowledging the request. Nothing was replayed; check the receiving app.")
                if process.returncode or not response["ok"]:
                    detail = response.get("error", {})
                    message = detail.get("message") if isinstance(detail, dict) else str(detail)
                    raise RuntimeError(message or "Audio receiver rejected the request")
                message = response.get("message", "Voice request accepted.")
                if not isinstance(message, str) or not message.strip() or len(message) > 1024:
                    raise RuntimeError("Audio receiver returned an invalid message. The request was not replayed; check the receiving app.")
                await self.notice(message)
            except (TimeoutError, ValueError) as error:
                raise RuntimeError("Audio receiver outcome is unknown. The request was not replayed; check the receiving app.") from error
            finally:
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
