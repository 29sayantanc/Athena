"""OpenAI Realtime API compatible WebSocket session with Silero VAD."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from config import settings
from stt_engine import STTEngine, SileroVADStream
from tts_engine import TTSEngine

logger = logging.getLogger("realtime")


class RealtimeSession:
    """One WebSocket session = one conversation turn pipeline.

    Implements a subset of the OpenAI Realtime API event protocol:
      inbound:  session.update, input_audio_buffer.append,
                input_audio_buffer.commit, response.create
      outbound: session.created, input_audio_buffer.speech_started,
                input_audio_buffer.speech_stopped,
                conversation.item.input_audio_transcription.completed,
                conversation.item.input_audio_transcription.partial,
                response.audio.delta, response.audio_transcript.delta,
                response.done, error
    """

    def __init__(self, ws: WebSocket, stt: STTEngine, tts: TTSEngine):
        self.ws = ws
        self.stt = stt
        self.tts = tts
        self.vad = SileroVADStream(stt)
        self.modalities: list[str] = ["text", "audio"]
        self.tts_voice: str | None = settings.tts_voice
        self.tts_format: str = "pcm16"
        self.stt_format: str = "pcm16"
        self.output_sample_rate: int = settings.tts_sample_rate
        self.create_response: bool = settings.auto_echo
        self._closed = False
        self._speaking = False  # TTS currently active (barge-in guard)
        self._tts_task: asyncio.Task | None = None

    async def _send(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            await self.ws.send_text(json.dumps(event))
        except Exception:
            self._closed = True

    async def run(self) -> None:
        await self._send({
            "type": "session.created",
            "session": self._session_snapshot(),
        })
        try:
            await self._receive_loop()
        except WebSocketDisconnect:
            logger.info("client disconnected")
        except Exception as e:
            logger.exception("session error: %s", e)
            await self._send({"type": "error", "error": {"message": str(e)}})
        finally:
            await self._cancel_speech(notify=False)
            self._closed = True

    async def _receive_loop(self) -> None:
        while not self._closed:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            if "text" in msg and msg["text"] is not None:
                await self._handle_event(json.loads(msg["text"]))
            elif "bytes" in msg and msg["bytes"] is not None:
                await self._feed_audio(msg["bytes"])  # raw PCM16 bytes

    async def _handle_event(self, event: dict[str, Any]) -> None:
        t = event.get("type")
        if t == "session.update":
            sess = event.get("session", {}) or {}
            if "modalities" in sess:
                self.modalities = sess["modalities"]
            if "voice" in sess:
                self.tts_voice = sess["voice"]
            if sess.get("input_audio_format"):
                self.stt_format = sess["input_audio_format"]
            if sess.get("output_audio_format"):
                self.tts_format = sess["output_audio_format"]
            turn_detection = sess.get("turn_detection", {}) or {}
            if "create_response" in turn_detection:
                self.create_response = bool(turn_detection["create_response"])
            # Turn-detection sensitivity is retunable per session so a client can
            # expose it as a setting instead of requiring a server restart.
            if any(
                key in turn_detection
                for key in ("threshold", "silence_duration_ms", "prefix_padding_ms", "min_speech_ms")
            ):
                try:
                    self.vad.configure(
                        threshold=turn_detection.get("threshold"),
                        min_silence_ms=turn_detection.get("silence_duration_ms"),
                        speech_pad_ms=turn_detection.get("prefix_padding_ms"),
                        min_speech_ms=turn_detection.get("min_speech_ms"),
                    )
                    logger.info("VAD retuned: %s", self.vad.describe())
                except (TypeError, ValueError) as exc:
                    await self._send({
                        "type": "error",
                        "error": {"message": f"invalid turn_detection: {exc}"},
                    })
            await self._send({"type": "session.updated", "session": self._session_snapshot()})
        elif t == "input_audio_buffer.append":
            audio = event.get("audio")
            if audio:
                raw = base64.b64decode(audio)
                await self._feed_audio(raw)
        elif t == "input_audio_buffer.clear":
            self.vad.reset()
        elif t == "input_audio_buffer.commit":
            events = await asyncio.get_running_loop().run_in_executor(None, self.vad.commit)
            await self._emit_transcription_events(events)
        elif t == "response.create":
            resp = event.get("response", {}) or {}
            text = resp.get("input") or resp.get("instructions") or ""
            if text:
                await self._start_speaking(text)
        elif t == "response.cancel":
            await self._cancel_speech(notify=False)
        else:
            logger.debug("unhandled event: %s", t)

    async def _feed_audio(self, pcm: bytes) -> None:
        # If TTS is playing and user starts speaking → barge-in: stop TTS
        if self._speaking:
            # Quick energy check before running full VAD
            from audio_utils import rms, pcm16_to_float32
            samples = pcm16_to_float32(pcm)
            if rms(samples) >= settings.barge_in_rms:
                logger.info("barge-in detected, stopping TTS")
                await self._cancel_speech(notify=True)
        # Run VAD in executor (Silero is fast but blocks)
        events = await asyncio.get_running_loop().run_in_executor(None, self.vad.feed, pcm)
        await self._emit_transcription_events(events)

    async def _emit_transcription_events(self, events: list[tuple[str, object]]) -> None:
        for kind, payload in events:
            if kind == "speech_started":
                await self._send({"type": "input_audio_buffer.speech_started",
                                  "audio_start_ms": int(time.time() * 1000) % 1000000})
            elif kind == "speech_stopped":
                await self._send({"type": "input_audio_buffer.speech_stopped",
                                  "audio_end_ms": int(time.time() * 1000) % 1000000})
            elif kind == "partial":
                await self._send({"type": "conversation.item.input_audio_transcription.partial",
                                  "transcript": payload})
            elif kind == "final":
                await self._send({"type": "conversation.item.input_audio_transcription.completed",
                                  "transcript": payload})
                if self.create_response:
                    await self._start_speaking(str(payload))
            elif kind == "error":
                await self._send({"type": "error", "error": {"message": str(payload)}})

    async def _start_speaking(self, text: str) -> None:
        await self._cancel_speech(notify=False)
        self._tts_task = asyncio.create_task(self._speak(text))

    async def _cancel_speech(self, notify: bool) -> None:
        task = self._tts_task
        self._tts_task = None
        self._speaking = False
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if notify:
            await self._send({"type": "response.cancel"})

    async def _speak(self, text: str) -> None:
        if not text.strip():
            return
        self._speaking = True
        resp_id = f"resp_{int(time.time()*1000)}"
        await self._send({"type": "response.created", "response": {"id": resp_id, "output": []}})
        item_id = f"msg_{int(time.time()*1000)}"
        await self._send({"type": "response.output_item.added", "output_index": 0,
                          "item": {"id": item_id, "type": "message", "role": "assistant", "content": []}})
        frame_bytes = int(settings.tts_sample_rate * settings.tts_frame_ms / 1000) * 2
        buf = bytearray()
        status = "completed"
        await self._send({"type": "response.audio_transcript.delta", "item_id": item_id,
                          "delta": text})
        try:
            async for chunk in self.tts.stream_pcm16(text, voice=self.tts_voice):
                if not self._speaking:
                    logger.info("TTS interrupted by barge-in")
                    break
                buf.extend(chunk)
                while len(buf) >= frame_bytes:
                    frame = bytes(buf[:frame_bytes])
                    del buf[:frame_bytes]
                    await self._send({"type": "response.audio.delta", "item_id": item_id,
                                      "delta": base64.b64encode(frame).decode("ascii")})
            if self._speaking and buf:
                await self._send({"type": "response.audio.delta", "item_id": item_id,
                                  "delta": base64.b64encode(bytes(buf)).decode("ascii")})
        except asyncio.CancelledError:
            status = "cancelled"
        finally:
            self._speaking = False
        await self._send({"type": "response.audio.done", "item_id": item_id})
        await self._send({"type": "response.audio_transcript.done", "item_id": item_id,
                          "transcript": text})
        await self._send({"type": "response.output_item.done", "output_index": 0,
                          "item": {"id": item_id, "type": "message", "role": "assistant",
                                   "content": [{"type": "audio", "transcript": text}]}})
        await self._send({"type": "response.done",
                          "response": {"id": resp_id, "status": status}})

    def _session_snapshot(self) -> dict[str, Any]:
        return {
            "id": f"sess_{id(self):x}",
            "object": "realtime.session",
            "model": "parakeet-pocket",
            "modalities": self.modalities,
            "instructions": "Realtime voice bridge. STT=Parakeet TDT, TTS=Pocket TTS, VAD=Silero.",
            "voice": self.tts_voice,
            "input_audio_format": self.stt_format,
            "output_audio_format": self.tts_format,
            "input_audio_transcription": {"model": settings.stt_model},
            "turn_detection": {
                "type": "server_vad",
                "threshold": self.vad.threshold,
                "prefix_padding_ms": self.vad.speech_pad_ms,
                "silence_duration_ms": self.vad.min_silence_ms,
                "min_speech_ms": self.vad.min_speech_ms,
                "create_response": self.create_response,
            },
            "sample_rate": self.output_sample_rate,
        }


async def realtime_endpoint(ws: WebSocket, stt: STTEngine, tts: TTSEngine) -> None:
    await ws.accept()
    session = RealtimeSession(ws, stt, tts)
    await session.run()
