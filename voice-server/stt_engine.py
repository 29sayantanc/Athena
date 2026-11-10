"""Parakeet TDT STT engine with Silero VAD for semantic voice activity detection."""
from __future__ import annotations

import logging
import threading
from collections import deque

import numpy as np
import torch

from audio_utils import pcm16_to_float32, resample_linear
from config import resolve_device, settings

logger = logging.getLogger("stt")


class STTEngine:
    """Wraps nvidia/parakeet-tdt-0.6b-v3 for streaming-friendly transcription.

    Parakeet TDT is an RNN-T model; the HF `transformers` port supports full-utterance
    `generate()`. For realtime we run Silero VAD on the incoming 16k PCM16 stream,
    and once an utterance is committed (silence after speech detected by Silero)
    we transcribe the buffered audio. Partial results are emitted by re-running
    on the growing buffer every `stt_min_chunk_ms` while speech is ongoing.
    """

    def __init__(self, device: str | None = None):
        from transformers import ParakeetForTDT, ParakeetProcessor

        device = device or resolve_device()
        self.device = device
        self._inference_lock = threading.Lock()
        logger.info("Loading Parakeet STT model %s on %s ...", settings.stt_model, self.device)
        self.processor = ParakeetProcessor.from_pretrained(settings.stt_model)
        dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32
        self.model = ParakeetForTDT.from_pretrained(
            settings.stt_model, torch_dtype=dtype
        ).to(self.device).eval()
        self.dtype = dtype
        self.sample_rate = settings.stt_sample_rate
        logger.info("STT ready (sr=%d).", self.sample_rate)

    @torch.inference_mode()
    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe a full utterance (float32 mono). Returns decoded text."""
        if samples.size == 0:
            return ""
        if sample_rate != self.sample_rate:
            samples = resample_linear(samples, sample_rate, self.sample_rate)
        inputs = self.processor(samples, sampling_rate=self.sample_rate, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        if "input_features" in inputs and self.dtype == torch.bfloat16:
            inputs["input_features"] = inputs["input_features"].to(torch.bfloat16)
        with self._inference_lock:
            out = self.model.generate(**inputs, max_new_tokens=512)
        sequences = out.sequences if hasattr(out, "sequences") else out
        text = self.processor.batch_decode(sequences)[0]
        text = text.replace("<blank>", "").replace("<unk>", "").strip()
        return text


class SileroVADStream:
    """Silero-based streaming VAD that segments incoming PCM16 into utterances.

    Uses VADIterator for frame-by-frame speech detection. Yields events:
      ('speech_started', None)
      ('partial', str)          — best-effort partial transcription during speech
      ('speech_stopped', None)
      ('final', str)            — full transcription of the utterance
    """

    def __init__(self, engine: STTEngine):
        from silero_vad import load_silero_vad

        self.engine = engine
        self.sr = engine.sample_rate
        self.vad_model = load_silero_vad()
        # Silero requires 512-sample chunks at 16kHz
        self.chunk_size = 512
        self.threshold = settings.vad_threshold
        self.min_silence_ms = settings.vad_min_silence_ms
        self.speech_pad_ms = settings.vad_speech_pad_ms
        self.min_speech_ms = settings.vad_min_speech_ms
        self._pending: deque[float] = deque()
        self._preroll: deque[np.ndarray] = deque(maxlen=self._preroll_len())
        self._build_iterator()
        self.buffer: list[np.ndarray] = []
        self.input_buffer: list[np.ndarray] = []
        self.in_speech = False
        self.utterance_ms = 0
        self.last_partial_ms = 0

    def _preroll_len(self) -> int:
        return max(1, int(self.speech_pad_ms / 1000 * self.sr / self.chunk_size) + 1)

    def _build_iterator(self) -> None:
        from silero_vad import VADIterator

        self.vad = VADIterator(
            self.vad_model,
            threshold=self.threshold,
            min_silence_duration_ms=self.min_silence_ms,
            speech_pad_ms=self.speech_pad_ms,
        )

    def configure(
        self,
        threshold: float | None = None,
        min_silence_ms: int | None = None,
        speech_pad_ms: int | None = None,
        min_speech_ms: int | None = None,
    ) -> dict:
        """Retune turn detection mid-session from a client `session.update`.

        Sensitivity is the setting people actually need to adjust per room and
        microphone, so it is reachable at runtime rather than only via env vars.
        """
        if threshold is not None:
            self.threshold = min(0.95, max(0.05, float(threshold)))
        if min_silence_ms is not None:
            self.min_silence_ms = min(5000, max(100, int(min_silence_ms)))
        if speech_pad_ms is not None:
            self.speech_pad_ms = min(2000, max(0, int(speech_pad_ms)))
        if min_speech_ms is not None:
            self.min_speech_ms = min(5000, max(0, int(min_speech_ms)))
        self._preroll = deque(self._preroll.copy(), maxlen=self._preroll_len())
        self._build_iterator()
        self.reset()
        return self.describe()

    def describe(self) -> dict:
        return {
            "threshold": self.threshold,
            "min_silence_ms": self.min_silence_ms,
            "speech_pad_ms": self.speech_pad_ms,
            "min_speech_ms": self.min_speech_ms,
        }

    def feed(self, pcm: bytes) -> list[tuple[str, object]]:
        """Process a chunk of PCM16 bytes. Returns list of (event_type, payload)."""
        events: list[tuple[str, object]] = []
        samples = pcm16_to_float32(pcm)
        if samples.size == 0:
            return events

        self._pending.extend(samples.tolist())

        # Process in 512-sample chunks as required by Silero
        while len(self._pending) >= self.chunk_size:
            chunk_data = [self._pending.popleft() for _ in range(self.chunk_size)]
            chunk_np = np.array(chunk_data, dtype=np.float32)
            chunk = torch.tensor(chunk_data, dtype=torch.float32).unsqueeze(0)
            self._preroll.append(chunk_np.copy())

            vad_out = self.vad(chunk, return_seconds=False)

            speech_started = vad_out is not None and "start" in vad_out
            speech_stopped = vad_out is not None and "end" in vad_out

            if speech_started:
                self.in_speech = True
                self.buffer = list(self._preroll)
                self.input_buffer = list(self._preroll)
                self.utterance_ms = int(len(self.buffer) * self.chunk_size / self.sr * 1000)
                self.last_partial_ms = 0
                events.append(("speech_started", None))

            if self.in_speech and not speech_started:
                self.buffer.append(chunk_np.copy())
                self.input_buffer.append(chunk_np.copy())
                self.utterance_ms += int(self.chunk_size / self.sr * 1000)

            if speech_stopped and self.in_speech:
                self.in_speech = False
                final_audio = np.concatenate(self.buffer) if self.buffer else np.zeros(0, dtype=np.float32)
                events.append(("speech_stopped", None))
                if len(final_audio) * 1000 / self.sr >= self.min_speech_ms:
                    try:
                        text = self.engine.transcribe(final_audio, self.sr)
                        if text:
                            events.append(("final", text))
                    except Exception as e:
                        logger.warning("final transcription failed: %s", e)
                        events.append(("error", f"final transcription failed: {e}"))
                self.buffer = []
                self.input_buffer = []
            elif self.in_speech and settings.stt_partial_results:
                if self.utterance_ms - self.last_partial_ms >= settings.stt_min_chunk_ms:
                    self.last_partial_ms = self.utterance_ms
                    partial_audio = np.concatenate(self.buffer) if self.buffer else np.zeros(0, dtype=np.float32)
                    try:
                        text = self.engine.transcribe(partial_audio, self.sr)
                        if text:
                            events.append(("partial", text))
                    except Exception as e:
                        logger.warning("partial transcription failed: %s", e)

        return events

    def commit(self) -> list[tuple[str, object]]:
        """Force transcription of all audio received since the last turn."""
        pieces = list(self.input_buffer)
        if self._pending:
            pieces.append(np.asarray(self._pending, dtype=np.float32))
        audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
        was_speaking = self.in_speech
        self.reset()

        events: list[tuple[str, object]] = []
        if was_speaking:
            events.append(("speech_stopped", None))
        if audio.size == 0:
            return events
        try:
            text = self.engine.transcribe(audio, self.sr)
            if text:
                events.append(("final", text))
        except Exception as e:
            logger.warning("committed transcription failed: %s", e)
            events.append(("error", f"committed transcription failed: {e}"))
        return events

    def reset(self):
        """Clear all buffers and reset VAD state."""
        self.vad.reset_states()
        self._pending.clear()
        self._preroll.clear()
        self.buffer = []
        self.input_buffer = []
        self.in_speech = False
        self.utterance_ms = 0
        self.last_partial_ms = 0
