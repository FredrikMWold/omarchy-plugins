"""Executable receiver adapter; only the recorder owner confirms capture drain."""

import asyncio
import ctypes
import json
import os
import signal
from uuid import uuid4

FAILURE_CODES = frozenset(
    (
        "resource_busy lease_conflict lease_required expired invalid_identity invalid_audio "
        "invalid_audio_digest capture_id_conflict capture_identity_conflict capture_audio_conflict "
        "capture_expired capture_already_adopted capture_drain_required capture_owner_lost "
        "capture_disconnected unknown_capture job_id_conflict job_identity_conflict unknown_job "
        "broker_capacity unconfirmed_drain recognizer_unavailable invalid_recognition_result "
        "invalid_capture_command invalid_capture_identity receiver_uncertain "
        "receiver_transport_error "
        "request_cancelled request_rejected"
        " speaker_draining capture_output_unsupported capture_identity_conflict "
        "capture_readiness_timeout"
    ).split()
)


def receiver_failure(response):
    error = response.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or code not in FAILURE_CODES:
        code = "receiver_uncertain"
    return RuntimeError(f"shared_receiver_failure:{code}")


def child_guard():
    parent = os.getpid()

    def arm():
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(127)
        if os.getppid() != parent:
            os._exit(127)

    return arm


class SharedReceiver:
    def __init__(self, command):
        self.command = command
        self.process = None
        self.capture = {
            "lease_id": uuid4().hex,
            "job_id": uuid4().hex,
            "request_id": uuid4().hex,
            "source": "shield",
            "pcm_digest": "",
        }
        self.result = None
        self.submitted = False

    async def response(self):
        raw = await asyncio.wait_for(self.process.stdout.readline(), 70)
        if not raw.endswith(b"\n") or len(raw) > 131072:
            raise RuntimeError("Shared receiver disconnected; capture ownership is unconfirmed")
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise RuntimeError("Shared receiver returned an invalid acknowledgement")
        return value

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            "--capture",
            "--lease-id",
            self.capture["lease_id"],
            "--job-id",
            self.capture["job_id"],
            "--request-id",
            self.capture["request_id"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=131072,
            preexec_fn=child_guard(),
        )
        ready = await asyncio.wait_for(self.response(), 3)
        if not ready["ok"]:
            raise receiver_failure(ready)
        if not ready["ok"] or ready.get("capture") != self.capture:
            raise RuntimeError("Shared recognizer refused capture admission; nothing was recorded")
        self.result = asyncio.create_task(self.response())

    async def submit(self, audio):
        # A complete handoff command is intentional submission. Transport uncertainty
        # after this point must not automatically cancel an accepted coordinator job.
        self.submitted = True
        self.process.stdin.write(
            json.dumps({"action": "submit", "audio_file": str(audio)}).encode() + b"\n"
        )
        await self.process.stdin.drain()
        response = await self.result
        if not response["ok"]:
            raise receiver_failure(response)
        return response

    async def close(self, *, capture_drained):
        try:
            if not self.submitted and capture_drained:
                if (
                    self.process is not None
                    and self.process.returncode is None
                    and self.result is not None
                    and not self.result.done()
                ):
                    self.process.stdin.write(b'{"action":"abort"}\n')
                    await self.process.stdin.drain()
                    response = await asyncio.wait_for(asyncio.shield(self.result), 3)
                    if response["ok"]:
                        return
                if self.process is not None and self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()
                # The caller already waited for its microphone child to exit. Recover
                # that exact lease, never a global "idle" hint or someone else's job.
                recovery = await asyncio.create_subprocess_exec(
                    *self.command,
                    "--abort-capture",
                    json.dumps(self.capture),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    preexec_fn=child_guard(),
                )
                try:
                    raw, _ = await asyncio.wait_for(recovery.communicate(), 3)
                    response = json.loads(raw)
                    if recovery.returncode or response.get("ok") is not True:
                        raise RuntimeError(
                            "Capture drained but shared ownership release is unconfirmed"
                        )
                finally:
                    if recovery.returncode is None:
                        recovery.kill()
                        await recovery.wait()
        finally:
            if self.result is not None:
                self.result.cancel()
                await asyncio.gather(self.result, return_exceptions=True)
            if self.process is not None:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if self.process.returncode is None:
                    try:
                        await asyncio.wait_for(self.process.wait(), 1)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
