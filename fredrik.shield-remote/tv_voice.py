# SPDX-License-Identifier: Apache-2.0
# Connection setup adapts androidtvremote2 0.3.2; see licenses/androidtvremote2-LICENSE.
from __future__ import annotations

import asyncio
from collections.abc import Callable
import signal
import ssl

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth, VoiceStream
from androidtvremote2.remote import RemoteProtocol, VOICE_CHUNK_SIZE
from androidtvremote2.remotemessage_pb2 import RemoteMessage
from google.protobuf.message import DecodeError


async def stream_microphone(stream: VoiceStream, source: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "pw-record", f"--target={source}", "--rate=8000", "--channels=1", "--format=s16",
        "--raw", "--properties", '{"node.dont-fallback":true,"node.dont-reconnect":true}', "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        while True:
            try:
                # Smaller accepted packets produced no recognition on this Shield.
                chunk = await asyncio.wait_for(process.stdout.readexactly(VOICE_CHUNK_SIZE), 3)
            except asyncio.IncompleteReadError as error:
                _, stderr = await asyncio.wait_for(process.communicate(), 2)
                reason = stderr.decode().strip() or f"exit status {process.returncode}"
                raise RuntimeError(f"Shield microphone capture stopped: {reason}") from error
            except TimeoutError as error:
                raise RuntimeError("Shield microphone capture delivered no PCM for 3 seconds") from error
            if not stream.send_chunk(chunk):
                return
    finally:
        if process.returncode is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                await process.wait()
        try:
            await asyncio.wait_for(process.communicate(), 2)
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise RuntimeError("Shield microphone capture required forced termination") from error


class AppVoiceProtocol(RemoteProtocol):
    def __init__(self, *args, voice_requested, voice_ended, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_requested: Callable[[VoiceStream], None] = voice_requested
        self.voice_ended: Callable[[VoiceStream], None] = voice_ended
        self.app_voice: VoiceStream | None = None

    def finish_app_voice(self) -> None:
        stream, self.app_voice = self.app_voice, None
        if stream:
            stream.end()
            self.voice_ended(stream)

    def end_voice(self, session_id: int) -> None:
        super().end_voice(session_id)
        if self.app_voice and self.app_voice.session_id == session_id:
            stream, self.app_voice = self.app_voice, None
            self.voice_ended(stream)

    def _handle_message(self, raw_msg: bytes) -> None:
        try:
            message = RemoteMessage.FromString(raw_msg)
        except DecodeError:
            super()._handle_message(raw_msg)
            return
        if message.HasField("remote_voice_begin") and self._on_voice_begin is None:
            session_id = message.remote_voice_begin.session_id
            if self.app_voice and self.app_voice.session_id == session_id:
                return
            self.finish_app_voice()
            self._voice_session_id = session_id
            self.app_voice = VoiceStream(self, session_id)
            acknowledgement = RemoteMessage(remote_voice_begin={"session_id": session_id})
            self._send_message(acknowledgement)
            self.voice_requested(self.app_voice)
        elif message.HasField("remote_voice_end"):
            if self.app_voice and self.app_voice.session_id == message.remote_voice_end.session_id:
                self.finish_app_voice()
        else:
            super()._handle_message(raw_msg)

    def connection_lost(self, exc: Exception | None) -> None:
        self.finish_app_voice()
        super().connection_lost(exc)

    def close(self) -> None:
        self.finish_app_voice()
        super().close()


class VoiceRemote(AndroidTVRemote):
    def __init__(self, *args, voice_requested, voice_ended, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_requested: Callable[[VoiceStream], None] = voice_requested
        self.voice_ended: Callable[[VoiceStream], None] = voice_ended

    async def async_connect(self) -> None:
        ssl_context = await self._create_ssl_context()
        lost = self._loop.create_future()
        started = self._loop.create_future()
        try:
            self._transport, self._remote_message_protocol = await self._loop.create_connection(
                lambda: AppVoiceProtocol(
                    lost, started, self._on_is_on_updated, self._on_current_app_updated,
                    self._on_volume_info_updated, self._loop, self._enable_ime, self._enable_voice,
                    voice_requested=self.voice_requested, voice_ended=self.voice_ended,
                ),
                self.host, self._api_port, ssl=ssl_context,
            )
        except ssl.SSLError as error:
            raise InvalidAuth("Need to pair") from error
        except OSError as error:
            raise CannotConnect(f"Couldn't connect to {self.host}:{self._api_port}") from error
        try:
            await asyncio.wait((lost, started), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            self._remote_message_protocol.close()
            raise
        if lost.done():
            error = lost.result()
            self._remote_message_protocol.close()
            if isinstance(error, ssl.SSLError):
                raise InvalidAuth("Need to pair again") from error
            raise ConnectionClosed("Connection closed") from error
