"""Tool schemas exposed to the model (OpenAI function-calling shape).

The same definitions drive native tool calling and the JSON fallback protocol
for models without function calling.
"""
from __future__ import annotations

import json

_XY = {
    "x": {"type": "number", "description": "X in screenshot pixel coordinates"},
    "y": {"type": "number", "description": "Y in screenshot pixel coordinates"},
    "element": {"type": "integer", "description": "Element id from the [n] marks/list. Prefer this over x/y when available."},
}

TOOLS: list[dict] = [
    # ---- observe -----------------------------------------------------------
    {"name": "take_screenshot",
     "description": "Capture the screen again to see the current state. Use it whenever you need to look: "
                    "after actions to verify they worked, after waiting for something to load, or before deciding.",
     "parameters": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "What you want to check"}}, "required": []}},
    {"name": "zoom",
     "description": "Look closer at a region of the latest screenshot at full resolution (for small text or icons). "
                    "Coordinates you use afterwards are still in the normal screenshot space.",
     "parameters": {"type": "object", "properties": {
         "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"}},
         "required": ["x1", "y1", "x2", "y2"]}},
    {"name": "read_clipboard",
     "description": "Return the text currently on the clipboard (e.g. after you pressed ctrl+c to copy page text).",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_windows",
     "description": "List open top-level windows (title and app).",
     "parameters": {"type": "object", "properties": {}, "required": []}},

    # ---- act ---------------------------------------------------------------
    {"name": "click",
     "description": "Move the mouse to a target and click it.",
     "parameters": {"type": "object", "properties": {
         **_XY,
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean", "description": "Double-click"},
         "description": {"type": "string", "description": "What you are clicking, e.g. 'Send button'"},
         "risky": {"type": "boolean", "description": "True if this click sends, deletes, buys, submits or is otherwise hard to undo"}},
         "required": ["description"]}},
    {"name": "move_mouse",
     "description": "Move the mouse pointer without clicking (e.g. to hover and reveal a tooltip or menu).",
     "parameters": {"type": "object", "properties": {**_XY}, "required": []}},
    {"name": "drag",
     "description": "Press at a start point, drag, release at an end point (sliders, selecting, moving things).",
     "parameters": {"type": "object", "properties": {
         "from_x": {"type": "number"}, "from_y": {"type": "number"},
         "to_x": {"type": "number"}, "to_y": {"type": "number"}},
         "required": ["from_x", "from_y", "to_x", "to_y"]}},
    {"name": "scroll",
     "description": "Scroll with the mouse wheel over a point (defaults to the screen centre / current pointer).",
     "parameters": {"type": "object", "properties": {
         **_XY,
         "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "amount": {"type": "integer", "description": "Wheel notches, 1-15 (about 3 lines each)"}},
         "required": ["direction"]}},
    {"name": "type_text",
     "description": "Type text into the focused field. Multi-line or long text is pasted. Click the field first if it is not focused.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"},
         "risky": {"type": "boolean"}}, "required": ["text"]}},
    {"name": "press_keys",
     "description": "Press a key or shortcut, e.g. 'enter', 'ctrl+c', 'ctrl+shift+t', 'alt+tab'. "
                    "Several presses separated by spaces: 'ctrl+a ctrl+c'.",
     "parameters": {"type": "object", "properties": {
         "keys": {"type": "string"},
         "risky": {"type": "boolean", "description": "True if this sends/deletes/submits"}}, "required": ["keys"]}},
    {"name": "focus_window",
     "description": "Bring a window to the front by (part of) its title.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}},
    {"name": "open_url",
     "description": "Open a web address in the default browser.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "wait",
     "description": "Wait for something to load or animate.",
     "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},

    # ---- teach / show ------------------------------------------------------
    {"name": "highlight",
     "description": "Draw attention to things on screen while you explain or teach: animated boxes, circles, "
                    "arrows, underlines or a spotlight, each with an optional short label. Highlights stay until "
                    "replaced, cleared, or the duration ends. Set wait_for_click to pause until the user clicks "
                    "(use this to walk the user through steps they perform themselves).",
     "parameters": {"type": "object", "properties": {
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "shape": {"type": "string", "enum": ["box", "circle", "arrow", "underline", "spotlight"]},
             "element": {"type": "integer"},
             "x": {"type": "number"}, "y": {"type": "number"},
             "box": {"type": "array", "items": {"type": "number"}, "description": "[x1,y1,x2,y2] region"},
             "label": {"type": "string", "description": "Short caption shown next to it, e.g. '1. Click Image'"}}}},
         "duration_s": {"type": "number", "description": "Auto-hide after N seconds (0 = keep)"},
         "keep_previous": {"type": "boolean", "description": "Add to existing highlights instead of replacing"},
         "wait_for_click": {"type": "boolean", "description": "Pause until the user clicks; returns where they clicked"},
         "timeout_s": {"type": "number"}},
         "required": ["items"]}},
    {"name": "clear_highlights",
     "description": "Remove all highlights.",
     "parameters": {"type": "object", "properties": {}, "required": []}},

    # ---- communicate -------------------------------------------------------
    {"name": "say",
     "description": "Speak a short line out loud WITHOUT ending your turn, e.g. narrate what you're doing "
                    "('Scrolling up to read the thread') or explain a step while highlighting it.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "request_confirmation",
     "description": "Ask the user to approve an action before doing it. Returns approved or denied.",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "description": "What you are about to do, e.g. 'Send the reply to Priya'"},
         "details_markdown": {"type": "string", "description": "Optional preview, e.g. the drafted email"}},
         "required": ["action"]}},
    {"name": "remember",
     "description": "Save a durable fact or preference about the user for future conversations.",
     "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
    {"name": "reply",
     "description": "Finish the task and answer the user. Two different texts: `speech` is a SHORT spoken "
                    "summary (1-2 sentences). `popup_markdown` is the FULL detailed answer shown in the panel by "
                    "the cursor: complete explanation, every step, specifics, examples, code — much longer than "
                    "the speech and never a copy of it. Leave popup_markdown empty when the short speech fully "
                    "answers the question (then don't mention a popup).",
     "parameters": {"type": "object", "properties": {
         "speech": {"type": "string", "description": "Short spoken summary, 1-2 sentences, no markdown"},
         "popup_markdown": {"type": "string", "description": "The complete, detailed answer in markdown (adds what the speech leaves out)"},
         "popup_title": {"type": "string"}},
         "required": ["speech"]}},
    {"name": "ask_user",
     "description": "End your turn with a question and wait for the user's spoken answer (they press the hotkey to reply).",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string"},
         "popup_markdown": {"type": "string"}}, "required": ["question"]}},
]

