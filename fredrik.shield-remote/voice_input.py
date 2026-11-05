"""Mode-independent recording with one configurable, non-retrying WAV receiver."""

import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import wave

from shared_receiver import SharedReceiver, child_guard


def receiver_command(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError("The audio receiver configuration must be a private, user-owned regular file")
    if info.st_size > 8192:
        raise ValueError("Audio receiver configuration is too large")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) - {"version", "command", "activation", "ownership"}
            or not {"version", "command"} <= set(value)
            or type(value["version"]) is not int or value["version"] != 1
            or value.get("activation", "double-press") not in {"double-press", "hold"}
            or value.get("ownership") not in {None, "shared-recognition"}
            or (value.get("activation") == "hold" and value.get("ownership") != "shared-recognition")):
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
            if not 8000 <= frames < 480000:
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
        self.input_check = None

    @property
    def hold_enabled(self):
        try:
            receiver_command(self.config)
            return json.loads(self.config.read_text()).get("activation") == "hold"
        except FileNotFoundError:
            return False

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    async def start(self, source, *, wait_ready=True):
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
        if not wait_ready:
            return
        await self.ready.wait()
        if self.phase != "recording":
            raise RuntimeError(self.failure or "The remote microphone could not start recording")

    def finish(self):
        if self.phase != "recording":
            raise RuntimeError("No voice command is recording")
        self.phase = "finalizing"
        self.finish_requested.set()

    async def close(self):
        if self.busy:
            if not self.task.cancelling():
                self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.phase = "idle"

    async def run(self, source, command):
        input_check = self.input_check

        def verify_input():
            if input_check is not None:
                try:
                    input_check()
                except OSError:
                    raise RuntimeError(
                        "Physical remote input disconnected before submission; recording discarded"
                    ) from None

        process = None
        reader = None
        stop = None
        shared = None
        try:
            if json.loads(self.config.read_text()).get("ownership") == "shared-recognition":
                shared = SharedReceiver(command)
                await shared.start()
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
                    preexec_fn=child_guard() if shared is not None else None,
                )
                reader = asyncio.create_task(process.communicate())
                for _ in range(75):
                    if reader.done():
                        raise RuntimeError("The remote microphone stopped before recording began")
                    if shared is not None and shared.result.done():
                        raise RuntimeError("Shared capture ownership was lost before readiness")
                    if audio.stat().st_size > 44:
                        break
                    await asyncio.sleep(0.04)
                else:
                    raise RuntimeError("The remote microphone did not deliver audio")
                self.phase = "recording"
                self.ready.set()
                try:
                    await asyncio.wait_for(
                        self.notice("Listening for a voice command. Release Speak to finish and send."
                                    if self.hold_enabled else
                                    "Listening for a voice command. Press Speak once to finish and send."),
                        max(0, 30 - (asyncio.get_running_loop().time() - started)),
                    )
                except TimeoutError:
                    raise RuntimeError("Voice recording cancelled at the 30-second limit; nothing was sent") from None
                stop = asyncio.create_task(self.finish_requested.wait())
                remaining = max(0, 30 - (asyncio.get_running_loop().time() - started))
                waiting = (reader, stop, shared.result) if shared is not None else (reader, stop)
                done, _ = await asyncio.wait(waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if shared is not None and shared.result in done:
                    raise RuntimeError("Shared capture ownership was lost; nothing was sent")
                if reader in done:
                    raise RuntimeError("Microphone capture ended unexpectedly; no audio was sent")
                if stop not in done or asyncio.get_running_loop().time() - started >= 30:
                    raise RuntimeError("Voice recording cancelled at the 30-second limit; nothing was sent")
                verify_input()
                process.send_signal(signal.SIGINT)
                _, stderr = await asyncio.wait_for(asyncio.shield(reader), 3)
                # pw-record 1.6 closes the WAV on SIGINT but may exit 1, either
                # silently (1.6.9) or with only the filename. Validate the WAV below.
                clean_sigint = process.returncode == 1 and stderr.strip() in (b"", os.fsencode(audio))
                if process.returncode not in (0, -signal.SIGINT) and not clean_sigint:
                    raise RuntimeError("The microphone recording could not be finalized; nothing was sent")
                validate_recording(audio)
                verify_input()
                await self.notice("Sending the voice command for local transcription.")
                verify_input()
                self.phase = "sending"
                if shared is None:
                    await self.deliver(command, audio)
                else:
                    response = await shared.submit(audio)
                    if response["ok"] is not True:
                        raise RuntimeError("Shared receiver rejected the finalized recording; inspect its request status")
                    await self.notice(response.get("message", "Voice request processed."))
                self.phase = "accepted"
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.phase = "failed"
            self.failure = f"Voice command failed: {error}"
            await self.error(self.failure)
        finally:
            cleanup_failure = ""
            if stop is not None:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill()
            if reader is not None:
                try:
                    await reader
                except (OSError, RuntimeError) as error:
                    cleanup_failure = f"Capture reader cleanup failed: {error}"
            if process is not None:
                try:
                    await process.wait()
                except (OSError, RuntimeError) as error:
                    cleanup_failure = f"Capture exit is unconfirmed: {error}"
            if shared is not None:
                try:
                    await shared.close(capture_drained=process is None or process.returncode is not None)
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    cleanup_failure = f"Shared capture cleanup failed: {error}"
            if cleanup_failure:
                self.phase = "failed"
                self.failure = cleanup_failure
            self.ready.set()
            if cleanup_failure:
                await self.error(self.failure)

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
