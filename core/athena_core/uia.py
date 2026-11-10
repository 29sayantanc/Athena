"""Windows UI Automation: fast snapshot of interactive elements in the foreground window.

Uses a single cached FindAll call (one cross-process round trip) instead of
walking the tree property-by-property, so it is usually 50-600 ms even on big
web pages. Apps with poor accessibility (e.g. Photoshop canvases) simply
return few elements and the model falls back to raw coordinates.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import threading

from . import win32
from .screen import Element

log = logging.getLogger("athena.uia")

CONTROL_TYPES = {
    50000: "button", 50002: "checkbox", 50003: "combobox", 50004: "edit", 50005: "link",
    50007: "listitem", 50011: "menuitem", 50013: "radio", 50015: "slider", 50016: "spinner",
    50018: "tab", 50019: "tabitem", 50024: "treeitem", 50029: "dataitem", 50031: "splitbutton",
    50030: "document", 50033: "pane-input", 50025: "custom",
}
INTERACTIVE = [50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50015, 50016,
               50019, 50024, 50029, 50031]

UIA_BoundingRectanglePropertyId = 30001
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_IsEnabledPropertyId = 30010
UIA_IsOffscreenPropertyId = 30022
UIA_HelpTextPropertyId = 30013
TreeScope_Descendants = 4

_executor = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="athena-uia")
_local = threading.local()
_busy = threading.Event()


def _client():
    if getattr(_local, "auto", None) is None:
        import sys as _sys
        if not hasattr(_sys, "coinit_flags"):
            _sys.coinit_flags = 0  # COINIT_MULTITHREADED for this worker thread
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # already initialised on this thread (possibly in another mode) — that's fine
        uia = comtypes.client.GetModule("UIAutomationCore.dll")
        _local.mod = uia
        _local.auto = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                                                   interface=uia.IUIAutomation)
    return _local.auto


def _snapshot(hwnd: int, monitor: tuple[int, int, int, int], limit: int) -> list[Element]:
    auto = _client()
    root = auto.ElementFromHandle(hwnd)
    cache = auto.CreateCacheRequest()
    for pid in (UIA_NamePropertyId, UIA_ControlTypePropertyId, UIA_BoundingRectanglePropertyId,
                UIA_IsEnabledPropertyId, UIA_HelpTextPropertyId):
        cache.AddProperty(pid)
    cond = None
    for ct in INTERACTIVE:
        c = auto.CreatePropertyCondition(UIA_ControlTypePropertyId, ct)
        cond = c if cond is None else auto.CreateOrCondition(cond, c)
    onscreen = auto.CreatePropertyCondition(UIA_IsOffscreenPropertyId, False)
    cond = auto.CreateAndCondition(cond, onscreen)
    arr = root.FindAllBuildCache(TreeScope_Descendants, cond, cache)
    mx, my, mw, mh = monitor
    raw: list[tuple] = []
    seen: set[tuple] = set()
    for i in range(arr.Length):
        try:
            el = arr.GetElement(i)
            r = el.CachedBoundingRectangle
            l, t, rr, b = int(r.left), int(r.top), int(r.right), int(r.bottom)
            if rr - l < 4 or b - t < 4:
                continue
            if rr < mx or b < my or l > mx + mw or t > my + mh:
                continue
            if not el.CachedIsEnabled:
                continue
            name = (el.CachedName or el.CachedHelpText or "").strip().replace("\n", " ")
            ct = int(el.CachedControlType)
            key = (l, t, rr, b, name)
            if key in seen:
                continue
            seen.add(key)
            raw.append((CONTROL_TYPES.get(ct, "control"), name[:80], (l, t, rr, b)))
        except Exception:
            continue
    # Prefer named elements, then reading order.
    raw.sort(key=lambda e: (0 if e[1] else 1, e[2][1] // 12, e[2][0]))
    raw = raw[:limit]
    raw.sort(key=lambda e: (e[2][1] // 12, e[2][0]))
    return [Element(id=i + 1, role=r, name=n, rect=rect) for i, (r, n, rect) in enumerate(raw)]


def snapshot(monitor: tuple[int, int, int, int], limit: int = 120, timeout: float = 2.5) -> list[Element]:
    if not win32.IS_WINDOWS:
        return []
    if _busy.is_set():
        return []
    fg = win32.foreground_window()
    if not fg or not fg.hwnd:
        return []

    def run():
        _busy.set()
        try:
            return _snapshot(fg.hwnd, monitor, limit)
        finally:
            _busy.clear()

    fut = _executor.submit(run)
    try:
        return fut.result(timeout=timeout)
    except cf.TimeoutError:
        log.info("UIA snapshot timed out for %s", fg.process)
        return []
    except Exception as exc:
        log.info("UIA snapshot failed: %s", exc)
        return []


def describe(elements: list[Element], frame) -> str:
    """Compact text list for the model, coordinates in the frame's image space."""
    lines = []
    for el in elements:
        cx, cy = frame.to_image(*el.center)
        label = f'"{el.name}"' if el.name else "(unnamed)"
        lines.append(f"[{el.id}] {el.role} {label} @({cx},{cy})")
    return "\n".join(lines)
