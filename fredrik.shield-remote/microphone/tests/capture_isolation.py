"""Opt-in regression for the real PipeWire source, using only synthetic audio."""

import array
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import wave


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise TimeoutError("The isolated PipeWire fixture did not become ready.")


def source_exists(name):
    objects = json.loads(subprocess.check_output(["pw-dump"], timeout=5))
    return any(item.get("info", {}).get("props", {}).get("node.name") == name for item in objects)


def start_recording(name, path):
    process = subprocess.Popen([
        "pw-record", f"--target={name}", "--rate=8000", "--channels=1", "--format=s16",
        "--properties", '{"node.dont-fallback":true,"node.dont-reconnect":true}', str(path),
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077)
    try:
        wait_for(lambda: path.exists() and path.stat().st_size > 44)
    except BaseException:
        process.kill()
        process.wait()
        raise
    return process


def stop_recording(process, path):
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) in (0, 1, -signal.SIGINT)
    with wave.open(str(path), "rb") as recording:
        assert (recording.getframerate(), recording.getnchannels(), recording.getsampwidth()) == (8000, 1, 2)
        data = recording.readframes(recording.getnframes())
        assert len(data) == recording.getnframes() * 2, "Truncated fixture WAV."
        samples = array.array("h", data)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples


def run_case(binary, case, root):
    name = f"shield-isolation-{os.getpid()}-{case}"
    with tempfile.TemporaryFile() as errors:
        source = subprocess.Popen([str(binary), name], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=errors, text=True)
        recording = None
        try:
            wait_for(lambda: source_exists(name))

            def send(command, value=None, count=None):
                line = command if value is None else f"{command} {value} {count}"
                source.stdin.write(line + "\n")
                source.stdin.flush()
                readable, _, _ = select.select([source.stdout], [], [], 5)
                assert readable, "The fixture did not acknowledge the audio command."
                assert source.stdout.readline() == "ok\n", "The fixture rejected the audio command."

            first = root / f"{case}-first.wav"
            recording = start_recording(name, first)
            for _ in range(30):
                send("frame", 10000, 160)
                time.sleep(0.02)
            if case == "pending":
                send("frame", 10000, 4000)
                time.sleep(0.04)
            a = stop_recording(recording, first)
            recording = None
            assert any(value == 10000 for value in a), "First capture did not receive fixture audio."
            time.sleep(0.1)
            if case in ("queued", "silence"):
                send("frame", 10000, 4000)
            elif case == "late":
                send("hold", 10000, 4000)
            time.sleep(0.1)

            second = root / f"{case}-second.wav"
            recording = start_recording(name, second)
            if case == "late":
                send("release")
            if case == "silence":
                time.sleep(0.4)
            else:
                for _ in range(45):
                    send("frame", 2000, 160)
                    time.sleep(0.02)
            time.sleep(0.1)
            b = stop_recording(recording, second)
            recording = None
            # The new signal cannot cross this threshold, even at a resampling edge.
            old = sum(abs(value) > 6000 for value in b)
            new = sum(value == 2000 for value in b)
            print(f"{case}: previous-command samples={old}, fresh-command samples={new}", flush=True)
            assert old == 0, "Previous-command PCM leaked into the next recording."
            if case == "silence":
                assert len(b) >= 2000, "The silent capture must contain actual recorded frames."
                assert not any(b), "A silent second recording must remain silent."
            else:
                assert new >= 4000, "Fresh audio was missing or substantially clipped."
        finally:
            if recording is not None and recording.poll() is None:
                recording.kill()
                recording.wait()
            if source.poll() is None:
                source.stdin.close()
                try:
                    source.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    source.kill()
                    source.wait()
            source.stdout.close()
            errors.seek(0)
            logs = errors.read().decode(errors="replace")
            if source.returncode:
                raise RuntimeError(f"The fixture source failed:\n{logs}")
        wait_for(lambda: not source_exists(name))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: capture_isolation.py PATH_TO_CAPTURE_ISOLATION_BINARY")
    binary = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="voice-isolation-") as directory:
        for case in ("pending", "queued", "late", "silence"):
            run_case(binary, case, Path(directory))
