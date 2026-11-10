"""Global low-level keyboard + mouse hooks (WH_KEYBOARD_LL / WH_MOUSE_LL).

Runs in its own thread with a Win32 message loop. Hook procs must return
quickly (Windows silently drops slow LL hooks), so they only update a few
fields and post events to the asyncio loop.

Responsibilities
  * Hotkey detection with press AND release (push-to-talk), any combo,
    modifier-only combos like Ctrl+Win, and Start-menu/Alt-menu masking.
  * Take-over detection: physical (non-Athena) mouse movement beyond a
    threshold, physical clicks, or Esc while Athena is driving.
  * Tutor mode: report the user's physical clicks.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from typing import Callable

from . import win32
from .win32 import ATHENA_MAGIC, VK, parse_combo

log = logging.getLogger("athena.hooks")

MODIFIER_VKS = {
    "ctrl": {0x11, 0xA2, 0xA3}, "shift": {0x10, 0xA0, 0xA1},
    "alt": {0x12, 0xA4, 0xA5}, "win": {0x5B, 0x5C},
}
MASK_VK = 0xE8  # unassigned VK used to stop Win/Alt release from opening Start/menu bar
ESC_VK = 0x1B


def combo_vk_sets(combo: str) -> list[set[int]]:
    out = []
    for k in parse_combo(combo):
        out.append(MODIFIER_VKS.get(k, {VK[k]}))
    return out


class HotkeyMatcher:
    """Pure-logic hotkey state machine (unit-testable, used by the hook)."""

    def __init__(self, combo: str):
        self.set_combo(combo)

    def set_combo(self, combo: str) -> None:
        self.combo = combo
        self.sets = combo_vk_sets(combo)
        self.all_vks = set().union(*self.sets)
        self.nonmod_vks = {v for s in self.sets for v in s
                           if not any(v in m for m in MODIFIER_VKS.values())}
        self.mask_vks = MODIFIER_VKS["win"] | MODIFIER_VKS["alt"]
        self.pressed: set[int] = set()
        self.active = False
        self.fired_this_press = False
        self.polluted = False  # another key was pressed while active

    def satisfied(self) -> bool:
        return all(self.pressed & s for s in self.sets)

    def extra_keys(self) -> set[int]:
        return {v for v in self.pressed if v not in self.all_vks}

    def key_down(self, vk: int) -> tuple[str | None, bool]:
        """Returns (event, swallow). event in {'down', 'cancel', None}."""
        is_repeat = vk in self.pressed
        self.pressed.add(vk)
        if self.active:
            if vk not in self.all_vks and not self.polluted:
                self.polluted = True
                return "cancel", False   # e.g. Ctrl+Win+Left: user meant a Windows shortcut
            return None, vk in self.nonmod_vks
        if not is_repeat and self.satisfied() and not self.extra_keys():
            self.active = True
            self.polluted = False
            self.fired_this_press = True
            return "down", vk in self.nonmod_vks
        return None, False

    def key_up(self, vk: int) -> tuple[str | None, bool, bool]:
        """Returns (event, swallow, need_mask)."""
        self.pressed.discard(vk)
        event, swallow, need_mask = None, False, False
        if self.active and vk in self.all_vks:
            self.active = False
            event = "up"
            swallow = vk in self.nonmod_vks
        if vk in self.mask_vks and self.fired_this_press and vk in self.all_vks:
            need_mask = True
            swallow = True
        if not (self.pressed & self.all_vks):
            self.fired_this_press = False
        return event, swallow, need_mask


class InputHooks:
    def __init__(self, loop: asyncio.AbstractEventLoop, hotkey: str,
                 on_hotkey: Callable[[str], None], on_takeover: Callable[[str], None],
                 on_user_click: Callable[[int, int, str], None],
                 on_user_wheel: Callable[[], None] | None = None):
        self.loop = loop
        self.matcher = HotkeyMatcher(hotkey)
        self.on_hotkey = on_hotkey
        self.on_takeover = on_takeover
        self.on_user_click = on_user_click
        self.enabled = True
        # takeover state (read in hook thread; simple attribute writes are atomic under the GIL)
        self.takeover_armed = False
        self.takeover_mouse = True
        self.takeover_esc = True
        self.takeover_threshold = 40
        self._acc = 0.0
        self._last_pt: tuple[int, int] | None = None
        self.watch_clicks = False
        self.watch_wheel = False
        self.on_user_wheel = on_user_wheel
        self.esc_always = False   # Esc cancels listening/thinking even when not driving
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    # -- control API (asyncio thread) ---------------------------------------
    def set_hotkey(self, combo: str) -> None:
        self.matcher.set_combo(combo)

    def arm_takeover(self, threshold: int, mouse: bool, esc: bool) -> None:
        self.takeover_threshold = threshold
        self.takeover_mouse = mouse
        self.takeover_esc = esc
        self._acc = 0.0
        self._last_pt = None
        self.takeover_armed = True

    def disarm_takeover(self) -> None:
        self.takeover_armed = False

    def reset_motion(self) -> None:
        """Called after Athena injects input so her own moves never count."""
        self._acc = 0.0
        self._last_pt = None

    def _post(self, fn, *args) -> None:
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass

    # -- hook thread ---------------------------------------------------------
    def start(self) -> None:
        if not win32.IS_WINDOWS:
            log.warning("Input hooks are Windows-only; running without global hotkey.")
            return
        self._thread = threading.Thread(target=self._run, name="athena-hooks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if win32.IS_WINDOWS and self._thread_id:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT

    def _run(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        WM_APP_MASK = 0x8001
        WM_APP_REINSTALL = 0x8002
        self._thread_id = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        pending_masks: list[int] = []

        def kb_proc(n_code, w_param, l_param):
            try:
                if n_code == 0:
                    ks = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if ks.dwExtraInfo != ATHENA_MAGIC:
                        vk = int(ks.vkCode)
                        down = w_param in (0x0100, 0x0104)
                        if down and vk == ESC_VK:
                            if (self.takeover_armed and self.takeover_esc) or self.esc_always:
                                self._post(self.on_takeover, "esc")
                        if self.enabled:
                            if down:
                                ev, swallow = self.matcher.key_down(vk)
                                if ev:
                                    self._post(self.on_hotkey, ev)
                                if swallow:
                                    return 1
                            else:
                                ev, swallow, need_mask = self.matcher.key_up(vk)
                                if ev:
                                    self._post(self.on_hotkey, ev)
                                if need_mask:
                                    pending_masks.append(vk)
                                    user32.PostThreadMessageW(self._thread_id, WM_APP_MASK, 0, 0)
                                if swallow:
                                    return 1
            except Exception:
                log.exception("kb hook error")
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        def ms_proc(n_code, w_param, l_param):
            try:
                if n_code == 0:
                    ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    ours = ms.dwExtraInfo == ATHENA_MAGIC
                    pt = (ms.pt.x, ms.pt.y)
                    if ours:
                        self._last_pt = pt
                    else:
                        if w_param == 0x0200:  # WM_MOUSEMOVE
                            if self.takeover_armed and self.takeover_mouse:
                                if self._last_pt is not None:
                                    self._acc += math.hypot(pt[0] - self._last_pt[0], pt[1] - self._last_pt[1])
                                    if self._acc >= self.takeover_threshold:
                                        self.takeover_armed = False
                                        self._post(self.on_takeover, "mouse")
                            self._last_pt = pt
                        elif w_param in (0x020A, 0x020E):  # wheel / horizontal wheel
                            if self.watch_wheel and self.on_user_wheel:
                                self.watch_wheel = False
                                self._post(self.on_user_wheel)
                        elif w_param in (0x0201, 0x0204, 0x0207):  # L/R/M button down
                            btn = {0x0201: "left", 0x0204: "right", 0x0207: "middle"}[w_param]
                            if self.watch_clicks:
                                self._post(self.on_user_click, pt[0], pt[1], btn)
                            elif self.takeover_armed and self.takeover_mouse:
                                self.takeover_armed = False
                                self._post(self.on_takeover, "click")
            except Exception:
                log.exception("mouse hook error")
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._kb_cb = HOOKPROC(kb_proc)
        self._ms_cb = HOOKPROC(ms_proc)

        def install():
            kh = user32.SetWindowsHookExW(13, self._kb_cb, hmod, 0)
            mh = user32.SetWindowsHookExW(14, self._ms_cb, hmod, 0)
            if not kh or not mh:
                log.error("SetWindowsHookEx failed: %s", ctypes.get_last_error())
            return kh, mh

        kh, mh = install()
        log.info("Input hooks installed (hotkey=%s)", self.matcher.combo)

        # Windows silently removes LL hooks that time out; re-install periodically when idle.
        def reinstall_timer():
            import time
            while self._thread_id:
                time.sleep(45)
                if not self.matcher.pressed:
                    user32.PostThreadMessageW(self._thread_id, WM_APP_REINSTALL, 0, 0)
        threading.Thread(target=reinstall_timer, daemon=True).start()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_APP_MASK:
                while pending_masks:
                    vk = pending_masks.pop(0)
                    try:
                        win32.send_raw_keys([(MASK_VK, False), (MASK_VK, True), (vk, True)])
                    except OSError:
                        log.exception("mask send failed")
            elif msg.message == WM_APP_REINSTALL:
                if not self.matcher.pressed:
                    user32.UnhookWindowsHookEx(kh)
                    user32.UnhookWindowsHookEx(mh)
                    kh, mh = install()
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(kh)
        user32.UnhookWindowsHookEx(mh)
        self._thread_id = 0
