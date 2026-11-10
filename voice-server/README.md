# Voice Server (OpenAI Realtime API Compatible)

A self-hosted realtime voice server exposing **OpenAI-compatible endpoints** for bidirectional streaming speech-to-text (STT) and text-to-speech (TTS).

## Engines

| Component | Model | Source |
|-----------|-------|--------|
| **STT** | NVIDIA Parakeet TDT 0.6B v3 | `nvidia/parakeet-tdt-0.6b-v3` via HuggingFace `transformers` |
| **TTS** | Kyutai Pocket TTS 1.6B | `kyutai/pocket-tts` via the `pocket-tts` package |
| **VAD** | Silero VAD | `silero-vad` — semantic voice activity detection for uninterrupted conversations |

## Features

- **Silero VAD** — proper semantic voice activity detection (not just energy threshold) for accurate turn boundaries and barge-in support
- **Barge-in** — if the user speaks while TTS is playing, TTS is interrupted automatically
- **Port fallback** — if port 8000 is busy, automatically tries 8001, 8002, 8080, 8765, 9000, then 5000
- **TUI launcher** — configure language, voice, host, port, and auto-echo from a rich terminal UI
- **26 voices** — all predefined Pocket TTS voices (alba, jean, cosette, marius, etc.)
- **12 languages** — English, French, German, Italian, Portuguese, Spanish (with variants)
- **Streaming** — WebSocket Realtime API + REST streaming endpoints
- **Client discovery** — clients (such as Athena) check the configured endpoint and every known fallback port, then load the server's voice catalog

## Requirements

- Python 3.11
- NVIDIA GPU with CUDA 12.8+ (tested on RTX 5080, Blackwell sm_120)
- PyTorch 2.11+ cu128 build
- ~5GB VRAM (Parakeet ~1.5GB + Pocket TTS ~3GB)

## Quick Start

### Option 1: TUI Launcher (recommended)

Double-click **`start_server.bat`** or run:

```bat
start_server.bat
```

The TUI lets you:
- Select TTS language (12 options)
- Select TTS voice (26 predefined voices)
- Set host and port
- Toggle auto-echo mode (STT → TTS demo)
- Start/stop the server
- View live logs in a scrolling panel

**Keyboard shortcuts:**
- `S` — Start server
- `X` — Stop server
- `Q` — Quit

### Option 2: Direct command line

```bash
python server.py --host 0.0.0.0 --port 8000 --preload
```

The server loads both models on startup (first run downloads weights):
- Parakeet STT (~1.5 GB) to `~/.cache/huggingface`
- Pocket TTS (~3 GB) to `~/.cache/huggingface`
- Silero VAD (~5 MB, cached by torch hub)

## Install

```bash
python -m pip install -r requirements.txt
```

Key packages: `torch` (cu128), `transformers>=4.57`, `pocket-tts`, `silero-vad`, `fastapi`, `uvicorn`, `textual`, `numpy<2.3`, `librosa`.

## OpenAI-compatible endpoints

### REST (OpenAI `/v1/...` shape)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/audio/speech` | POST | OpenAI TTS shape. Body: `{model, input, voice, response_format}`. Streams audio bytes (default `pcm` → raw 24kHz PCM16; `wav` → full WAV). |
| `/v1/audio/transcriptions` | POST | OpenAI STT shape. Multipart upload of WAV, raw PCM16, or an FFmpeg-decodable audio container. Returns `{text}`. |
| `/v1/models` | GET | Lists available models with voices and languages. |
| `/v1/realtime/models` | GET | Realtime session model catalog. |
| `/health` | GET | Liveness + model load status. |

### Realtime WebSocket

- `WS /v1/realtime?model=...` — OpenAI Realtime API session.
  - Client sends JSON events: `session.update`, `input_audio_buffer.append`, `input_audio_buffer.commit`, `response.create`
  - Server streams back: `input_audio_buffer.speech_started`, `input_audio_buffer.speech_stopped`, `conversation.item.input_audio_transcription.completed`, `conversation.item.input_audio_transcription.partial`, `response.audio.delta` (PCM16 chunks), `response.audio_transcript.delta`, `response.done`, `response.cancel` (barge-in)
  - Audio format: 24kHz PCM16 little-endian mono for TTS; 16kHz PCM16 mono for STT input
  - VAD: Silero semantic detection with configurable threshold, silence duration, and speech padding

