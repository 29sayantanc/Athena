"""OpenAI-compatible realtime voice server entrypoint with port fallback.

Loads Parakeet STT + Pocket TTS once, then serves:
  - REST:  /v1/audio/speech, /v1/audio/transcriptions, /v1/models, /v1/realtime/models, /health
  - WS:    /v1/realtime
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from typing import Any, Callable

import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from config import settings, AVAILABLE_VOICES, AVAILABLE_LANGUAGES
from stt_engine import STTEngine
from tts_engine import TTSEngine
from realtime import realtime_endpoint
from audio_utils import decode_audio_bytes, make_wav_bytes

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("server")

app = FastAPI(
    title="Voice Server",
    version="1.0.0",
    description="OpenAI-compatible realtime STT/TTS (Parakeet + Pocket TTS + Silero VAD)",
)
# Wildcard origins with credentials is an invalid CORS combination that
# browsers reject; the server uses no cookies or auth, so credentials stay off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Model singletons (lazy) ----------------------------------------------
_stt: STTEngine | None = None
_tts: TTSEngine | None = None


def get_stt() -> STTEngine:
    global _stt
    if _stt is None:
        _stt = STTEngine()
    return _stt


def get_tts() -> TTSEngine:
    global _tts
    if _tts is None:
        _tts = TTSEngine()
    return _tts


# --- Schemas (OpenAI shape) -----------------------------------------------
class SpeechRequest(BaseModel):
    model: str = "pocket-tts"
    input: str
    voice: str | None = None
    response_format: str = "pcm"  # pcm | wav
    speed: float | None = None  # ignored


class TranscriptionResponse(BaseModel):
    text: str


# --- Health ---------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "device": settings.device,
        "cuda": torch.cuda.is_available(),
        "stt_loaded": _stt is not None,
        "tts_loaded": _tts is not None,
        "stt_device": _stt.device if _stt is not None else None,
        "tts_device": _tts.device if _tts is not None else None,
        "stt_model": settings.stt_model,
        "tts_language": settings.tts_language,
        "tts_voice": settings.tts_voice,
        "vad": "silero",
    }


# --- Models catalog -------------------------------------------------------
@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "pocket-tts", "object": "model", "owned_by": "kyutai", "modality": "tts",
             "voices": AVAILABLE_VOICES, "languages": AVAILABLE_LANGUAGES},
            {"id": "parakeet-tdt-0.6b-v3", "object": "model", "owned_by": "nvidia", "modality": "stt"},
            {"id": "parakeet-pocket", "object": "model", "owned_by": "local", "modality": "realtime"},
        ],
    }


@app.get("/v1/realtime/models")
async def realtime_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "parakeet-pocket", "object": "realtime_model", "modalities": ["text", "audio"],
             "voice": settings.tts_voice, "voices": AVAILABLE_VOICES},
        ],
    }


# --- TTS: POST /v1/audio/speech ------------------------------------------
@app.post("/v1/audio/speech")
async def audio_speech(req: SpeechRequest):
    if not req.input.strip():
        raise HTTPException(400, "input cannot be empty")
    if req.voice and req.voice not in AVAILABLE_VOICES and not req.voice.startswith("hf://"):
        raise HTTPException(400, f"unknown voice: {req.voice}")
    fmt = req.response_format.lower()
    if fmt not in {"pcm", "wav"}:
        raise HTTPException(400, "response_format must be 'pcm' or 'wav'")

    tts = get_tts()

    if fmt == "wav":
        pcm = await asyncio.get_running_loop().run_in_executor(
            None, lambda: tts.synthesize(req.input, voice=req.voice)
        )
        samples = np.frombuffer(pcm, dtype="<i2")
        wav = make_wav_bytes(samples, settings.tts_sample_rate)
        return Response(
            wav,
            media_type="audio/wav",
            headers={"Content-Disposition": 'attachment; filename="speech.wav"'},
        )

    async def gen_pcm():
        async for chunk in tts.stream_pcm16(req.input, voice=req.voice):
            yield chunk
    return StreamingResponse(gen_pcm(), media_type="audio/pcm",
                             headers={"Content-Disposition": 'attachment; filename="speech.pcm"'})


# --- STT: POST /v1/audio/transcriptions ----------------------------------
@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("parakeet-tdt-0.6b-v3"),
    language: str | None = Form(None),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty audio upload")
    try:
        samples, sr = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: decode_audio_bytes(
                data,
                filename=file.filename or "",
                content_type=file.content_type or "",
                raw_sample_rate=settings.stt_sample_rate,
            ),
        )
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc

    stt = get_stt()
    text = await asyncio.get_running_loop().run_in_executor(None, lambda: stt.transcribe(samples, sr))
    return TranscriptionResponse(text=text)


# --- Realtime WS: /v1/realtime -------------------------------------------
@app.websocket("/v1/realtime")
async def ws_realtime(ws: WebSocket, model: str | None = Query(default=None)):
    await realtime_endpoint(ws, get_stt(), get_tts())


# --- Port management ------------------------------------------------------
def _is_port_available(host: str, port: int) -> bool:
    """Check if a TCP port is available for binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.bind((host, port))
            return True
        except (OSError, socket.error):
            return False


def _find_available_port(host: str, preferred: int, fallbacks: list[int]) -> int:
    """Find first available port from preferred + fallbacks list."""
    candidates = [preferred] + fallbacks
    for p in candidates:
        if _is_port_available(host, p):
            return p
    ports = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"No configured voice-server port is available ({ports})")


# --- Entrypoint -----------------------------------------------------------
def run_server(
    host: str | None = None,
    port: int | None = None,
    preload: bool = False,
    on_server_created: Callable[[Any], None] | None = None,
):
    import uvicorn

    host = host or settings.host
    port = port or settings.port

    # Find available port with fallbacks
    actual_port = _find_available_port(host, port, settings.port_fallbacks)
    if actual_port != port:
        logger.warning("Port %d busy, using %d instead.", port, actual_port)
    settings.port = actual_port

    if preload:
        logger.info("Preloading models...")
        get_stt()
        get_tts()
        logger.info("Models ready.")

    logger.info("Starting server on %s:%d", host, actual_port)
    config = uvicorn.Config(
        app,
        host=host,
        port=actual_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    if on_server_created is not None:
        on_server_created(server)
    server.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Voice Server")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--preload", action="store_true", help="Preload both models at startup")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, preload=args.preload)