TOOL_NAMES = {t["name"] for t in TOOLS}
TERMINAL = {"reply", "ask_user"}
SCREEN_CHANGING = {"click", "drag", "scroll", "type_text", "press_keys", "focus_window", "open_url", "wait"}
INPUT_TOOLS = {"click", "move_mouse", "drag", "scroll", "type_text", "press_keys", "focus_window", "open_url"}
IMAGE_TOOLS = {"take_screenshot", "zoom"}


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": t} for t in TOOLS]


def _sig(t: dict) -> str:
    props = t["parameters"].get("properties", {})
    req = set(t["parameters"].get("required", []))
    parts = []
    for k, v in props.items():
        typ = v.get("type", "any")
        if "enum" in v:
            typ = "|".join(v["enum"])
        parts.append(f"{k}{'' if k in req else '?'}: {typ}")
    return f"{t['name']}({', '.join(parts)})"


def json_protocol_doc() -> str:
    lines = [f"- {_sig(t)} — {t['description']}" for t in TOOLS]
    example = json.dumps({"thought": "The Send button is element 14; the user already said to send.",
                          "actions": [{"tool": "click", "args": {"element": 14, "description": "Send button", "risky": True}},
                                      {"tool": "take_screenshot", "args": {"reason": "verify it sent"}}]})
    return (
        "TOOLS (you call them by replying with JSON only):\n" + "\n".join(lines) +
        "\n\nRESPONSE FORMAT — reply with exactly one JSON object and nothing else:\n"
        '{"thought": "<brief reasoning>", "actions": [{"tool": "<name>", "args": {...}}, ...]}\n'
        "Actions run in order. End the task with a `reply` (or `ask_user`) action.\nExample:\n" + example
    )
