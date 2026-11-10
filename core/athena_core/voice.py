"""Client for the local voice-server (Parakeet STT + Pocket TTS) + optional autostart."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .audio import pcm16_wav
from .config import LOGS_DIR

log = logging.getLogger("athena.voice")

FALLBACK_PORTS = [8000, 8001, 8002, 8080, 8765, 9000, 5000]


def split_sentences(text: str, max_chars: int = 220) -> list[str]:
    """Split for incremental TTS so the first words play quickly."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'(])", text)
    out: list[str] = []
    for p in parts:
        while len(p) > max_chars:
            cut = p.rfind(", ", 0, max_chars)
            cut = cut + 1 if cut > 40 else max_chars
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


def clean_for_speech(text: str) -> str:
    """Strip thinking blocks and markdown so TTS never reads reasoning or symbols aloud."""
    from .llm import strip_think
    t = strip_think(text or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_#>|~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


class VoiceClient:
    def __init__(self, settings):
        self.settings = settings
        self._proc: subprocess.Popen | None = None
        # Local server: never route through a system proxy.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=3.0), trust_env=False)
        self.base_url = settings.get("voice.server_url").rstrip("/")
        self.status = "unknown"
        self._on_status = None
        self._wake = asyncio.Event()
        self._logf = None
        self._started_at = 0.0
        self._last_problem = ""

    async def close(self):
        await self._client.aclose()

    # -- discovery / health ----------------------------------------------------
    async def _healthy(self, url: str) -> bool:
        try:
            r = await self._client.get(url + "/health", timeout=2.0)
            return r.status_code == 200 and r.json().get("status") == "healthy"
        except Exception:
            return False

    async def find_server(self) -> str | None:
        configured = self.settings.get("voice.server_url").rstrip("/")
        if await self._healthy(configured):
            self.base_url = configured
            return configured
        u = urlparse(configured)
        host = u.hostname or "127.0.0.1"
        for port in FALLBACK_PORTS:
            url = f"{u.scheme or 'http'}://{host}:{port}"
            if url != configured and await self._healthy(url):
                self.base_url = url
                return url
        return None

    # -- supervision -----------------------------------------------------------
    def kick(self) -> None:
        """Re-check (and start, if needed) right away instead of waiting for the next tick."""
        self._wake.set()

    def _set_status(self, s: str) -> None:
        if s != self.status:
            self.status = s
            log.info("voice server: %s", s)
            if self._on_status:
                self._on_status(s)

    def _launch(self) -> bool:
        server_dir = Path(self.settings.get("voice.server_dir"))
        if not (server_dir / "server.py").exists():
            log.error("voice server not found at %s", server_dir)
            return False
        py = self.settings.get("voice.server_python") or sys.executable
        port = urlparse(self.settings.get("voice.server_url")).port or 8000
        env = dict(os.environ, TTS_LANGUAGE=self.settings.get("voice.tts_language"),
                   TTS_VOICE=self.settings.get("voice.voice"), PORT=str(port),
                   PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if self._logf:
            self._logf.close()
        self._logf = open(LOGS_DIR / "voice-server.log", "ab")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._logf.write(f"\n===== {stamp} starting: {py} server.py --preload --port {port} (in {server_dir})\n".encode())
        self._logf.flush()
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        log.info("starting voice server: %s server.py --preload --port %s", py, port)
        try:
            self._proc = subprocess.Popen([py, "server.py", "--preload", "--port", str(port)],
                                          cwd=server_dir, env=env, stdout=self._logf, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as exc:
            log.error("could not launch voice server: %s", exc)
            self._last_problem = f"Could not launch the voice server: {exc}"
            return False
        self._started_at = time.monotonic()
        return True

    @staticmethod
    def log_tail(lines: int = 40) -> list[str]:
        try:
            with open(LOGS_DIR / "voice-server.log", "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 16000))
                text = f.read().decode("utf-8", "replace")
            return [ln for ln in text.splitlines() if ln.strip()][-lines:]
        except OSError:
            return []

    def _crash_reason(self) -> str:
        tail = self.log_tail()
        for ln in reversed(tail):
            if re.search(r"(Error|Exception|error:|No module named|CUDA|Traceback)", ln):
                return ln.strip()[:240]
        return tail[-1].strip()[:240] if tail else "no output"

    async def supervise(self, on_status=None, on_problem=None) -> None:
        """Keep the voice server available for as long as Athena runs.

        Finds a server that's already running (any of its fallback ports), otherwise starts one and
        waits for as long as it is still loading models, restarts it if it crashes (with the reason),
        and notices when it comes back if it was started by hand.
        """
        self._on_status = on_status
        crashes = 0
        while True:
            self._wake.clear()
            delay = 3.0
            if await self.find_server():
                self._set_status("ready")
                crashes = 0
                self._last_problem = ""
                delay = 15.0
            elif self._proc is not None and self._proc.poll() is None:
                self._set_status("starting")   # ours, still loading models
                delay = 2.0
            else:
                if self._proc is not None:     # ours, and it exited
                    code = self._proc.returncode
                    self._proc = None
                    crashes += 1
                    reason = self._crash_reason()
                    log.error("voice server exited (code %s): %s", code, reason)
                    if on_problem and reason != self._last_problem:
                        on_problem(f"Voice server stopped: {reason}")
                    self._last_problem = reason
                if not self.settings.get("voice.autostart_server"):
                    self._set_status("offline")
                    delay = 5.0
                elif crashes >= 3:
                    self._set_status("crashed")  # stop retrying until asked (Settings → Voice → Start)
                    delay = 3600.0
                elif self._launch():
                    self._set_status("starting")
                    delay = 2.0
                else:
                    self._set_status("missing")
                    if on_problem and self._last_problem:
                        on_problem(self._last_problem)
                    delay = 30.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                if self.status == "crashed":
                    crashes = 0                # an explicit Start resets the retry budget
            except asyncio.TimeoutError:
                pass

    async def quick_check(self) -> bool:
        if await self._healthy(self.base_url):
            self._set_status("ready")
            return True
        return False

    def shutdown(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # -- API -------------------------------------------------------------------
    async def voices(self) -> list[str]:
        try:
            r = await self._client.get(self.base_url + "/v1/models", timeout=4.0)
            for m in r.json().get("data", []):
                if m.get("modality") == "tts":
                    return m.get("voices", [])
        except Exception:
            pass
        return []

    async def transcribe(self, pcm16: bytes) -> str:
        wav = pcm16_wav(pcm16)
        data = {"model": "parakeet-tdt-0.6b-v3"}
        lang = self.settings.get("voice.stt_language")
        if lang:
            data["language"] = lang
        r = await self._client.post(self.base_url + "/v1/audio/transcriptions",
                                    files={"file": ("speech.wav", wav, "audio/wav")}, data=data)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    async def stream_tts(self, text: str, voice: str | None = None):
        """Async generator of 24 kHz PCM16 chunks."""
        body = {"model": "pocket-tts", "input": text, "voice": voice or self.settings.get("voice.voice"),
                "response_format": "pcm"}
        async with self._client.stream("POST", self.base_url + "/v1/audio/speech", json=body) as r:
            r.raise_for_status()
            carry = b""
            async for chunk in r.aiter_bytes():
                chunk = carry + chunk
                cut = len(chunk) - (len(chunk) % 2)
                carry = chunk[cut:]
                if cut:
                    yield chunk[:cut]
