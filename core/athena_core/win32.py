"""Windows input + window helpers via ctypes (no compiled deps).

All coordinates are PHYSICAL screen pixels (the process is per-monitor DPI
aware), which is what mss screenshots use too, so a pixel in a screenshot maps
1:1 to a SendInput target.

Every input Athena injects carries dwExtraInfo == ATHENA_MAGIC so the
low-level hooks can tell Athena's input apart from the user's (take-over
detection, hotkey handling).

On non-Windows platforms a recording stub is exposed so the agent can be
unit-tested.
"""
from __future__ import annotations

import logging
import math
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("athena.win32")

ATHENA_MAGIC = 0x00A7E4A0
IS_WINDOWS = sys.platform == "win32"


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom

    def as_dict(self) -> dict:
        return {"hwnd": self.hwnd, "title": self.title, "process": self.process, "rect": list(self.rect)}


# --------------------------------------------------------------------------
# Key name table (shared by both implementations and the hotkey parser)
# --------------------------------------------------------------------------
VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "apps": 0x5D, "menu": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplay": 0xB3,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x70 + _i - 1
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "0123456789":
    VK[_d] = ord(_d)
ALIASES = {"cmd": "win", "meta": "win", "super": "win", "windows": "win", "option": "alt",
           "del": "delete", "ins": "insert", "return": "enter", "escape": "esc",
           "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
           "control": "ctrl", "pgup": "pageup", "pgdn": "pagedown"}
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
                0xA3, 0xA5, 0x90, 0x2C}


def normalize_key(name: str) -> str:
    n = name.strip().lower().replace(" ", "")
    return ALIASES.get(n, n)


def parse_combo(combo: str) -> list[str]:
    """'Ctrl+Shift+T' -> ['ctrl','shift','t']. A literal '+' key is written 'plus'."""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    keys = [normalize_key(p) for p in parts]
    for k in keys:
        if k not in VK:
            raise ValueError(f"Unknown key: {k!r}")
    return keys


def _ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


SPEED_MS = {"instant": 0, "fast": 120, "natural": 280, "slow": 600}


