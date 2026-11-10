"""The Athena agent: look → think → act → verify loop with tools.

The agent is UI-agnostic. Everything that touches the shell (voice, popups,
highlights, confirmations, cursor state) goes through the `Env` interface the
controller implements, which keeps this file testable with a fake env and a
scripted model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import screen, win32
from .calibration import CoordMapper
from .llm import LLMClient, LLMError, ToolCall, chat_with_fallback, strip_think
from .memory import Memory
from .prompts import system_prompt
from .screen import Frame
from .tools import IMAGE_TOOLS, INPUT_TOOLS, SCREEN_CHANGING, TERMINAL
from .uia import describe as describe_elements
from .voice import clean_for_speech

log = logging.getLogger("athena.agent")


class Env(Protocol):
    settings: Any

    async def capture(self) -> Frame: ...
    async def say(self, text: str, wait: bool = False) -> None: ...
    async def show_popup(self, title: str, markdown: str, kind: str = "info", spoken: str = "") -> None: ...
    async def highlight(self, items: list[dict], duration: float, keep: bool, sticky: bool = False) -> None: ...
    def chat(self, role: str, text: str) -> None: ...
    async def clear_highlights(self) -> None: ...
    async def confirm(self, action: str, details: str | None) -> bool: ...
    async def wait_click(self, timeout: float) -> tuple[int, int, str] | None: ...
    def set_state(self, state: str, detail: str = "") -> None: ...
    def begin_control(self) -> None: ...
    def pause_control(self) -> None: ...
    def input_done(self) -> None: ...
    def feedback(self, kind: str, x: int, y: int) -> None: ...
    def caption(self, text: str, kind: str = "status") -> None: ...


@dataclass
class Item:
    kind: str                       # user | assistant | results
    text: str = ""
    frame: Frame | None = None
    extra_frames: list[Frame] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    results: list[tuple[ToolCall, str]] = field(default_factory=list)
    thought: str = ""
    window: dict | None = None      # active window at the time (follow-ups without a screenshot)


class StopRun(Exception):
    pass


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _wc(s: str) -> int:
    return len(re.findall(r"\S+", s or ""))


def _first_words(s: str, n: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", s.strip())
    out = ""
    for sen in sentences:
        if _wc(out + " " + sen) > n:
            break
        out = (out + " " + sen).strip()
    return out or " ".join(s.split()[:n]) + "…"


class Agent:
    def __init__(self, settings, llm: LLMClient, env: Env, mapper: CoordMapper, memory: Memory):
        self.settings = settings
        self.llm = llm
        self.env = env
        self.mapper = mapper
        self.memory = memory
        self.history: list[Item] = []
        self.frame: Frame | None = None
        self.last_activity = 0.0
        self.task_text = ""
        self._approvals = 0
        self._popup_retries = 0
        self.driving = False

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        self.history.clear()
        self.frame = None
        self.task_text = ""

    def maybe_expire(self) -> None:
        mins = self.settings.get("general.conversation_idle_reset_min") or 0
        if mins and self.history and time.time() - self.last_activity > mins * 60:
            log.info("conversation idle for %s min: starting fresh", mins)
            self.reset()

    # ---------------------------------------------------------------- render
    def _context_text(self, frame: Frame | None) -> str:
        if frame is None:
            return "(no screenshot)"
        w, h = frame.size
        lines = [f"Screenshot: {w}x{h} px."]
        if frame.window:
            lines.append(f"Active window: \"{frame.window.get('title', '')}\" ({frame.window.get('process', '')}).")
        if frame.cursor and frame.contains_screen(*frame.cursor):
            cx, cy = frame.to_image(*frame.cursor)
            lines.append(f"Mouse pointer at ({cx},{cy}).")
        if frame.elements:
            lines.append("Interactive elements [id] role \"name\" @(center x,y):")
            lines.append(describe_elements(frame.elements, frame))
        return "\n".join(lines)

    @staticmethod
    def _followup_text(window: dict | None) -> str:
        w = f' Active window right now: "{window.get("title", "")}" ({window.get("process", "")}).' if window else ""
        return ("(Follow-up in the same conversation — no new screenshot was taken. The user is most likely "
                "still talking about the earlier screenshot and your previous answer. If they refer to something "
                f"new on screen, or you need to see the current state, call take_screenshot.{w})")

    def render(self, mode: str, protocol: bool = True) -> list[dict]:
        s = self.settings
        vision = s.get("model.supports_vision")
        keep = max(1, int(s.get("model.keep_images") or 3))
        quality = int(s.get("model.jpeg_quality") or 82)
        model_id = self.llm.model_id()
        coord_mode = s.get("model.coordinate_mode")
        cal = self.mapper.get(model_id) if coord_mode == "auto" else None
        conv = cal["convention"] if cal else (coord_mode if coord_mode != "auto" else None)
        coord_hint = {"norm1000": " Coordinates may be given on a 0-1000 grid over the image.",
                      "norm1": " Coordinates may be given as 0-1 fractions of the image."}.get(conv or "", "")

        sys_msg = system_prompt(
            name=s.get("general.assistant_name") or "Athena", user_name=s.get("general.user_name") or "",
            safety_mode=s.get("control.safety_mode"), memory=self.memory.facts(), json_mode=(mode == "json" and protocol),
            control_enabled=bool(s.get("control.allow_control")), max_spoken_words=s.get("voice.max_spoken_words"),
            extra=s.get("model.extra_instructions") or "", coord_hint=coord_hint)

        # Which frames still get pixels (the most recent `keep`).
        all_frames: list[int] = []
        for it in self.history:
            for f in ([it.frame] if it.frame else []) + it.extra_frames:
                all_frames.append(id(f))
        allowed = set(all_frames[-keep:]) if vision else set()

        def img_parts(frames: list[Frame]) -> list[dict]:
            parts = []
            for f in frames:
                if id(f) in allowed:
                    parts.append({"type": "image_url", "image_url": {"url": f.encode(quality)}})
                else:
                    parts.append({"type": "text", "text": "[older screenshot omitted]"})
            return parts

        msgs: list[dict] = [{"role": "system", "content": sys_msg}]
        for it in self.history:
            if it.kind == "user":
                ctx = self._context_text(it.frame) if it.frame is not None else self._followup_text(it.window)
                content = [{"type": "text", "text": f"{ctx}\n\nUser said: {it.text}"}]
                content += img_parts([it.frame] if it.frame else [])
                msgs.append({"role": "user", "content": content})
            elif it.kind == "assistant":
                if mode == "native" and it.calls:
                    msgs.append({"role": "assistant", "content": it.text or "",
                                 "tool_calls": [{"id": c.id, "type": "function",
                                                 "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                                for c in it.calls]})
                elif it.calls:
                    msgs.append({"role": "assistant", "content": json.dumps(
                        {"thought": it.thought, "actions": [{"tool": c.name, "args": c.args} for c in it.calls]})})
                else:
                    msgs.append({"role": "assistant", "content": it.text or "(no response)"})
            elif it.kind == "results":
                frames = ([it.frame] if it.frame else []) + it.extra_frames
                if mode == "native":
                    for call, res in it.results:
                        msgs.append({"role": "tool", "tool_call_id": call.id, "content": res})
                    if frames or it.text:
                        txt = it.text or "Here is the screen now."
                        if it.frame:
                            txt += "\n" + self._context_text(it.frame)
                        msgs.append({"role": "user", "content": [{"type": "text", "text": txt}] + img_parts(frames)})
                else:
                    lines = [f"- {c.name}: {r}" for c, r in it.results]
                    txt = "Results:\n" + "\n".join(lines)
                    if it.text:
                        txt += "\n" + it.text
                    if it.frame:
                        txt += "\nCurrent screen:\n" + self._context_text(it.frame)
                    msgs.append({"role": "user", "content": [{"type": "text", "text": txt}] + img_parts(frames)})
        return _merge_consecutive(msgs)

    # ------------------------------------------------------------- coordinates
    def _frame(self) -> Frame:
        if self.frame is None:
            raise ValueError("no screenshot yet — call take_screenshot first")
        return self.frame

    def _point(self, args: dict, required: bool = True) -> tuple[int, int] | None:
        """Resolve element id or x,y (any coordinate convention) to physical screen px."""
        f = self._frame()
        el_id = args.get("element") if args.get("element") is not None else args.get("element_id")
        if el_id is not None:
            try:
                el_id = int(el_id)
            except (TypeError, ValueError):
                el_id = None
            for el in f.elements:
                if el.id == el_id:
                    return el.center
            if el_id is not None and args.get("x") is None:
                raise ValueError(f"element {el_id} is not in the latest screenshot's element list")
        x, y = _num(args.get("x")), _num(args.get("y"))
        if x is None or y is None:
            for k in ("coordinate", "point", "position", "coordinates"):
                v = args.get(k)
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    x, y = _num(v[0]), _num(v[1])
                    break
        if x is None or y is None:
            if required:
                raise ValueError("give either element or x and y")
            return None
        w, h = f.size
        ix, iy = self.mapper.map(self.llm.model_id(), x, y, w, h, self.settings.get("model.coordinate_mode"))
        return f.to_screen(ix, iy)

    def _rect_from(self, item: dict) -> tuple[list[int] | None, list[int] | None]:
        """Highlight geometry -> (rect, point) in physical px."""
        f = self._frame()
        if item.get("element") is not None:
            for el in f.elements:
                if el.id == int(item["element"]):
                    return list(el.rect), list(el.center)
        box = item.get("box") or item.get("bbox")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            p1 = self._point({"x": box[0], "y": box[1]})
            p2 = self._point({"x": box[2], "y": box[3]})
            rect = [min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1])]
            return rect, [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2]
        p = self._point(item)
        return None, list(p)

    # ---------------------------------------------------------------- safety
    def _risk(self, call: ToolCall) -> str | None:
        """Returns a human description if this call needs confirmation, else None."""
        mode = self.settings.get("control.safety_mode")
        if mode == "autonomous" or call.name not in INPUT_TOOLS:
            return None
        desc = str(call.args.get("description") or "")
        if call.name == "click" and call.args.get("element") is not None and self.frame:
            for el in self.frame.elements:
                if el.id == _num(call.args.get("element")):
                    desc = f"{desc} ({el.role} '{el.name}')"
        if call.name == "type_text":
            desc = f"type: {str(call.args.get('text'))[:80]}"
        if call.name == "press_keys":
            desc = f"press {call.args.get('keys')}"
        if mode == "confirm_all":
            return None if call.name in ("move_mouse", "scroll") else (desc or call.name)
        keywords = [k.lower() for k in self.settings.get("control.risky_keywords") or []]
        hits = [k for k in keywords if re.search(rf"\b{re.escape(k)}\b", desc.lower())]
        flagged = bool(call.args.get("risky")) or (call.name == "click" and hits)
        if not flagged:
            return None
        said = self.task_text.lower()
        if hits and all(re.search(rf"\b{re.escape(k)}\b", said) for k in hits):
            return None  # the user explicitly asked for this
        return desc or call.name

    # ----------------------------------------------------------------- tools
    async def _exec(self, call: ToolCall, out: dict) -> str:
        a = call.args or {}
        n = call.name
        s = self.settings
        speed = s.get("control.mouse_speed")

        if n in INPUT_TOOLS and not s.get("control.allow_control"):
            return "error: mouse/keyboard control is disabled in settings; guide the user with highlight/say instead"

        if n in INPUT_TOOLS:
            risk = self._risk(call)
            if risk:
                if self._approvals > 0:
                    self._approvals -= 1
                else:
                    ok = await self.env.confirm(risk, None)
                    if not ok:
                        return "denied: the user did not approve this action. Do not retry it; ask or reply."
            self.driving = True
            self.env.begin_control()

        if n == "take_screenshot":
            self.frame = await self.env.capture()
            out["frame"] = self.frame
            return "new screenshot attached"
        if n == "zoom":
            f = self._frame()
            p1 = self._point({"x": a.get("x1"), "y": a.get("y1")})
            p2 = self._point({"x": a.get("x2"), "y": a.get("y2")})
            i1, i2 = f.to_image(*p1), f.to_image(*p2)
            z = await asyncio.to_thread(screen.zoom, f, (*i1, *i2))
            out.setdefault("extra", []).append(z)
            return "zoomed view attached (coordinates still refer to the full screenshot)"
        if n == "read_clipboard":
            txt = await asyncio.to_thread(win32.get_clipboard)
            return f"clipboard ({len(txt)} chars):\n{txt[:12000]}" if txt else "clipboard is empty"
        if n == "list_windows":
            wins = await asyncio.to_thread(win32.list_windows)
            return "\n".join(f"- \"{w.title}\" ({w.process})" for w in wins[:40]) or "no windows"

        if n == "click":
            x, y = self._point(a)
            btn = a.get("button") or "left"
            if btn not in ("left", "right", "middle"):
                btn = "left"
            await asyncio.to_thread(win32.mouse_move, x, y, speed)
            self.env.feedback("click", x, y)
            await asyncio.to_thread(win32.click, btn, 2 if a.get("double") else 1)
            self.env.input_done()
            return f"clicked {a.get('description', '')} ".strip()
        if n == "move_mouse":
            x, y = self._point(a)
            await asyncio.to_thread(win32.mouse_move, x, y, speed)
            self.env.input_done()
            return "moved"
        if n == "drag":
            p1 = self._point({"x": a.get("from_x"), "y": a.get("from_y")})
            p2 = self._point({"x": a.get("to_x"), "y": a.get("to_y")})
            await asyncio.to_thread(win32.mouse_move, *p1, speed)
            await asyncio.to_thread(win32.mouse_button, "left", True)
            await asyncio.sleep(0.08)
            await asyncio.to_thread(win32.mouse_move, *p2, "slow")
            await asyncio.sleep(0.05)
            await asyncio.to_thread(win32.mouse_button, "left", False)
            self.env.input_done()
            return "dragged"
        if n == "scroll":
            p = self._point(a, required=False)
            if p is None:
                f = self._frame()
                cur = win32.cursor_pos()
                p = cur if f.contains_screen(*cur) else f.to_screen(f.size[0] / 2, f.size[1] / 2)
            await asyncio.to_thread(win32.mouse_move, *p, speed)
            amount = max(1, min(15, int(_num(a.get("amount")) or 3)))
            d = (a.get("direction") or "down").lower()
            sign = 1 if d in ("up", "right") else -1
            await asyncio.to_thread(win32.scroll, sign * amount, d in ("left", "right"))
            self.env.input_done()
            return f"scrolled {d} {amount}"
        if n == "type_text":
            text = str(a.get("text") or "")
            if not text:
                return "error: empty text"
            if "\n" in text or len(text) > 80:
                old = await asyncio.to_thread(win32.get_clipboard)
                await asyncio.to_thread(win32.set_clipboard, text)
                await asyncio.sleep(0.05)
                await asyncio.to_thread(win32.press_combo, "ctrl+v")
                await asyncio.sleep(0.35)
                await asyncio.to_thread(win32.set_clipboard, old)
                method = "pasted"
            else:
                await asyncio.to_thread(win32.type_unicode, text)
                method = "typed"
            self.env.input_done()
            return f"{method} {len(text)} characters"
        if n == "press_keys":
            combos = str(a.get("keys") or "").split()
            if not combos:
                return "error: no keys"
            for c in combos:
                await asyncio.to_thread(win32.press_combo, c)
                await asyncio.sleep(0.06)
            self.env.input_done()
            return f"pressed {' '.join(combos)}"
        if n == "focus_window":
            title = str(a.get("title") or "").lower()
            wins = await asyncio.to_thread(win32.list_windows)
            match = next((w for w in wins if title and title in w.title.lower()), None)
            if not match:
                return "error: no window with that title; use list_windows"
            await asyncio.to_thread(win32.focus_window, match.hwnd)
            return f"focused \"{match.title}\""
        if n == "open_url":
            url = str(a.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                url = "https://" + url
            await asyncio.to_thread(webbrowser.open, url)
            return f"opened {url}"
        if n == "wait":
            secs = max(0.0, min(30.0, _num(a.get("seconds")) or 1.0))
            await asyncio.sleep(secs)
            return f"waited {secs:g}s"

        if n == "highlight":
            items = []
            for it in a.get("items") or []:
                if not isinstance(it, dict):
                    continue
                try:
                    rect, pt = self._rect_from(it)
                except ValueError as exc:
                    return f"error: {exc}"
                items.append({"shape": it.get("shape") or ("box" if rect else "circle"),
                              "rect": rect, "point": pt, "label": str(it.get("label") or "")[:80]})
            if not items:
                return "error: no valid highlight items"
            wait_s = float(_num(a.get("timeout_s")) or 120)
            # A highlight that waits for the user's click lives exactly as long as the wait, never forever.
            dur = wait_s + 2 if a.get("wait_for_click") else float(_num(a.get("duration_s")) or 0)
            await self.env.highlight(items, dur, bool(a.get("keep_previous")), sticky=bool(a.get("wait_for_click")))
            if a.get("wait_for_click"):
                self.env.pause_control()
                self.env.set_state("waiting", "Your turn")
                res = await self.env.wait_click(float(_num(a.get("timeout_s")) or 120))
                await self.env.clear_highlights()   # step done (or timed out): don't leave it on screen
                if self.driving:
                    self.env.begin_control()
                if res is None:
                    return "highlighted; the user did not click before the timeout"
                ux, uy, btn = res
                hit = any(it["rect"] and it["rect"][0] - 12 <= ux <= it["rect"][2] + 12 and
                          it["rect"][1] - 12 <= uy <= it["rect"][3] + 12 or
                          (it["point"] and abs(it["point"][0] - ux) < 40 and abs(it["point"][1] - uy) < 40)
                          for it in items)
                await asyncio.sleep(0.6)
                self.frame = await self.env.capture()
                out["frame"] = self.frame
                ix, iy = self.frame.to_image(ux, uy)
                return (f"user {btn}-clicked at ({ix},{iy}) — {'on the highlighted target' if hit else 'NOT on the highlighted target'}. "
                        "New screenshot attached.")
            return f"highlighted {len(items)} item(s)"
        if n == "clear_highlights":
            await self.env.clear_highlights()
            return "cleared"

        if n == "say":
            line = strip_think(str(a.get("text") or ""))
            self.env.chat("note", clean_for_speech(line))
            await self.env.say(line, wait=False)
            return "said"
        if n == "request_confirmation":
            self.env.pause_control()
            ok = await self.env.confirm(str(a.get("action") or "Proceed?"), a.get("details_markdown"))
            if self.driving:
                self.env.begin_control()
            if ok:
                self._approvals = 1
                return "approved"
            return "denied: do not do it; ask the user what they'd like instead or reply"
        if n == "remember":
            self.memory.add(str(a.get("fact") or ""))
            return "remembered"
        if n == "reply":
            speech = str(a.get("speech") or a.get("text") or a.get("message") or "")
            popup = _popup_arg(a)
            if not popup:
                # Many local models write the details as normal message text and call reply with a lead-in.
                extra = strip_think(out.get("assistant_text") or "")
                if extra and extra.strip() != speech.strip() and _worth_popup(extra):
                    popup = extra
            if popup and not _adds_detail(speech, popup):
                log.info("popup_markdown just repeats the speech; treating it as missing")
                popup = None
            if popup and not _worth_popup(popup):
                # A sentence or two of extra detail: say it instead of opening a popup for it.
                short = clean_for_speech(popup)
                if _wc(short) <= int(self.settings.get("voice.max_spoken_words") or 45):
                    speech = short if _wc(short) >= _wc(_strip_popup_mention(speech)) else _strip_popup_mention(speech)
                popup = None
            if not popup and _mentions_popup(speech):
                # The model promised details but didn't write them: ask it for just the detailed version.
                popup = await self._expand_details(speech)
            if not popup and _mentions_popup(speech):
                speech = _strip_popup_mention(speech)
            await self.deliver(speech, popup, a.get("popup_title") or a.get("title"))
            return "delivered to user"
        if n == "ask_user":
            details = _popup_arg(a)
            await self.deliver(str(a.get("question") or a.get("speech") or ""),
                               details if details and _worth_popup(details) else None, "Question", kind="question")
            return "asked; waiting for the user's answer"
        return f"error: unknown tool {n}"

    async def _expand_details(self, speech: str) -> str | None:
        """One extra, tool-free call that writes the full answer the spoken summary refers to."""
        self.env.set_state("thinking", "Writing the details")
        try:
            msgs = self.render("json", protocol=False)
            msgs.append({"role": "user", "content": (
                "Your spoken reply was: \"" + speech + "\"\n\nNow write the complete, detailed answer that this "
                "summary refers to, for the popup the user will read: every step, specific names, settings, "
                "shortcuts and examples, well formatted in markdown. Do not repeat the summary sentence, do not "
                "mention the popup, and do not call any tools. Output only the markdown.")})
            msgs = _merge_consecutive(msgs)
            res = await self.llm.chat(msgs, mode="text")
            text = strip_think(res.text)
            if text and _adds_detail(speech, text) and _worth_popup(text):
                return text
            log.info("detail expansion added nothing new")
        except Exception as exc:
            log.warning("could not expand details: %s", exc)
        return None

    async def deliver(self, speech: str, popup: str | None, title: str | None, kind: str = "info") -> None:
        popup = strip_think(popup or "") or None
        speech_raw = strip_think(speech)
        max_words = int(self.settings.get("voice.max_spoken_words") or 45)
        spoken = clean_for_speech(speech_raw)
        structured = _wc(spoken) > 25 and bool(re.search(r"```|^\s*[-*\d]+[.)]?\s|\n\n|https?://", speech_raw, re.M))
        if not popup and (_wc(spoken) > max_words or structured):
            popup = speech_raw
            spoken = _first_words(spoken, max(12, max_words // 2)) + " I've put the details in a popup for you."
        if popup and not spoken:
            spoken = "Here you go, it's in the popup."
        if popup:
            # Only a genuinely detailed answer opens the popup by the cursor.
            await self.env.show_popup(title or self.settings.get("general.assistant_name") or "Athena",
                                      popup or "", kind, spoken=spoken)
        else:
            self.env.chat("athena", spoken)
        if spoken:
            self.env.caption(spoken, "athena")
            await self.env.say(spoken, wait=True)

    # ------------------------------------------------------------------- run
    def is_followup(self, window_s: float) -> bool:
        """A reply soon after Athena's last answer continues the chat on the same screenshot."""
        return bool(window_s and self.history and self.frame is not None and
                    time.time() - self.last_activity < window_s)

    async def run(self, user_text: str, frame: Frame | None, window: dict | None = None) -> None:
        self.maybe_expire()
        self.last_activity = time.time()
        self._popup_retries = 0
        if frame is not None:
            self.frame = frame
        self.task_text = (self.task_text + " \n " + user_text) if self.history and \
            self.history[-1].kind == "results" and any(c.name == "ask_user" for c, _ in self.history[-1].results) \
            else user_text
        self.history.append(Item(kind="user", text=user_text, frame=frame, window=window))
        self.driving = False
        self._approvals = 0
        max_steps = int(self.settings.get("control.max_steps") or 30)
        settle = (self.settings.get("control.settle_ms") or 450) / 1000
        error_streak = 0
        pending: Item | None = None
        try:
            for step in range(max_steps):
                self.env.set_state("acting" if self.driving else "thinking")
                try:
                    res = await chat_with_fallback(self.llm, self.render)
                except LLMError as exc:
                    await self.deliver(f"I couldn't reach the model. {exc}", None, "Model error")
                    return
                self.last_activity = time.time()
                if res.thought:
                    log.info("thought: %s", res.thought[:300])
                if not res.calls:
                    self.history.append(Item(kind="assistant", text=res.text))
                    await self.deliver(res.text or "Sorry, I didn't get an answer from the model.", None, None)
                    return
                calls = res.calls[:12]
                self.history.append(Item(kind="assistant", text=res.text, calls=calls, thought=res.thought))
                pending = Item(kind="results")
                out: dict = {"assistant_text": res.text}
                changed = False
                terminal = False
                errors = 0
                for i, call in enumerate(calls):
                    if terminal:
                        pending.results.append((call, "skipped: task already finished"))
                        continue
                    log.info("tool %s %s", call.name, json.dumps(call.args)[:300])
                    try:
                        r = await self._exec(call, out)
                    except asyncio.CancelledError:
                        pending.results.append((call, "interrupted"))
                        for rest in calls[i + 1:]:
                            pending.results.append((rest, "not run: interrupted"))
                        raise
                    except Exception as exc:
                        log.warning("tool %s failed: %s", call.name, exc)
                        r = f"error: {exc}"
                    if r.startswith("error") or r.startswith("denied"):
                        errors += 1
                        if r.startswith("denied"):
                            # Stop the batch after a denial.
                            pending.results.append((call, r))
                            for rest in calls[i + 1:]:
                                pending.results.append((rest, "not run: previous action denied"))
                            break
                    pending.results.append((call, r))
                    if call.name in SCREEN_CHANGING:
                        changed = True
                    if call.name in IMAGE_TOOLS or "frame" in out and call.name == "highlight":
                        changed = False
                    if call.name in TERMINAL and not r.startswith("error"):
                        terminal = True
                pending.frame = out.get("frame")
                pending.extra_frames = out.get("extra", [])
                if changed and not terminal and self.settings.get("control.auto_screenshot", True):
                    await asyncio.sleep(settle)
                    self.frame = await self.env.capture()
                    pending.frame = self.frame
                    pending.text = "Screen after your actions."
                self.history.append(pending)
                pending = None
                if terminal:
                    return
                error_streak = error_streak + 1 if errors and errors == len(calls) else 0
                if error_streak >= 3:
                    await self.deliver("I keep running into errors on this one, so I'll stop here. "
                                       "Could you check the screen or rephrase what you need?", None, None)
                    return
            await self.deliver("I've taken a lot of steps on this without finishing, so I'm pausing here. "
                               "Tell me if you want me to keep going.", None, None)
        except asyncio.CancelledError:
            if pending is not None:
                pending.text = "[The user interrupted / took back control here.]"
                self.history.append(pending)
            raise
        finally:
            self.driving = False
            self.last_activity = time.time()
            self._trim()

    def _trim(self, max_items: int = 80) -> None:
        if len(self.history) <= max_items:
            return
        # Cut at a user turn so tool-call/results pairs stay intact.
        cut = len(self.history) - max_items
        while cut < len(self.history) and self.history[cut].kind != "user":
            cut += 1
        self.history = self.history[cut:]


