"""Settings, paths and persistence for Athena.

The core process owns settings.json. The Electron shell reads and writes it
only through the core's WebSocket API (settings.get / settings.set), except at
startup when it needs `python_path` to launch the core at all.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import secrets

log = logging.getLogger("athena.config")


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = Path(os.environ.get("ATHENA_HOME", base / "Athena"))
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = data_dir()
SETTINGS_PATH = DATA_DIR / "settings.json"
SESSIONS_DIR = DATA_DIR / "sessions"
LOGS_DIR = DATA_DIR / "logs"
MEMORY_PATH = DATA_DIR / "memory.json"
CALIBRATION_PATH = DATA_DIR / "calibration.json"
for _d in (SESSIONS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Repo layout: Athena/core/athena_core/config.py -> Athena/
REPO_ROOT = Path(__file__).resolve().parents[2]

PROVIDER_PRESETS = {
    "lmstudio": {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "needs_key": False},
    "ollama": {"name": "Ollama", "base_url": "http://localhost:11434/v1", "needs_key": False},
    "venice": {"name": "Venice", "base_url": "https://api.venice.ai/api/v1", "needs_key": True},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "needs_key": True},
    "custom": {"name": "Custom (OpenAI-compatible)", "base_url": "http://localhost:8080/v1", "needs_key": False},
}

DEFAULTS: dict[str, Any] = {
    "version": 5,
    "general": {
        "assistant_name": "Athena",
        "user_name": "",
        "hotkey": "f8",
        # hybrid: quick tap latches listening on (tap again to stop), long hold = push-to-talk
        "hotkey_mode": "hybrid",          # hybrid | hold | toggle
        "hybrid_hold_ms": 350,
        "auto_stop_on_silence": True,     # only when latched/toggled
        "silence_stop_ms": 1800,
        "max_listen_s": 90,
        "paused": False,
        "launch_at_login": False,
        "conversation_idle_reset_min": 10,
        "screenshot_retention_days": 7,
        "screenshot_scope": "cursor_monitor",  # cursor_monitor | all_monitors
        "save_screenshots": True,         # keep a copy in %APPDATA%\\Athena\\sessions
        # Replies within this many seconds of Athena's last answer are follow-ups: no new
        # screenshot is taken; the earlier one stays in context (the model can still look again).
        "followup_window_s": 180,
    },
    "control": {
        # autonomous | confirm_risky | confirm_all
        "safety_mode": "confirm_risky",
        "risky_keywords": ["send", "delete", "remove", "submit", "pay", "purchase", "buy", "order",
                            "post", "publish", "transfer", "confirm", "discard", "erase", "format",
                            "uninstall", "sign out", "log out", "unsubscribe", "archive", "reply all"],
        # take back control: any physical mouse movement past threshold, or Esc
        "takeover_mouse": True,
        "takeover_threshold_px": 40,
        "takeover_esc": True,
        "mouse_speed": "natural",         # instant | fast | natural | slow
        "settle_ms": 450,                 # wait after actions before re-screenshot
        "auto_screenshot": True,          # attach a fresh screenshot after screen-changing actions
        "max_steps": 30,
        "allow_control": True,            # master switch for mouse/keyboard
    },
    "model": {
        "active_provider": "lmstudio",
        "providers": {
            "lmstudio": {"base_url": "http://localhost:1234/v1", "api_key": "", "model": ""},
            "ollama": {"base_url": "http://localhost:11434/v1", "api_key": "", "model": ""},
            "venice": {"base_url": "https://api.venice.ai/api/v1", "api_key": "", "model": ""},
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": "", "model": ""},
            "custom": {"base_url": "http://localhost:8080/v1", "api_key": "", "model": ""},
        },
        "tool_mode": "auto",              # auto | native | json
        "supports_vision": True,
        # None = use the server's / model's own default (not sent in the request)
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "request_timeout_s": 180,
        "image_max_width": 1120,          # screenshots are downscaled to this width
        "image_round_to": 28,             # keep dims a multiple of this (Qwen-VL patch size)
        "jpeg_quality": 78,
        "keep_images": 3,                 # screenshots kept in the conversation context
        "coordinate_mode": "auto",        # auto | pixels | norm1000 | norm1
        "use_accessibility": True,        # Windows UI Automation element list
        "draw_marks": True,               # draw numbered element marks on screenshots
        "max_elements": 120,
        "extra_instructions": "",
    },
    "voice": {
        "server_url": "http://127.0.0.1:8000",
        "autostart_server": True,
        "server_dir": str(REPO_ROOT / "voice-server"),
        "server_python": "",              # blank = same interpreter as the core
        "tts_language": "english",
        "voice": "alba",
        "speak_replies": True,
        "max_spoken_words": 45,           # longer answers go to a popup
        "mic_device": None,               # None = system default
        "output_device": None,
        "stt_language": "en",
    },
    "appearance": {
        "show_idle_ring": True,
        "ring_size": 34,
        "show_captions": True,
        "hide_from_screenshots": True,    # overlay/popup excluded from screen capture
        "colors": {
            "idle": "#A7ABA3",
            "listening": "#A6B85A",
            "thinking": "#9C8CD9",
            "speaking": "#6FA8DC",
            "acting": "#C8894A",
            "confirm": "#E0B54A",
            "user": "#A6B85A",
            "error": "#D7654F",
            "highlight": "#E8BE45",
        },
        "popup_width": 440,
        "highlight_seconds": 45,          # highlights fade after this (0 = until closed / Esc)
        "popup_autoclose_s": 0,           # 0 = stays until closed
    },
    "shell": {
        "python_path": "",                # read by Electron to launch the core
        "ws_port": 47321,
    },
}

SECRET_FIELDS = ("api_key",)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate(raw: dict) -> dict:
    """Move settings saved by older versions onto new defaults (only untouched old defaults)."""
    if raw.get("version", 1) < 2:
        m = raw.setdefault("model", {})
        for key, old in (("temperature", 0.3), ("max_tokens", 2048), ("image_max_width", 1344), ("jpeg_quality", 82)):
            if m.get(key) == old:
                m.pop(key)
        raw["version"] = 2
    if raw.get("version", 1) < 3:
        a = raw.get("appearance") or {}
        if a.get("highlight_seconds") == 20:   # v3: highlights linger longer by default
            a.pop("highlight_seconds")
        raw["version"] = 3
    if raw.get("version", 1) < 4:
        # v4: redesigned palette. Colours still on the old defaults move to the new ones.
        old = {"idle": "#8B9BB4", "listening": "#3EE6C1", "thinking": "#A78BFA", "speaking": "#60A5FA",
               "acting": "#FF9F43", "confirm": "#FACC15", "user": "#4ADE80", "error": "#F87171", "highlight": "#FF4FA3"}
        cols = (raw.get("appearance") or {}).get("colors") or {}
        for k, v in list(cols.items()):
            if isinstance(v, str) and old.get(k, "").lower() == v.lower():
                cols.pop(k)
        raw["version"] = 4
    if raw.get("version", 1) < 5:
        # v5: clearer thinking/speaking colours (the v4 ones were hard to see on light apps).
        cols = (raw.get("appearance") or {}).get("colors") or {}
        for k, v in (("thinking", "#A9BCD0"), ("speaking", "#E4E2DA")):
            if str(cols.get(k, "")).lower() == v.lower():
                cols.pop(k)
        raw["version"] = 5
    return raw


class Settings:
    """Thread-safe settings store with change listeners."""

    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._listeners: list = []
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    self._data = _deep_merge(DEFAULTS, _migrate(self._decrypt(raw)))
                except Exception as exc:  # corrupt file: keep defaults, back it up
                    log.error("settings.json unreadable (%s); using defaults", exc)
                    try:
                        self.path.replace(self.path.with_suffix(".bad.json"))
                    except OSError:
                        pass
            self.save()

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._encrypt(self._data), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    @staticmethod
    def _walk_providers(data: dict, fn) -> dict:
        data = copy.deepcopy(data)
        for prov in data.get("model", {}).get("providers", {}).values():
            for f in SECRET_FIELDS:
                if f in prov:
                    prov[f] = fn(prov[f])
        return data

    def _encrypt(self, data: dict) -> dict:
        return self._walk_providers(data, secrets.protect)

    def _decrypt(self, data: dict) -> dict:
        return self._walk_providers(data, secrets.unprotect)

    # -- access ------------------------------------------------------------
    def get(self, dotted: str | None = None, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            if not dotted:
                return copy.deepcopy(node)
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def public(self) -> dict:
        """Settings for the UI: secrets masked."""
        def mask(v):
            return ("•" * 8 + v[-4:]) if isinstance(v, str) and len(v) > 4 else ("•" * len(v) if v else "")
        return self._walk_providers(self.get(), mask)

    def update(self, patch: dict) -> dict:
        """Deep-merge a patch. Masked secrets (starting with •) are ignored."""
        with self._lock:
            patch = copy.deepcopy(patch)
            for prov in patch.get("model", {}).get("providers", {}).values():
                for f in SECRET_FIELDS:
                    if isinstance(prov.get(f), str) and prov[f].startswith("•"):
                        prov.pop(f)
            self._data = _deep_merge(self._data, patch)
            self.save()
            snapshot = self.get()
        for cb in list(self._listeners):
            try:
                cb(patch, snapshot)
            except Exception:
                log.exception("settings listener failed")
        return snapshot

    def set(self, dotted: str, value: Any) -> dict:
        patch: dict = {}
        node = patch
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        return self.update(patch)

    def on_change(self, cb) -> None:
        self._listeners.append(cb)

    # -- convenience -------------------------------------------------------
    def provider(self) -> dict:
        key = self.get("model.active_provider")
        prov = self.get(f"model.providers.{key}") or {}
        prov["id"] = key
        return prov
