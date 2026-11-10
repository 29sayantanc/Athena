"""Central configuration for the voice server."""
import os
from dataclasses import dataclass, field


# All available Pocket TTS voices (from kyutai/pocket-tts predefined catalog)
AVAILABLE_VOICES = [
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "estelle", "eve", "fantine", "george",
    "giovanni", "jane", "javert", "jean", "juergen", "lola",
    "marius", "mary", "michael", "paul", "peter_yearsley",
    "rafael", "stuart_bell", "vera",
]

# All available Pocket TTS languages
AVAILABLE_LANGUAGES = [
    "english", "english_2026-01", "english_2026-04",
    "french_24l", "german", "german_24l",
    "italian", "italian_24l", "portuguese", "portuguese_24l",
    "spanish", "spanish_24l",
]

# Default voice per language
DEFAULT_VOICE_FOR_LANG = {
    "english": "alba", "english_2026-01": "alba", "english_2026-04": "alba",
    "french_24l": "estelle",
    "german": "juergen", "german_24l": "juergen",
    "italian": "giovanni", "italian_24l": "giovanni",
    "portuguese": "rafael", "portuguese_24l": "rafael",
    "spanish": "lola", "spanish_24l": "lola",
}


@dataclass
class Settings:
    # STT (Parakeet)
    stt_model: str = os.environ.get("STT_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
    stt_sample_rate: int = int(os.environ.get("SAMPLE_RATE_STT", "16000"))
    stt_language: str = os.environ.get("STT_LANGUAGE", "en")

    # TTS (Pocket TTS)
    tts_language: str = os.environ.get("TTS_LANGUAGE", "english")
    tts_voice: str = os.environ.get("TTS_VOICE", "alba")
    tts_sample_rate: int = int(os.environ.get("SAMPLE_RATE_TTS", "24000"))
    tts_quantize: bool = os.environ.get("TTS_QUANTIZE", "0") == "1"

    # Runtime
    device: str = os.environ.get("DEVICE", "cuda")
    # Loopback by default: the server is unauthenticated with open CORS, so
    # binding all interfaces exposed STT/TTS to the whole LAN. Set HOST or
    # pass --host 0.0.0.0 explicitly to serve other machines.
    host: str = os.environ.get("HOST", "127.0.0.1")
    port: int = int(os.environ.get("PORT", "8000"))
    port_fallbacks: list[int] = field(default_factory=lambda: [8001, 8002, 8080, 8765, 9000, 5000])
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

    # VAD (Silero)
    vad_threshold: float = float(os.environ.get("VAD_THRESHOLD", "0.5"))
    vad_min_silence_ms: int = int(os.environ.get("VAD_MIN_SILENCE_MS", "500"))
    vad_speech_pad_ms: int = int(os.environ.get("VAD_SPEECH_PAD_MS", "200"))
    vad_min_speech_ms: int = int(os.environ.get("VAD_MIN_SPEECH_MS", "250"))
    barge_in_rms: float = float(os.environ.get("BARGE_IN_RMS", "0.025"))

    # STT streaming
    stt_min_chunk_ms: int = int(os.environ.get("STT_MIN_CHUNK_MS", "300"))
    stt_partial_results: bool = os.environ.get("STT_PARTIAL_RESULTS", "0") == "1"

    # TTS streaming
    tts_frame_ms: int = int(os.environ.get("TTS_FRAME_MS", "100"))

    # Conversation behavior
    # When True, STT final transcript is automatically echoed back as TTS (demo mode).
    # In production, set False and have an LLM generate the response.
    auto_echo: bool = os.environ.get("AUTO_ECHO", "0") == "1"


settings = Settings()


def resolve_device() -> str:
    import torch
    if settings.device == "cuda" and not torch.cuda.is_available():
        return "cpu"
    return settings.device