def _merge_consecutive(msgs: list[dict]) -> list[dict]:
    """Some chat templates require strict user/assistant alternation."""
    out: list[dict] = []
    for m in msgs:
        if out and m["role"] == "user" and out[-1]["role"] == "user":
            prev = out[-1]["content"]
            prev = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
            cur = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            out[-1] = {"role": "user", "content": prev + cur}
        else:
            out.append(m)
    return out


_POPUP_KEYS = ("popup_markdown", "popup", "details", "details_markdown", "markdown", "popup_content",
               "popup_text", "detail", "content", "body")
_POPUP_MENTION = re.compile(r"\bpop[- ]?up\b|\b(chat )?panel\b|\bon (your |the )?screen\b.*\b(details|steps)\b", re.I)


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (s or "").lower())


def _structured(d: str) -> bool:
    return bool(
        re.search(r"```", d) or
        len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+\S", d, re.M)) >= 2 or
        re.search(r"^\s*\|.*\|\s*$", d, re.M) or
        re.search(r"^#{1,4}\s", d, re.M) or
        re.search(r"https?://", d))


def _worth_popup(details: str) -> bool:
    """A popup is for substance: a long answer, or something structured you'd want to read or copy."""
    words = _wc(clean_for_speech(details or ""))
    return words >= 40 or (_structured(details or "") and words >= 8)


def _adds_detail(speech: str, details: str) -> bool:
    """True when the popup text is a genuinely fuller answer, not a copy of what was spoken."""
    sw, dw = _words(clean_for_speech(speech)), _words(details)
    if not dw:
        return False
    novel = set(dw) - set(sw)
    if _structured(details):
        # Steps, code or a table: brief is fine as long as it says something new.
        return len(novel) >= max(4, 0.3 * len(set(dw)))
    return len(dw) >= max(len(sw) * 1.25, len(sw) + 6) and len(novel) >= max(5, 0.3 * len(set(dw)))


def _popup_arg(a: dict) -> str | None:
    for k in _POPUP_KEYS:
        v = a.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _mentions_popup(speech: str) -> bool:
    return bool(_POPUP_MENTION.search(speech or ""))


def _strip_popup_mention(speech: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", speech.strip())
    kept = [p for p in parts if not _POPUP_MENTION.search(p)]
    return " ".join(kept) or "Sorry, I lost the details there. Could you ask me again?"
