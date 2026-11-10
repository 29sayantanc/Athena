"""Orchestrates hotkey → screenshot → mic → STT → agent → voice/popup/overlay.

Also implements the agent's Env interface and the RPC surface for the shell.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import screen, uia, win32
from .agent import Agent
from .audio import Recorder, Speaker, list_devices
from .bus import Bus
from .calibration import LABELS, TARGET_FRACTIONS, CoordMapper, parse_points
from .config import DATA_DIR, LOGS_DIR, PROVIDER_PRESETS, SESSIONS_DIR, Settings
from .hooks import InputHooks
from .llm import LLMClient, LLMError, extract_json
from .memory import Memory
from .screen import Frame
from .voice import VoiceClient, clean_for_speech, split_sentences

log = logging.getLogger("athena.controller")

YES_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|send it|confirm|approved?|please do|correct|affirmative)\b", re.I)
NO_RE = re.compile(r"\b(no|nope|don'?t|do not|stop|cancel|wait|hold on|never ?mind|abort)\b", re.I)


class Controller:
    def __init__(self, settings: Settings, bus: Bus):
        self.settings = settings
        self.bus = bus
        self.loop = asyncio.get_running_loop()
        self.voice = VoiceClient(settings)
        self.llm = LLMClient(settings)
        self.memory = Memory()
        self.mapper = CoordMapper()
        self.agent = Agent(settings, self.llm, self, self.mapper, self.memory)
        cs = self.loop.call_soon_threadsafe
        self.recorder = Recorder(on_level=lambda v: cs(self._level, "mic", v),
                                 on_silence=lambda: cs(self._on_silence))
        self.speaker = Speaker(on_level=lambda v: cs(self._level, "tts", v))
        self.speaker.device = settings.get("voice.output_device")
        self.hooks = InputHooks(self.loop, settings.get("general.hotkey"), self._on_hotkey,
                                self._on_takeover, self._on_user_click, on_user_wheel=self._on_user_wheel)
        self.state = "idle"
        self.listening = False
        self.latched = False
        self.press_t = 0.0
        self.shot_task: asyncio.Task | None = None
        self.agent_task: asyncio.Task | None = None
        self.listen_timer: asyncio.Task | None = None
        self.confirm_future: asyncio.Future | None = None
        self.click_future: asyncio.Future | None = None
        self.calib_future: asyncio.Future | None = None
        self.tts_lock = asyncio.Lock()
        self.speech_tasks: set[asyncio.Task] = set()
        self._shot_n = 0
        self.followup = False
        self.press_window: dict | None = None
        self.chat_log: list[dict] = _load_chat()
        self._hl_anchor = None
        self._hl_until = 0.0
        self._hl_task: asyncio.Task | None = None
        self.turn_id = ""
        self._chat_save_handle = None
        self.chat_visible = False
        self.chat_confirm_id: str | None = None
        settings.on_change(self._settings_changed)
        self._register_rpc()

    # ================================================================ lifecycle
    async def start(self) -> None:
        if self.chat_log and self.chat_log[-1].get("role") != "divider":
            self.chat_post("divider", "New conversation")   # each launch starts a fresh conversation
        self.hooks.start()
        self.bus.on_connect = self._on_shell_connect
        self._voice_task = asyncio.ensure_future(self.voice.supervise(
            on_status=self._voice_status, on_problem=lambda m: self.toast(m, "error")))
        asyncio.ensure_future(asyncio.to_thread(self._cleanup_sessions))
        self.set_state("paused" if self.settings.get("general.paused") else "idle")

    async def shutdown(self) -> None:
        if getattr(self, "_voice_task", None):
            self._voice_task.cancel()
        self.interrupt()
        self.hooks.stop()
        self.recorder.stop_stream()
        self.speaker.close()
        self.voice.shutdown()
        await self.voice.close()
        await self.llm.close()

    async def _voice_boot(self) -> None:
        self.voice.kick()

    def _voice_status(self, s: str) -> None:
        self.bus.emit("voice.status", {"status": s})
        self.bus.emit("state", self._state_payload())
        if s == "ready" and getattr(self, "_voice_warned", False):
            self.toast("Voice server is ready.", "info")
            self._voice_warned = False
        elif s in ("offline", "crashed", "missing") and not getattr(self, "_voice_warned", False):
            self._voice_warned = True
            self.toast({"offline": "Voice server isn't running. Replies will show as text.",
                        "crashed": "Voice server keeps stopping. See Settings, Voice.",
                        "missing": "Voice server folder not found. Check Settings, Voice."}[s], "warn")

    def _cleanup_sessions(self) -> None:
        days = int(self.settings.get("general.screenshot_retention_days") or 0)
        if days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=days)
        for d in SESSIONS_DIR.iterdir():
            try:
                if d.is_dir() and datetime.strptime(d.name, "%Y-%m-%d") < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except ValueError:
                continue

    async def _on_shell_connect(self) -> None:
        self.bus.emit("settings.changed", self.settings.public())
        self.bus.emit("state", self._state_payload())
        self.bus.emit("voice.status", {"status": self.voice.status})

    def _settings_changed(self, patch: dict, snapshot: dict) -> None:
        g = patch.get("general", {})
        if "hotkey" in g:
            self.hooks.set_hotkey(snapshot["general"]["hotkey"])
        if "paused" in g:
            if snapshot["general"]["paused"]:
                self.interrupt()
                self._abort_listening()
                self.set_state("paused")
            else:
                self.set_state("idle")
        v = patch.get("voice", {})
        if "output_device" in v:
            self.speaker.set_device(snapshot["voice"]["output_device"])
        if "server_url" in v:
            self.voice.base_url = snapshot["voice"]["server_url"].rstrip("/")
            self._voice_task = asyncio.ensure_future(self.voice.supervise(
            on_status=self._voice_status, on_problem=lambda m: self.toast(m, "error")))
        self.bus.emit("settings.changed", self.settings.public())

    # ==================================================================== state
    def _state_payload(self, detail: str = "") -> dict:
        return {"state": self.state, "detail": detail, "paused": bool(self.settings.get("general.paused")),
                "driving": bool(self.agent.driving), "model": self.llm.model_id(),
                "voice": self.voice.status}

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        # Esc cancels listening/thinking/speaking, and clears highlights when idle.
        self.hooks.esc_always = True
        self.bus.emit("state", self._state_payload(detail))

    def _level(self, kind: str, v: float) -> None:
        self.bus.emit("level", {kind: round(v, 3)})

    def caption(self, text: str, kind: str = "status") -> None:
        if self.settings.get("appearance.show_captions"):
            self.bus.emit("caption", {"text": text, "kind": kind})

    def toast(self, text: str, level: str = "info") -> None:
        self.bus.emit("toast", {"text": text, "level": level})

    def feedback(self, kind: str, x: int, y: int) -> None:
        self.bus.emit("feedback", {"kind": kind, "x": x, "y": y})

    # =================================================================== hotkey
    def _on_hotkey(self, ev: str) -> None:
        if self.settings.get("general.paused") or self.calib_future is not None:
            return
        mode = self.settings.get("general.hotkey_mode")
        now = time.monotonic()
        if ev == "down":
            if self.listening:
                if mode in ("toggle", "hybrid"):
                    self._finish_listening()
                return
            self.press_t = now
            self._start_listening()
        elif ev == "up":
            if not self.listening or self.latched:
                return
            held_ms = (now - self.press_t) * 1000
            if mode == "hold" or (mode == "hybrid" and held_ms >= self.settings.get("general.hybrid_hold_ms")):
                self._finish_listening()
            else:
                self.latched = True
                self.recorder.silence_ms = int(self.settings.get("general.silence_stop_ms") or 1800)
                self.recorder.watch_silence = bool(self.settings.get("general.auto_stop_on_silence"))
                self.caption("Listening. Tap again to send.", "status")
        elif ev == "cancel":
            if self.listening and now - self.press_t < 1.0:
                self._abort_listening()

    def _start_listening(self, source: str = "hotkey") -> None:
        answering = self.confirm_future is not None and not self.confirm_future.done()
        if not answering:
            self.interrupt()
        else:
            self.stop_speaking()
        self.latched = False
        self.recorder.watch_silence = False
        # Ignore the first moments if Athena was just talking (the device still plays the tail)
        # or if we play the start chime; otherwise she transcribes herself.
        echo = self.speaker.recently_active()
        self.stop_speaking()
        chime = source != "chat"
        mute = (0.35 if echo else 0.0) + (0.2 if chime else 0.0)
        try:
            self.recorder.start(self.settings.get("voice.mic_device"), mute_s=mute)
        except Exception as exc:
            log.error("mic start failed: %s", exc)
            self.set_state("error", "Microphone unavailable")
            self.toast(f"Microphone unavailable: {exc}", "error")
            return
        self.listening = True
        if chime:
            self.speaker.tone([620, 880])
        self.set_state("listening")
        # Replying soon after Athena answered, or while the chat panel is open, or from the panel's
        # mic = chat follow-up: keep the earlier screenshot as context (she can still look again).
        has_ctx = bool(self.agent.history and self.agent.frame is not None)
        self.followup = (not answering) and has_ctx and (
            source == "chat" or self.chat_visible or
            self.agent.is_followup(float(self.settings.get("general.followup_window_s") or 0)))
        self.press_window = None
        self.shot_task = None
        if source == "chat":
            # Panel mic works like a toggle: click to talk, click again (or pause) to send.
            self.latched = True
            self.recorder.silence_ms = int(self.settings.get("general.silence_stop_ms") or 1800)
            self.recorder.watch_silence = True
        if self.followup:
            self.press_window = self._user_window()
        elif not answering:
            self._drop_highlights(emit=True)   # a new question: old highlights no longer apply
            self.shot_task = asyncio.ensure_future(self.capture())
        max_s = float(self.settings.get("general.max_listen_s") or 90)
        self.listen_timer = asyncio.ensure_future(self._listen_timeout(max_s))

    async def _listen_timeout(self, secs: float) -> None:
        await asyncio.sleep(secs)
        if self.listening:
            self._finish_listening()

    def _on_silence(self) -> None:
        if self.listening and self.latched:
            self._finish_listening()

    def _abort_listening(self) -> None:
        if not self.listening:
            return
        self.listening = False
        self.recorder.stop()
        if self.listen_timer:
            self.listen_timer.cancel()
        if self.shot_task:
            self.shot_task.cancel()
        self.set_state("idle")

    def _finish_listening(self) -> None:
        if not self.listening:
            return
        self.listening = False
        if self.listen_timer:
            self.listen_timer.cancel()
        pcm = self.recorder.stop()
        self.speaker.tone([880, 620])
        if len(pcm) < 16000 * 2 * 0.3 or not self.recorder.heard_speech:
            self.caption("Didn't catch that", "status")
            if self.shot_task:
                self.shot_task.cancel()
            self.set_state("idle")
            return
        asyncio.ensure_future(self._process(pcm))

    async def _process(self, pcm: bytes) -> None:
        self.set_state("transcribing")
        try:
            text = await self.voice.transcribe(pcm)
        except Exception as exc:
            log.warning("transcription failed: %s", exc)
            if await self.voice.find_server() is None:
                self._voice_task = asyncio.ensure_future(self.voice.supervise(
            on_status=self._voice_status, on_problem=lambda m: self.toast(m, "error")))
                self.toast("Starting the voice server. Try again in a moment.", "warn")
            else:
                self.toast(f"Transcription failed: {exc}", "error")
            self.set_state("idle")
            return
        if not text:
            self.caption("Didn't catch that", "status")
            self.set_state("idle")
            return
        self.caption(text, "user")
        if self.confirm_future is not None and not self.confirm_future.done():
            yes, no = bool(YES_RE.search(text)), bool(NO_RE.search(text))
            self.chat_post("user", text)
            self.confirm_future.set_result(yes and not no)
            return
        self.chat_post("user", text, shot=not self.followup)
        if self.followup:
            await self.run_agent(text, None, followup=True, window=self.press_window)
            return
        frame = None
        if self.shot_task:
            try:
                frame = await self.shot_task
            except (asyncio.CancelledError, Exception) as exc:
                log.warning("hotkey screenshot failed: %s", exc)
        await self.run_agent(text, frame)

    async def run_agent(self, text: str, frame: Frame | None, followup: bool = False,
                        window: dict | None = None) -> None:
        if frame is None and not followup:
            try:
                frame = await self.capture()
            except Exception:
                log.exception("capture failed")
        if followup:
            log.info("follow-up turn: reusing the earlier screenshot")
        self.agent_task = asyncio.ensure_future(self._agent_wrapper(text, frame, window))

    async def _agent_wrapper(self, text: str, frame: Frame | None, window: dict | None = None) -> None:
        try:
            await self.agent.run(text, frame, window)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("agent crashed")
            self.set_state("error", str(exc))
            self.toast(f"Stopped: {exc}", "error")
            await asyncio.sleep(1.5)
        finally:
            self.hooks.disarm_takeover()
            if not self.listening and self.state not in ("user", "error", "paused"):
                self.set_state("idle")

    # ================================================================ interrupt
    def stop_speaking(self) -> None:
        self.speaker.clear()
        for t in list(self.speech_tasks):
            t.cancel()

    def interrupt(self) -> None:
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()
        self.stop_speaking()
        if self.click_future and not self.click_future.done():
            self.click_future.set_result(None)
        if self.confirm_future and not self.confirm_future.done():
            self.confirm_future.set_result(False)
        self.hooks.disarm_takeover()
        # Interrupted mid-tutorial: highlights that were waiting for a click fade on the normal timer.
        self.bus.emit("highlight.settle", {"seconds": float(self.settings.get("appearance.highlight_seconds") or 20)})

    def _on_takeover(self, source: str) -> None:
        if source == "esc" and self.listening:
            self._abort_listening()
            return
        if self.calib_future is not None:
            return
        if source == "esc":
            self.bus.emit("highlight.clear", {})
        busy = (self.agent_task and not self.agent_task.done()) or self.speaker.pending_seconds() > 0
        if not busy:
            return
        driving = self.agent.driving
        self.interrupt()
        if driving:
            self.set_state("user", "You're in control")
            self.caption("You have control", "status")
            self.loop.call_later(1.6, lambda: self.state == "user" and self.set_state("idle"))
        else:
            self.set_state("idle")

    def _on_user_click(self, x: int, y: int, btn: str) -> None:
        if self.click_future and not self.click_future.done():
            self.click_future.set_result((x, y, btn))

    # ================================================================ Env impl
    async def capture(self) -> Frame:
        s = self.settings
        f = await asyncio.to_thread(screen.capture, int(s.get("model.image_max_width") or 1344),
                                    int(s.get("model.image_round_to") or 0), s.get("general.screenshot_scope"))
        fg = await asyncio.to_thread(win32.foreground_window)
        f.window = fg.as_dict() if fg else None
        if s.get("model.use_accessibility"):
            els = await asyncio.to_thread(uia.snapshot, f.monitor, int(s.get("model.max_elements") or 120))
            f.elements = els
            if els and s.get("model.draw_marks"):
                f.image = await asyncio.to_thread(screen.draw_marks, f, els)
        if s.get("general.save_screenshots"):
            self._shot_n += 1
            name = datetime.now().strftime("%H%M%S") + f"-{self._shot_n:03d}"
            folder = SESSIONS_DIR / datetime.now().strftime("%Y-%m-%d")
            asyncio.ensure_future(asyncio.to_thread(screen.save, f, folder, name, int(s.get("model.jpeg_quality") or 78)))
        return f

    async def say(self, text: str, wait: bool = False) -> None:
        text = clean_for_speech(text)
        if not text:
            return
        if not wait:
            self.caption(text, "athena")
        if self.settings.get("voice.speak_replies") and self.voice.status != "ready":
            await self.voice.quick_check()   # it may have come up since the last check
        if not self.settings.get("voice.speak_replies") or self.voice.status != "ready":
            if wait:
                await asyncio.sleep(min(6.0, 0.9 + 0.22 * len(text.split())))
            return
        task = asyncio.ensure_future(self._speak(text, self.speaker.generation))
        self.speech_tasks.add(task)
        task.add_done_callback(self.speech_tasks.discard)
        if wait:
            await task

    async def _speak(self, text: str, gen: int) -> None:
        async with self.tts_lock:
            if self.speaker.generation != gen:
                return
            prev = self.state
            if prev in ("idle", "thinking", "transcribing"):
                self.set_state("speaking")
            try:
                for sentence in split_sentences(text):
                    async for chunk in self.voice.stream_tts(sentence):
                        if self.speaker.generation != gen:
                            return
                        self.speaker.feed(chunk)
                while self.speaker.pending_seconds() > 0.03 and self.speaker.generation == gen:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("TTS failed: %s", exc)
            finally:
                if self.state == "speaking":
                    self.set_state("thinking" if self.agent_task and not self.agent_task.done() else "idle")

    # ------------------------------------------------------------------ chat panel
    def chat_post(self, role: str, text: str = "", md: str | None = None, open_panel: bool = False,
                  **extra) -> dict:
        # Each user message starts a turn; Athena's answer, notes and approvals belong to it.
        # The popup shows only the current turn, the app shows the whole log.
        if role in ("user", "divider"):
            self.turn_id = uuid.uuid4().hex[:10]
        entry = {"id": uuid.uuid4().hex[:10], "turn": self.turn_id, "role": role, "text": text, "md": md,
                 "ts": time.time(), **extra}
        self.chat_log.append(entry)
        self.chat_log = self.chat_log[-CHAT_KEEP:]
        self.bus.emit("chat.message", {"entry": entry, "open": open_panel})
        self._schedule_chat_save()
        return entry

    def _schedule_chat_save(self) -> None:
        if self._chat_save_handle:
            self._chat_save_handle.cancel()
        snapshot = list(self.chat_log)
        self._chat_save_handle = self.loop.call_later(
            1.0, lambda: asyncio.ensure_future(asyncio.to_thread(_save_chat, snapshot)))

    def chat(self, role: str, text: str) -> None:
        """Env hook: plain spoken replies and narration go into the thread without opening it."""
        if text:
            self.chat_post(role, text)

    async def show_popup(self, title: str, markdown: str, kind: str = "info", spoken: str = "") -> None:
        # The detailed answer opens the chat panel by the cursor; the short version is spoken.
        self.chat_post("athena", spoken, md=markdown, open_panel=True, title=title, kind=kind)

    def _user_window(self) -> dict | None:
        """The app the user is working in (never Athena's own chat panel)."""
        fg = win32.foreground_window()
        if fg and not _is_athena_window(fg):
            return fg.as_dict()
        f = self.agent.frame
        return f.window if f is not None else None

    async def highlight(self, items: list[dict], duration: float, keep: bool, sticky: bool = False) -> None:
        default = float(self.settings.get("appearance.highlight_seconds") or 0)
        if not sticky and default > 0:
            # Model-chosen durations are honoured but never longer than the user's setting.
            duration = default if duration <= 0 else min(duration, default)
        self.bus.emit("highlight.set", {"items": items, "duration": duration, "keep": keep})
        # Highlights describe one window's layout: drop them when that window changes.
        anchor = await asyncio.to_thread(self._user_window_info)
        self._hl_anchor = anchor
        self._hl_until = time.monotonic() + duration if duration > 0 else float("inf")
        self.hooks.watch_wheel = True
        if self._hl_task is None or self._hl_task.done():
            self._hl_task = asyncio.ensure_future(self._watch_highlights())

    async def clear_highlights(self) -> None:
        self._drop_highlights(emit=True)

    def _drop_highlights(self, emit: bool = True) -> None:
        self._hl_anchor = None
        self._hl_until = 0.0
        self.hooks.watch_wheel = False
        if emit:
            self.bus.emit("highlight.clear", {})

    def _user_window_info(self):
        fg = win32.foreground_window()
        if fg and not _is_athena_window(fg):
            return fg
        f = self.agent.frame
        w = (f.window if f is not None else None) or {}
        if w.get("hwnd"):
            return win32.WindowInfo(int(w["hwnd"]), w.get("title", ""), w.get("process", ""), tuple(w.get("rect") or (0, 0, 0, 0)))
        return fg

    async def _watch_highlights(self) -> None:
        while self._hl_until and time.monotonic() < self._hl_until:
            await asyncio.sleep(0.3)
            anchor = self._hl_anchor
            if anchor is None:
                return
            fg = await asyncio.to_thread(win32.foreground_window)
            if fg is None or _is_athena_window(fg):
                continue                      # Athena's own chat/app window doesn't count
            if fg.hwnd != anchor.hwnd or tuple(fg.rect) != tuple(anchor.rect):
                log.info("highlights cleared: %s", "window changed" if fg.hwnd != anchor.hwnd else "window moved")
                self._drop_highlights(emit=True)
                return
        self._drop_highlights(emit=False)

    def _on_user_wheel(self) -> None:
        if self._hl_anchor is not None:
            self._drop_highlights(emit=True)   # the page scrolled under the highlights

    async def confirm(self, action: str, details: str | None) -> bool:
        self.pause_control()
        fut = self.loop.create_future()
        self.confirm_future = fut
        entry = self.chat_post("confirm", action, md=details or None, open_panel=True, status="pending")
        pid = entry["id"]
        self.chat_confirm_id = pid
        prev = self.state
        self.set_state("confirm", action)
        await self.say(f"Just checking before I do this: {action}. Should I go ahead?", wait=False)
        try:
            ok = await asyncio.wait_for(fut, timeout=180)
        except asyncio.TimeoutError:
            ok = False
        finally:
            self.confirm_future = None
            self.chat_confirm_id = None
            entry["status"] = "approved" if fut.done() and fut.result() else "denied"
            self.bus.emit("chat.update", {"id": pid, "status": entry["status"]})
        self.caption("Approved" if ok else "Cancelled", "status")
        self.set_state(prev if prev not in ("confirm",) else "thinking")
        if self.agent.driving and ok:
            self.begin_control()
        return ok

    async def wait_click(self, timeout: float) -> tuple[int, int, str] | None:
        fut = self.loop.create_future()
        self.click_future = fut
        self.hooks.watch_clicks = True
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.hooks.watch_clicks = False
            self.click_future = None

    def begin_control(self) -> None:
        # If the user typed in Athena's chat panel, give focus back to the app she's working on
        # so clicks and keystrokes don't land in the panel.
        fg = win32.foreground_window()
        if fg and _is_athena_window(fg):
            target = (self.agent.frame.window or {}).get("hwnd") if self.agent.frame is not None else None
            if target:
                win32.focus_window(int(target))
        c = self.settings.get("control")
        self.hooks.arm_takeover(int(c["takeover_threshold_px"]), bool(c["takeover_mouse"]), bool(c["takeover_esc"]))
        if self.state != "acting":
            self.set_state("acting")

    def pause_control(self) -> None:
        self.hooks.disarm_takeover()

    def input_done(self) -> None:
        self.hooks.reset_motion()

    # ============================================================== calibration
    async def calibrate(self) -> dict:
        if not self.llm.model_id():
            raise LLMError("Select a model first.")
        self.interrupt()
        cx, cy = win32.cursor_pos()
        mon = screen.monitor_for_point(cx, cy)
        ml, mt, mw, mh = mon
        targets = [{"label": LABELS[i], "x": int(ml + fx * mw), "y": int(mt + fy * mh)}
                   for i, (fx, fy) in enumerate(TARGET_FRACTIONS)]
        self.calib_future = self.loop.create_future()
        self.bus.emit("calibration.show", {"monitor": list(mon), "targets": targets})
        try:
            await asyncio.wait_for(self.calib_future, 10)
            await asyncio.sleep(0.5)
            s = self.settings
            frame = await asyncio.to_thread(screen.capture, int(s.get("model.image_max_width") or 1344),
                                            int(s.get("model.image_round_to") or 0), "cursor_monitor", mon)
            self.bus.emit("calibration.status", {"text": "Asking the model to find the targets…"})
            w, h = frame.size
            prompt = (f"This screenshot is {w}x{h} pixels. It shows {len(targets)} large circular targets, each a "
                      f"ring with a bold letter ({', '.join(t['label'] for t in targets)}) in the middle. "
                      "Report the position of the centre of every target, using the same coordinate format you "
                      "use when pointing at or clicking things on screen. Respond with JSON only, like "
                      '{"A": [x, y], "B": [x, y], ...}')
            msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                 {"type": "image_url", "image_url": {"url": frame.encode(90)}}]}]
            res = await self.llm.chat(msgs, mode="json", max_tokens=4096)
            pts = parse_points(extract_json(res.text) if res.text else None, LABELS[:len(targets)])
            if len(pts) < 3:
                raise LLMError("The model couldn't locate the targets. Is it a vision model? "
                               f"Reply was: {res.text[:200]!r}")
            labs = [t["label"] for t in targets if t["label"] in pts]
            truth = [frame.to_image(t["x"], t["y"]) for t in targets if t["label"] in pts]
            pred = [pts[l] for l in labs]
            result = self.mapper.fit(self.llm.model_id(), pred, truth, w, h)
            mapped = []
            for l, (px, py) in zip(labs, pred):
                ix, iy = self.mapper.map(self.llm.model_id(), px, py, w, h, "auto")
                sx, sy = frame.to_screen(ix, iy)
                mapped.append({"label": l, "x": sx, "y": sy})
            self.bus.emit("calibration.result", {"ok": True, "result": result, "mapped": mapped})
            # Visual pointer test: glide the real cursor over each predicted point.
            for m in mapped:
                await asyncio.to_thread(win32.mouse_move, m["x"], m["y"], "natural")
                await asyncio.sleep(0.35)
            await asyncio.sleep(1.2)
            return {"ok": True, "model": self.llm.model_id(), **result}
        except Exception as exc:
            self.bus.emit("calibration.result", {"ok": False, "error": str(exc)})
            await asyncio.sleep(2.5)
            raise
        finally:
            self.calib_future = None
            self.bus.emit("calibration.hide", {})

    # ===================================================================== RPC
    def _register_rpc(self) -> None:
        b = self.bus
        S = self.settings

        @b.method("settings.get")
        async def _(p):
            return {"settings": S.public(), "presets": PROVIDER_PRESETS, "data_dir": str(DATA_DIR)}

        @b.method("settings.set")
        async def _(p):
            patch = p.get("patch") or {}
            hk = patch.get("general", {}).get("hotkey")
            if hk:
                from .hooks import combo_vk_sets
                combo_vk_sets(hk)  # raises on unknown keys
            S.update(patch)
            return S.public()

        @b.method("state.get")
        async def _(p):
            return self._state_payload()

        @b.method("models.list")
        async def _(p):
            pid = p.get("provider") or S.get("model.active_provider")
            prov = S.get(f"model.providers.{pid}") or {}
            prov = {**prov, "id": pid}
            if p.get("base_url"):
                prov["base_url"] = p["base_url"]
            if p.get("api_key") and not str(p["api_key"]).startswith("•"):
                prov["api_key"] = p["api_key"]
            return await self.llm.list_models(prov)

        @b.method("llm.test")
        async def _(p):
            t0 = time.monotonic()
            res = await self.llm.chat([{"role": "user", "content": "Reply with exactly: Athena online."}],
                                      mode="json", max_tokens=1024)
            return {"text": res.text[:200], "seconds": round(time.monotonic() - t0, 2),
                    "tool_mode": self.llm.effective_mode()}

        @b.method("voice.status")
        async def _(p):
            await self.voice.find_server()
            return {"status": self.voice.status, "url": self.voice.base_url}

        @b.method("voice.log")
        async def _(p):
            return self.voice.log_tail(int(p.get("lines") or 30))

        @b.method("voice.voices")
        async def _(p):
            return await self.voice.voices()

        @b.method("voice.start")
        async def _(p):
            self._voice_task = asyncio.ensure_future(self.voice.supervise(
            on_status=self._voice_status, on_problem=lambda m: self.toast(m, "error")))
            return True

        @b.method("voice.test")
        async def _(p):
            await self.say(p.get("text") or "Hi, I'm Athena. This is how I sound.", wait=False)
            return True

        @b.method("audio.devices")
        async def _(p):
            return list_devices()

        @b.method("agent.reset")
        async def _(p):
            self.interrupt()
            self.agent.reset()
            if self.chat_log and self.chat_log[-1].get("role") != "divider":
                self.chat_post("divider", "New conversation")
            self.caption("New conversation", "status")
            return True

        @b.method("agent.ask")
        async def _(p):
            text = (p.get("text") or "").strip()
            if text:
                self.interrupt()
                # Typed in the chat panel = a follow-up to what Athena just showed (same screenshot).
                fu = bool(self.agent.history and self.agent.frame is not None)
                self.chat_post("user", text, shot=not fu)
                await self.run_agent(text, None, followup=fu, window=self._user_window())
            return True

        @b.method("agent.stop")
        async def _(p):
            self._on_takeover("esc")
            return True

        @b.method("memory.list")
        async def _(p):
            return self.memory.facts()

        @b.method("memory.delete")
        async def _(p):
            self.memory.delete(int(p.get("index", -1)))
            return self.memory.facts()

        @b.method("memory.clear")
        async def _(p):
            self.memory.clear()
            return []

        @b.method("calibration.run")
        async def _(p):
            return await self.calibrate()

        @b.method("calibration.get")
        async def _(p):
            return {"model": self.llm.model_id(), "data": self.mapper.get(self.llm.model_id())}

        @b.method("calibration.clear")
        async def _(p):
            self.mapper.clear(self.llm.model_id())
            return True

        @b.method("app.open_folder")
        async def _(p):
            target = {"sessions": SESSIONS_DIR, "logs": LOGS_DIR, "data": DATA_DIR}.get(p.get("name"), DATA_DIR)
            if hasattr(os, "startfile"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            return str(target)

        def on_popup_action(d):
            act = d.get("action")
            if self.confirm_future and not self.confirm_future.done() and act in ("approve", "deny", "close"):
                self.confirm_future.set_result(act == "approve")

        def on_chat_confirm(d):
            if self.confirm_future and not self.confirm_future.done() and \
                    (not d.get("id") or d.get("id") == self.chat_confirm_id):
                self.confirm_future.set_result(bool(d.get("approve")))

        def on_chat_visible(d):
            self.chat_visible = bool(d.get("visible"))

        @b.method("chat.history")
        async def _(p):
            if p.get("current"):
                # Just the latest turn, for the popup.
                last = self.chat_log[-1]["turn"] if self.chat_log else None
                return [e for e in self.chat_log if e.get("turn") == last and e.get("role") != "divider"]
            return self.chat_log

        @b.method("chat.clear")
        async def _(p):
            self.chat_log = []
            self.turn_id = ""
            self._schedule_chat_save()
            self.bus.emit("chat.cleared", {})
            return True

        @b.method("chat.mic")
        async def _(p):
            act = p.get("action")
            if act == "start" and not self.listening:
                if self.settings.get("general.paused"):
                    raise ValueError("Athena is paused")
                self.press_t = time.monotonic()
                self._start_listening("chat")
            elif act == "stop" and self.listening:
                self._finish_listening()
            elif act == "stop_speaking":
                # Mic pressed while Athena talks: just silence her; the next press starts listening.
                self.stop_speaking()
                if self.state == "speaking":
                    self.set_state("idle")
            elif act == "cancel":
                self._abort_listening()
            return {"listening": self.listening}

        b.on("chat.confirm", on_chat_confirm)
        b.on("chat.visible", on_chat_visible)

        def on_calib_ready(d):
            if self.calib_future and not self.calib_future.done():
                self.calib_future.set_result(True)

        b.on("popup.action", on_popup_action)
        b.on("calibration.ready", on_calib_ready)
        b.on("hotkey.simulate", lambda d: self._on_hotkey(d.get("ev", "down")))


def _is_athena_window(w) -> bool:
    return (w.title or "").startswith("Athena") and (w.process or "").lower() in ("electron.exe", "athena.exe")


CHAT_KEEP = 2000
CHAT_PATH = DATA_DIR / "conversation.json"


def _load_chat() -> list[dict]:
    try:
        data = json.loads(CHAT_PATH.read_text(encoding="utf-8"))
        return data[-CHAT_KEEP:] if isinstance(data, list) else []
    except Exception:
        return []


def _save_chat(entries: list[dict]) -> None:
    try:
        tmp = CHAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CHAT_PATH)
    except Exception:
        log.exception("could not save conversation")