## Available Voices

`alba`, `anna`, `azelma`, `bill_boerst`, `caro_davy`, `charles`, `cosette`, `eponine`, `estelle`, `eve`, `fantine`, `george`, `giovanni`, `jane`, `javert`, `jean`, `juergen`, `lola`, `marius`, `mary`, `michael`, `paul`, `peter_yearsley`, `rafael`, `stuart_bell`, `vera`

## Available Languages

`english`, `english_2026-01`, `english_2026-04`, `french_24l`, `german`, `german_24l`, `italian`, `italian_24l`, `portuguese`, `portuguese_24l`, `spanish`, `spanish_24l`

## Config

Env vars (all optional):

| Var | Default | Description |
|-----|---------|-------------|
| `STT_MODEL` | `nvidia/parakeet-tdt-0.6b-v3` | STT model ID |
| `TTS_LANGUAGE` | `english` | TTS language config |
| `TTS_VOICE` | `alba` | Default voice |
| `DEVICE` | `cuda` | Device (falls back to cpu) |
| `PORT` | `8000` | Preferred port |
| `VAD_THRESHOLD` | `0.5` | Silero VAD sensitivity (0-1, lower = more sensitive) |
| `VAD_MIN_SILENCE_MS` | `500` | Min silence to end utterance |
| `VAD_SPEECH_PAD_MS` | `200` | Padding before/after speech |
| `BARGE_IN_RMS` | `0.025` | Fast RMS gate used to interrupt active TTS before full VAD completes |
| `STT_MIN_CHUNK_MS` | `300` | Partial transcription interval |
| `STT_PARTIAL_RESULTS` | `0` | Re-run Parakeet for rolling partial transcripts; disabled by default for lower latency |
| `TTS_FRAME_MS` | `100` | Audio frame size for WS streaming |
| `AUTO_ECHO` | `0` | Auto-speak STT result (demo mode); Athena keeps this disabled |
| `LOG_LEVEL` | `INFO` | Logging level |

## Architecture

```
┌─────────────────────────────────────────┐
│              Voice Server                │
├──────────┬──────────┬───────────────────┤
│  STT     │  TTS     │  VAD              │
│ Parakeet │ Pocket   │ Silero            │
│  TDT     │  TTS     │ (semantic)        │
│ 0.6B v3  │ 1.6B     │                   │
├──────────┴──────────┴───────────────────┤
│         FastAPI + Uvicorn               │
│    REST + WebSocket (OpenAI API)        │
├─────────────────────────────────────────┤
│           TUI (Textual)                 │
│    start_server.bat → tui.py            │
└─────────────────────────────────────────┘
```

## Client usage (OpenAI SDK style)

```js
// TTS (OpenAI compatible)
const resp = await fetch('http://localhost:8000/v1/audio/speech', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer dummy'},
  body: JSON.stringify({model: 'pocket-tts', input: 'Hello world', voice: 'alba', response_format: 'pcm'})
});
// resp.body is a streaming ReadableStream of PCM16 24kHz
```

```python
# Realtime session via websockets
import asyncio, websockets, json, base64
async def run():
    async with websockets.connect('ws://localhost:8000/v1/realtime?model=parakeet-pocket') as ws:
        await ws.send(json.dumps({'type':'session.update','session':{'modalities':['text','audio']}}))
        # stream mic bytes (16k PCM16) ...
        # receive response.audio.delta bytes ...
asyncio.run(run())
```

## File Structure

```
voice-server/
├── start_server.bat      # Windows launcher (double-click to run)
├── tui.py                # Textual TUI for configuration + start
├── server.py             # FastAPI app + entrypoint with port fallback
├── realtime.py           # WebSocket Realtime API session handler
├── stt_engine.py         # Parakeet STT + Silero VAD stream
├── tts_engine.py         # Pocket TTS streaming engine
├── audio_utils.py        # PCM/WAV conversion + resampling
├── config.py             # Settings, voices, languages catalog
├── requirements.txt      # Python dependencies
├── test_voice_server.py  # Lightweight regression tests (model weights are not loaded)
└── README.md             # This file
```

## Tests

```bash
python -B -m unittest -v test_voice_server.py
```

The tests cover WAV/PCM decoding, rejection of mislabeled compressed bytes,
REST STT/TTS compatibility, realtime buffer commit, and VAD commit behavior
without loading the model weights.
