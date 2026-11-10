"""Audio conversion helpers shared by STT and TTS paths."""
from __future__ import annotations

import io
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """Decode little-endian PCM16 mono bytes to float32 in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return arr


def float32_to_pcm16(samples: np.ndarray) -> bytes:
    """Encode float32 (-1..1) to little-endian PCM16 mono bytes."""
    arr = np.clip(samples, -1.0, 1.0)
    arr = (arr * 32767.0).astype("<i2")
    return arr.tobytes()


def resample_linear(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Fast linear resample. Good enough for 16k<->24k voice; use librosa for higher quality if needed."""
    if src_sr == dst_sr or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    n_out = int(round(len(samples) * dst_sr / src_sr))
    if n_out <= 1:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0, len(samples) - 1, n_out)
    i0 = idx.astype(np.int32)
    i1 = np.minimum(i0 + 1, len(samples) - 1)
    frac = (idx - i0).astype(np.float32)
    out = samples[i0] * (1.0 - frac) + samples[i1] * frac
    return out.astype(np.float32, copy=False)


def make_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Wrap float32/pcm mono samples into a WAV file bytes blob."""
    pcm = float32_to_pcm16(samples) if samples.dtype != np.int16 else samples.tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    """Read mono float32 samples + sample_rate from a WAV bytes blob."""
    with wave.open(io.BytesIO(data), "rb") as r:
        sr = r.getframerate()
        n = r.getnframes()
        channels = r.getnchannels()
        if r.getsampwidth() != 2 or r.getcomptype() != "NONE":
            raise wave.Error("WAV is not uncompressed PCM16")
        raw = r.readframes(n)
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return arr, sr


def decode_audio_bytes(
    data: bytes,
    *,
    filename: str = "",
    content_type: str = "",
    raw_sample_rate: int = 16000,
) -> tuple[np.ndarray, int]:
    """Decode common uploaded audio containers to float32 mono.

    WAV and libsndfile-supported formats are handled in-process. WebM/Opus and
    other compressed containers use the locally installed FFmpeg binary. Raw
    PCM16 is accepted only when the filename or content type explicitly says
    that it is raw audio; compressed bytes are never guessed as PCM.
    """
    if not data:
        raise ValueError("empty audio data")

    suffix = Path(filename or "").suffix.lower()
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    raw_types = {"audio/pcm", "audio/l16"}
    raw_suffixes = {".pcm", ".raw", ".s16", ".s16le"}
    if (
        suffix in raw_suffixes
        or content_type in raw_types
        or (content_type == "application/octet-stream" and not suffix)
    ):
        return pcm16_to_float32(data), raw_sample_rate

    try:
        return read_wav_bytes(data)
    except (EOFError, ValueError, wave.Error):
        pass

    try:
        import soundfile as sf

        samples, sample_rate = sf.read(
            io.BytesIO(data),
            dtype="float32",
            always_2d=True,
        )
        return samples.mean(axis=1).astype(np.float32, copy=False), int(sample_rate)
    except Exception:
        pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError(
            "unsupported audio format; upload WAV/PCM or install FFmpeg for compressed audio"
        )

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(raw_sample_rate),
                "-f",
                "f32le",
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace").strip()
        raise ValueError(f"could not decode uploaded audio{': ' + detail if detail else ''}") from exc

    samples = np.frombuffer(result.stdout, dtype="<f4").astype(np.float32, copy=False)
    if samples.size == 0:
        raise ValueError("decoded audio is empty")
    return samples, raw_sample_rate


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))
