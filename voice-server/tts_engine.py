"""Pocket TTS engine with streaming PCM16 output."""
from __future__ import annotations

import asyncio
import logging
import threading
from queue import Empty, Full, Queue
from typing import AsyncIterator

import numpy as np
import torch

from audio_utils import float32_to_pcm16, resample_linear
from config import resolve_device, settings

logger = logging.getLogger("tts")


class TTSEngine:
    """Wraps Kyutai Pocket TTS for streaming synthesis.

    Pocket TTS yields float32 chunks at 24kHz. We resample to the configured TTS
    sample rate and encode to PCM16 for streaming over the wire.
    """

    def __init__(self, device: str | None = None):
        from pocket_tts import TTSModel

        device = device or resolve_device()
        self.device = device
        self._inference_lock = threading.Lock()
        logger.info("Loading Pocket TTS (language=%s) on %s ...", settings.tts_language, self.device)
        self.model = TTSModel.load_model(
            language=settings.tts_language, quantize=settings.tts_quantize
        )
        if self.device.startswith("cuda"):
            self.model = self.model.to(self.device)
        self.sample_rate = int(self.model.sample_rate)
        logger.info("TTS ready (sr=%d).", self.sample_rate)

    def _state_for_voice(self, voice: str | None):
        voice = voice or settings.tts_voice
        # Pocket TTS accepts predefined voice names (alba, jean, ...) or hf:// URLs.
        return self.model._cached_get_state_for_audio_prompt(voice)

    def stream_pcm16(
        self, text: str, voice: str | None = None
    ) -> AsyncIterator[bytes]:
        """Async generator yielding PCM16 mono bytes at the TTS sample rate."""
        return self._stream_pcm16_async(text, voice)

    async def _stream_pcm16_async(self, text: str, voice: str | None) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: Queue = Queue(maxsize=8)
        stop_event = threading.Event()
        SENTINEL = object()

        def enqueue(item) -> bool:
            while not stop_event.is_set():
                try:
                    queue.put(item, timeout=0.1)
                    return True
                except Full:
                    continue
            return False

        def worker():
            try:
                with self._inference_lock:
                    state = self._state_for_voice(voice)
                    for chunk in self.model.generate_audio_stream(
                        model_state=state, text_to_generate=text
                    ):
                        if stop_event.is_set():
                            break
                        # chunk: torch.Tensor float32 at model.sample_rate
                        arr = chunk.detach().cpu().numpy().astype(np.float32)
                        if settings.tts_sample_rate != self.sample_rate:
                            arr = resample_linear(arr, self.sample_rate, settings.tts_sample_rate)
                        if not enqueue(float32_to_pcm16(arr)):
                            break
            except Exception as e:
                enqueue(e)
            finally:
                # The sentinel must arrive even after a cancelled consumer set
                # stop_event: enqueue() refuses then, and the executor thread
                # abandoned inside queue.get would block forever, leaking one
                # worker from the shared default executor per barge-in.
                while True:
                    try:
                        queue.put_nowait(SENTINEL)
                        break
                    except Full:
                        try:
                            queue.get_nowait()
                        except Empty:
                            pass

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            while True:
                item = await loop.run_in_executor(None, queue.get)
                if item is SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop_event.set()
            thread.join(timeout=1.0)

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Full-utterance synthesis -> single PCM16 bytes blob (non-streaming)."""
        chunks: list[np.ndarray] = []
        with self._inference_lock:
            state = self._state_for_voice(voice)
            for chunk in self.model.generate_audio_stream(
                model_state=state, text_to_generate=text
            ):
                chunks.append(chunk.detach().cpu().numpy().astype(np.float32))
        if not chunks:
            return b""
        audio = np.concatenate(chunks)
        if settings.tts_sample_rate != self.sample_rate:
            audio = resample_linear(audio, self.sample_rate, settings.tts_sample_rate)
        return float32_to_pcm16(audio)
