"""Focused regression tests that do not load the STT or TTS model weights."""
from __future__ import annotations

import json
import unittest
import shutil
import subprocess
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from audio_utils import (
    decode_audio_bytes,
    float32_to_pcm16,
    make_wav_bytes,
)
from stt_engine import SileroVADStream


class AudioDecodeTests(unittest.TestCase):
    def test_wav_round_trip(self):
        samples = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
        encoded = make_wav_bytes(samples, 16000)

        decoded, sample_rate = decode_audio_bytes(
            encoded,
            filename="recording.wav",
            content_type="audio/wav",
        )

        self.assertEqual(sample_rate, 16000)
        self.assertEqual(decoded.shape, samples.shape)
        np.testing.assert_allclose(decoded, samples, atol=2 / 32768)

    def test_explicit_raw_pcm_is_supported(self):
        samples = np.array([-0.25, 0.0, 0.25], dtype=np.float32)
        decoded, sample_rate = decode_audio_bytes(
            float32_to_pcm16(samples),
            filename="recording.pcm",
            content_type="audio/pcm",
        )

        self.assertEqual(sample_rate, 16000)
        np.testing.assert_allclose(decoded, samples, atol=1 / 32768)

    def test_compressed_bytes_are_not_guessed_as_pcm(self):
        with patch("audio_utils.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "unsupported audio format"):
                decode_audio_bytes(
                    b"not-a-real-webm-container",
                    filename="recording.webm",
                    content_type="audio/webm",
                )

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_webm_opus_decodes_through_ffmpeg(self):
        samples = np.sin(np.linspace(0, np.pi * 8, 3200, dtype=np.float32)) * 0.2
        wav = make_wav_bytes(samples, 16000)
        encoded = subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-f",
                "webm",
                "pipe:1",
            ],
            input=wav,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout

        decoded, sample_rate = decode_audio_bytes(
            encoded,
            filename="recording.webm",
            content_type="audio/webm",
        )

        self.assertEqual(sample_rate, 16000)
        self.assertGreater(decoded.size, 0)


class _FakeEngine:
    def transcribe(self, samples, sample_rate):
        return f"heard {len(samples)} samples at {sample_rate}"


class _FakeVad:
    def reset_states(self):
        return None


class VadCommitTests(unittest.TestCase):
    def test_commit_flushes_buffer_and_emits_final(self):
        stream = SileroVADStream.__new__(SileroVADStream)
        stream.engine = _FakeEngine()
        stream.sr = 16000
        stream.vad = _FakeVad()
        stream._pending = deque([0.1, -0.1])
        stream._preroll = deque()
        stream.buffer = []
        stream.input_buffer = [np.zeros(512, dtype=np.float32)]
        stream.in_speech = True
        stream.utterance_ms = 32
        stream.last_partial_ms = 0

        events = stream.commit()

        self.assertEqual(events[0][0], "speech_stopped")
        self.assertEqual(events[1], ("final", "heard 514 samples at 16000"))
        self.assertFalse(stream.in_speech)
        self.assertEqual(stream.input_buffer, [])


class RestEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import server

        self.server = server
        self.client = TestClient(server.app)

    def test_transcription_accepts_app_wav(self):
        class FakeStt:
            def transcribe(self, samples, sample_rate):
                self.sample_rate = sample_rate
                self.sample_count = len(samples)
                return "local transcript"

        fake = FakeStt()
        samples = np.zeros(1600, dtype=np.float32)
        wav = make_wav_bytes(samples, 16000)
        with patch.object(self.server, "get_stt", return_value=fake):
            response = self.client.post(
                "/v1/audio/transcriptions",
                files={"file": ("recording.wav", wav, "audio/wav")},
                data={"model": "parakeet-tdt-0.6b-v3"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "local transcript"})
        self.assertEqual(fake.sample_rate, 16000)
        self.assertEqual(fake.sample_count, 1600)

    def test_tts_wav_has_complete_header(self):
        class FakeTts:
            def synthesize(self, text, voice=None):
                return float32_to_pcm16(np.zeros(800, dtype=np.float32))

        with patch.object(self.server, "get_tts", return_value=FakeTts()):
            response = self.client.post(
                "/v1/audio/speech",
                json={
                    "model": "pocket-tts",
                    "input": "Hello",
                    "voice": "alba",
                    "response_format": "wav",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content[:4], b"RIFF")
        self.assertEqual(response.content[8:12], b"WAVE")
        self.assertEqual(len(response.content), 44 + 1600)


class RealtimeCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_commit_emits_completed_transcript(self):
        from realtime import RealtimeSession

        class FakeWebSocket:
            def __init__(self):
                self.events = []

            async def send_text(self, payload):
                self.events.append(payload)

        class FakeStream:
            threshold = 0.5
            min_silence_ms = 500
            speech_pad_ms = 200
            min_speech_ms = 250

            def configure(self, **kwargs):
                for key, value in kwargs.items():
                    if value is not None:
                        setattr(self, key, value)
                return self.describe()

            def describe(self):
                return {'threshold': self.threshold}

            def commit(self):
                return [("final", "hello from commit")]

            def reset(self):
                return None

        ws = FakeWebSocket()
        with patch("realtime.SileroVADStream", return_value=FakeStream()):
            session = RealtimeSession(ws, SimpleNamespace(), SimpleNamespace())
        session.create_response = False
        await session._handle_event({"type": "input_audio_buffer.commit"})

        self.assertTrue(any("hello from commit" in event for event in ws.events))

    async def test_session_can_disable_auto_echo_per_connection(self):
        from realtime import RealtimeSession

        class FakeWebSocket:
            def __init__(self):
                self.events = []

            async def send_text(self, payload):
                self.events.append(payload)

        class FakeStream:
            threshold = 0.5
            min_silence_ms = 500
            speech_pad_ms = 200
            min_speech_ms = 250

            def configure(self, **kwargs):
                for key, value in kwargs.items():
                    if value is not None:
                        setattr(self, key, value)
                return self.describe()

            def describe(self):
                return {'threshold': self.threshold}

            def reset(self):
                return None

        ws = FakeWebSocket()
        with patch("realtime.SileroVADStream", return_value=FakeStream()):
            session = RealtimeSession(ws, SimpleNamespace(), SimpleNamespace())
        session.create_response = True
        await session._handle_event({
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "turn_detection": {"type": "server_vad", "create_response": False},
            },
        })

        self.assertFalse(session.create_response)
        self.assertTrue(any('"create_response": false' in event for event in ws.events))


if __name__ == "__main__":
    unittest.main()


class VadTuningTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_detection_is_retunable_from_session_update(self):
        from realtime import RealtimeSession

        class FakeWebSocket:
            def __init__(self):
                self.events = []

            async def send_text(self, payload):
                self.events.append(payload)

        class FakeStream:
            threshold = 0.5
            min_silence_ms = 500
            speech_pad_ms = 200
            min_speech_ms = 250

            def __init__(self):
                self.configured = None

            def configure(self, **kwargs):
                self.configured = kwargs
                for key, value in kwargs.items():
                    if value is not None:
                        setattr(self, key, value)
                return self.describe()

            def describe(self):
                return {"threshold": self.threshold}

            def reset(self):
                return None

        stream = FakeStream()
        ws = FakeWebSocket()
        with patch("realtime.SileroVADStream", return_value=stream):
            session = RealtimeSession(ws, SimpleNamespace(), SimpleNamespace())

        await session._handle_event({
            "type": "session.update",
            "session": {
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.8,
                    "silence_duration_ms": 900,
                    "create_response": False,
                }
            },
        })

        self.assertEqual(stream.threshold, 0.8)
        self.assertEqual(stream.min_silence_ms, 900)
        self.assertFalse(session.create_response)
        # The acknowledgement reports the values now in force.
        snapshot = json.loads(ws.events[-1])["session"]["turn_detection"]
        self.assertEqual(snapshot["threshold"], 0.8)
        self.assertEqual(snapshot["silence_duration_ms"], 900)

    def test_configure_clamps_out_of_range_values(self):
        from stt_engine import SileroVADStream

        stream = SileroVADStream.__new__(SileroVADStream)
        stream.sr = 16000
        stream.chunk_size = 512
        stream.threshold = 0.5
        stream.min_silence_ms = 500
        stream.speech_pad_ms = 200
        stream.min_speech_ms = 250
        stream._pending = deque()
        stream._preroll = deque(maxlen=2)
        stream.buffer = []
        stream.input_buffer = []
        stream.in_speech = False
        stream.utterance_ms = 0
        stream.last_partial_ms = 0
        stream.vad = SimpleNamespace(reset_states=lambda: None)
        stream._build_iterator = lambda: None

        result = stream.configure(threshold=9.9, min_silence_ms=1, speech_pad_ms=99999)
        self.assertEqual(result["threshold"], 0.95)
        self.assertEqual(result["min_silence_ms"], 100)
        self.assertEqual(result["speech_pad_ms"], 2000)