# ==========================================================================
# Windows implementation
# ==========================================================================
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
    MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
    MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
    MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

    def set_dpi_awareness() -> None:
        try:
            # PER_MONITOR_AWARE_V2
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    def _send(*inputs: INPUT) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        n = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if n != len(inputs):
            err = ctypes.get_last_error()
            # UIPI: cannot inject into elevated windows from a normal process.
            raise OSError(f"SendInput blocked (err {err}). If the target app runs as administrator, "
                          f"Athena must also run as administrator.")

    def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, ATHENA_MAGIC)
        return inp

    def _key(vk: int, up: bool = False, scan: int | None = None, unicode: bool = False) -> INPUT:
        inp = INPUT(type=INPUT_KEYBOARD)
        flags = KEYEVENTF_KEYUP if up else 0
        if unicode:
            inp.ki = KEYBDINPUT(0, scan or 0, flags | KEYEVENTF_UNICODE, 0, ATHENA_MAGIC)
        else:
            if vk in EXTENDED_VKS:
                flags |= KEYEVENTF_EXTENDEDKEY
            sc = user32.MapVirtualKeyW(vk, 0) if scan is None else scan
            inp.ki = KEYBDINPUT(vk, sc, flags, 0, ATHENA_MAGIC)
        return inp

    def virtual_screen() -> tuple[int, int, int, int]:
        gm = user32.GetSystemMetrics
        return gm(SM_XVIRTUALSCREEN), gm(SM_YVIRTUALSCREEN), gm(SM_CXVIRTUALSCREEN), gm(SM_CYVIRTUALSCREEN)

    def cursor_pos() -> tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _abs_move(x: int, y: int) -> None:
        vx, vy, vw, vh = virtual_screen()
        nx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
        ny = int(round((y - vy) * 65535 / max(vh - 1, 1)))
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))

    def mouse_move(x: int, y: int, speed: str = "natural", on_step=None) -> None:
        dur = SPEED_MS.get(speed, 280) / 1000.0
        sx, sy = cursor_pos()
        dist = math.hypot(x - sx, y - sy)
        if dur <= 0 or dist < 4:
            _abs_move(x, y)
        else:
            # short hops are quicker than long sweeps
            dur = dur * min(1.0, 0.35 + dist / 1400.0)
            steps = max(6, int(dur * 120))
            for i in range(1, steps + 1):
                t = _ease(i / steps)
                _abs_move(int(sx + (x - sx) * t), int(sy + (y - sy) * t))
                if on_step:
                    on_step()
                time.sleep(dur / steps)
        _abs_move(x, y)

    _BTN = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}

    def mouse_button(button: str = "left", down: bool = True) -> None:
        d, u = _BTN[button]
        _send(_mouse(d if down else u))

    def click(button: str = "left", clicks: int = 1) -> None:
        d, u = _BTN[button]
        for i in range(clicks):
            _send(_mouse(d), _mouse(u))
            if i + 1 < clicks:
                time.sleep(0.06)

    def scroll(amount: int, horizontal: bool = False) -> None:
        """amount in wheel notches; positive = up (or right)."""
        flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        step = 1 if amount > 0 else -1
        for _ in range(abs(int(amount))):
            _send(_mouse(flag, data=120 * step))
            time.sleep(0.025)

    def press_combo(combo: str) -> None:
        keys = parse_combo(combo)
        vks = [VK[k] for k in keys]
        downs = [_key(v) for v in vks]
        ups = [_key(v, up=True) for v in reversed(vks)]
        _send(*downs)
        time.sleep(0.02)
        _send(*ups)

    def key_down(name: str) -> None:
        _send(_key(VK[normalize_key(name)]))

    def key_up(name: str) -> None:
        _send(_key(VK[normalize_key(name)], up=True))

    def type_unicode(text: str, delay: float = 0.004) -> None:
        for ch in text:
            if ch == "\n":
                press_combo("enter")
                continue
            if ch == "\t":
                press_combo("tab")
                continue
            units = ch.encode("utf-16-le")
            codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
            seq = [_key(0, scan=c, unicode=True) for c in codes] + \
                  [_key(0, up=True, scan=c, unicode=True) for c in codes]
            _send(*seq)
            if delay:
                time.sleep(delay)

    # -- clipboard ------------------------------------------------------------
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def _open_clipboard() -> None:
        for _ in range(20):
            if user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise OSError("Clipboard is busy")

    def get_clipboard() -> str:
        _open_clipboard()
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def set_clipboard(text: str) -> None:
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        _open_clipboard()
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                kernel32.GlobalFree(h)
                raise OSError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()

    # -- windows --------------------------------------------------------------
    def _title(hwnd) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _process_name(hwnd) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            kernel32.CloseHandle(h)

    def _info(hwnd) -> WindowInfo:
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return WindowInfo(int(hwnd or 0), _title(hwnd), _process_name(hwnd), (r.left, r.top, r.right, r.bottom))

    def foreground_window() -> WindowInfo | None:
        hwnd = user32.GetForegroundWindow()
        return _info(hwnd) if hwnd else None

    def window_at(x: int, y: int) -> WindowInfo | None:
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return None
        root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
        return _info(root or hwnd)

    def list_windows() -> list[WindowInfo]:
        out: list[WindowInfo] = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                info = _info(hwnd)
                l, t, r, b = info.rect
                if r - l > 40 and b - t > 40 and info.process.lower() not in ("athena.exe",):
                    out.append(info)
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return out

    def focus_window(hwnd: int) -> bool:
        hwnd = wintypes.HWND(hwnd)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Foreground lock workaround: a tap of Alt lets us steal focus.
        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        try:
            if fg_thread and fg_thread != cur:
                attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
            _send(_key(0x12), _key(0x12, up=True))
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_thread, False)
        return ok

    def send_raw_keys(seq: list[tuple[int, bool]]) -> None:
        """Used by hooks for the Start-menu mask trick: [(vk, up), ...]."""
        _send(*[_key(vk, up=up) for vk, up in seq])

# ==========================================================================
# Stub implementation (Linux/macOS dev + tests)
# ==========================================================================
else:
    ACTIONS: list[tuple] = []
    _STATE = {"pos": (500, 400), "clip": ""}

    def set_dpi_awareness() -> None:
        pass

    def virtual_screen():
        return 0, 0, 1920, 1080

    def cursor_pos():
        return _STATE["pos"]

    def mouse_move(x, y, speed="natural", on_step=None):
        ACTIONS.append(("move", x, y))
        _STATE["pos"] = (x, y)

    def mouse_button(button="left", down=True):
        ACTIONS.append(("button", button, down))

    def click(button="left", clicks=1):
        ACTIONS.append(("click", button, clicks, _STATE["pos"]))

    def scroll(amount, horizontal=False):
        ACTIONS.append(("scroll", amount, horizontal))

    def press_combo(combo):
        parse_combo(combo)
        ACTIONS.append(("keys", combo))

    def key_down(name):
        ACTIONS.append(("keydown", name))

    def key_up(name):
        ACTIONS.append(("keyup", name))

    def type_unicode(text, delay=0.0):
        ACTIONS.append(("type", text))

    def get_clipboard():
        return _STATE["clip"]

    def set_clipboard(text):
        _STATE["clip"] = text

    def foreground_window():
        return WindowInfo(1, "Inbox - Gmail - Google Chrome", "chrome.exe", (0, 0, 1920, 1080))

    def window_at(x, y):
        return foreground_window()

    def list_windows():
        return [foreground_window()]

    def focus_window(hwnd):
        ACTIONS.append(("focus", hwnd))
        return True

    def send_raw_keys(seq):
        ACTIONS.append(("raw", seq))
