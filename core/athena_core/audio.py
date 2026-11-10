"""Microphone capture and streaming speaker output (sounddevice/PortAudio)."""
from __future__ import annotations

import io
import logging
import threading
import time
import wave
from typing import Callable

import numpy as np

log = logging.getLogger("athena.audio")

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - missing PortAudio in CI
    sd = None

STT_RATE = 16000
TTS_RATE = 24000


def list_devices() -> dict:
    if sd is None:
        return {"inputs": [], "outputs": []}
    ins, outs = [], []
    try:
        apis = sd.query_hostapis()
        default_api = sd.default.hostapi if hasattr(sd.default, "hostapi") else 0
        for idx, dev in enumerate(sd.query_devices()):
            # Only show devices from the default host API (MME on Windows) to avoid 4x duplicates.
            if dev["hostapi"] != default_api:
                continue
            entry = {"index": idx, "name": dev["name"], "api": apis[dev["hostapi"]]["name"]}
            if dev["max_input_channels"] > 0:
                ins.append(entry)
            if dev["max_output_channels"] > 0:
                outs.append(entry)
    except Exception as exc:
        log.warning("device query failed: %s", exc)
    return {"inputs": ins, "outputs": outs}


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or x.size == 0:
        return x
    n = int(round(x.size * dst / src))
    idx = np.linspace(0, x.size - 1, n)
    return np.interp(idx, np.arange(x.size), x).astype(np.float32)


def pcm16_wav(pcm: bytes, rate: int = STT_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Recorder:
    """Records 16 kHz mono PCM16. Reports level and end-of-speech silence."""

    def __init__(self, on_level: Callable[[float], None] | None = None,
                 on_silence: Callable[[], None] | None = None):
        self.on_level = on_level
        self.on_silence = on_silence
        self._chunks: list[np.ndarray] = []
        self._stream = None
        self._rate = STT_RATE
        self._lock = threading.Lock()
        self.silence_ms = 1800
        self.watch_silence = False
        self._reset_vad()

    def _reset_vad(self):
        self._mute_until = 0.0
        self._noise = 0.004
        self._speech = False
        self._last_voice = 0.0
        self._t0 = time.monotonic()
        self._last_level = 0.0
        self._silence_fired = False

    def start(self, device=None, mute_s: float = 0.0) -> None:
        """mute_s: ignore this much audio at the start (Athena's own voice tail, the start chime)."""
        if sd is None:
            raise RuntimeError("sounddevice is not installed")
        self.stop_stream()
        self._chunks = []
        self._reset_vad()
        self._mute_until = self._t0 + max(0.0, mute_s)
        for rate in (STT_RATE, None):
            try:
                if rate is None:
                    info = sd.query_devices(device, "input")
                    rate = int(info["default_samplerate"])
                self._rate = rate
                self._stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                              blocksize=int(rate * 0.03), device=device,
                                              callback=self._cb)
                self._stream.start()
                return
            except Exception as exc:
                log.warning("mic open at %s Hz failed: %s", rate, exc)
                self._stream = None
        raise RuntimeError("Could not open microphone")

    def _cb(self, indata, frames, t, status):
        now = time.monotonic()
        if now < self._mute_until:
            return  # dropped: still hearing the speaker / chime
        x = indata[:, 0].copy()
        with self._lock:
            self._chunks.append(x)
        rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
        elapsed = now - max(self._t0, self._mute_until)
        if elapsed < 0.35 and not self._speech:
            self._noise = 0.8 * self._noise + 0.2 * rms
        thresh = max(0.012, self._noise * 3.0)
        if rms > thresh:
            self._speech = True
            self._last_voice = now
        if (self.watch_silence and self._speech and not self._silence_fired
                and (now - self._last_voice) * 1000 > self.silence_ms):
            self._silence_fired = True
            if self.on_silence:
                self.on_silence()
        if self.on_level and now - self._last_level > 0.05:
            self._last_level = now
            self.on_level(min(1.0, rms / 0.12))

    def stop_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def stop(self) -> bytes:
        self.stop_stream()
        with self._lock:
            x = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks = []
        x = resample(x, self._rate, STT_RATE)
        return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()

    @property
    def heard_speech(self) -> bool:
        return self._speech


class Speaker:
    """Callback-driven PCM16 player. feed() bytes as they stream in; clear() to barge-in."""

    def __init__(self, rate: int = TTS_RATE, on_level: Callable[[float], None] | None = None):
        self.rate = rate
        self.on_level = on_level
        self.device = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = None
        self._last_level = 0.0
        self._generation = 0
        self.last_audio = 0.0

    def _ensure(self):
        if self._stream is not None or sd is None:
            return
        self._stream = sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16",
                                          blocksize=int(self.rate * 0.02), device=self.device,
                                          latency="low", callback=self._cb)
        self._stream.start()

    def set_device(self, device) -> None:
        if device != self.device:
            self.device = device
            self.close()

    def _cb(self, outdata, frames, t, status):
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
        now = time.monotonic()
        if chunk:
            self.last_audio = now
        if len(chunk) < need:
            chunk += b"\x00" * (need - len(chunk))
        outdata[:] = chunk
        if self.on_level and now - self._last_level > 0.05:
            self._last_level = now
            a = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768
            self.on_level(min(1.0, float(np.sqrt(np.mean(a * a))) / 0.15))

    def feed(self, pcm: bytes) -> None:
        self._ensure()
        with self._lock:
            self._buf.extend(pcm)

    def clear(self) -> None:
        self._generation += 1
        with self._lock:
            had = bool(self._buf)
            self._buf.clear()
        if had or time.monotonic() - self.last_audio < 0.5:
            self.abort()

    def abort(self) -> None:
        """Drop audio already handed to the sound device so speech stops instantly."""
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def recently_active(self, within: float = 0.6) -> bool:
        return time.monotonic() - self.last_audio < within or self.pending_seconds() > 0

    @property
    def generation(self) -> int:
        return self._generation

    def pending_seconds(self) -> float:
        with self._lock:
            return len(self._buf) / 2 / self.rate

    def tone(self, freqs: list[float], dur: float = 0.07, vol: float = 0.12) -> None:
        """Tiny earcon (sequence of soft sine blips)."""
        parts = []
        for f in freqs:
            t = np.arange(int(self.rate * dur)) / self.rate
            env = np.sin(np.pi * t / dur) ** 2
            parts.append(np.sin(2 * np.pi * f * t) * env * vol)
        x = np.concatenate(parts)
        self.feed((x * 32767).astype("<i2").tobytes())

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
