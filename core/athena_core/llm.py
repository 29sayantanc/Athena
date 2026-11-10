"""Model-agnostic chat client for any OpenAI-compatible endpoint.

Supports two tool protocols:
  native - OpenAI `tools` / `tool_calls` (LM Studio, Venice, OpenRouter, Ollama…)
  json   - the model replies with a JSON object of actions (works with any
           instruction-following model, including ones without tool support)
`auto` tries native and falls back to json per model when the server or
model can't do function calling.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field

import httpx

from .tools import TOOL_NAMES, openai_tools

log = logging.getLogger("athena.llm")

THINK_RE = re.compile(
    r"<(think|thinking|reasoning|reflection)>.*?</\1>"
    r"|\[THINK\].*?\[/THINK\]"
    r"|<\|channel\|>analysis.*?(<\|end\|>|<\|start\|>|(?=<\|channel\|>final))"
    r"|<\|begin_of_thought\|>.*?<\|end_of_thought\|>",
    re.S | re.I)
OPEN_THINK_RE = re.compile(r"<(think|thinking|reasoning)>|\[THINK\]|<\|channel\|>analysis", re.I)
CLOSE_THINK_RE = re.compile(r"</(think|thinking|reasoning)>|\[/THINK\]", re.I)
CHANNEL_JUNK_RE = re.compile(r"<\|(start|end|channel|message|return)\|>(assistant|final)?", re.I)


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])


@dataclass
class LLMResult:
    text: str
    calls: list[ToolCall]
    thought: str = ""
    mode: str = "native"
    usage: dict | None = None


class LLMError(Exception):
    pass


def strip_think(text: str | None) -> str:
    if not text:
        return ""
    t = THINK_RE.sub("", text)
    # Orphan closing tag: template injected the opening <think> into the prompt,
    # so everything before the close tag is reasoning.
    m = CLOSE_THINK_RE.search(t)
    if m:
        t = t[m.end():]
    # Unterminated opening tag (model ran out of tokens while thinking).
    m = OPEN_THINK_RE.search(t)
    if m:
        t = t[: m.start()]
    t = CHANNEL_JUNK_RE.sub("", t)
    return t.strip()


def _balanced(s: str, start: int) -> str | None:
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _loads_lenient(s: str):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", s)            # trailing commas
        fixed = re.sub(r"(?<![\\])'", '"', fixed) if '"' not in fixed else fixed
        return json.loads(fixed)


def extract_json(text: str):
    """Find the first parseable JSON object/array in free text (handles ``` fences)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    for cand in candidates:
        for m in re.finditer(r"[\[{]", cand):
            block = _balanced(cand, m.start())
            if not block:
                continue
            try:
                return _loads_lenient(block)
            except Exception:
                continue
    return None


def calls_from_json(obj) -> tuple[list[ToolCall], str]:
    """Accepts {"thought","actions":[...]}, {"tool","args"}, [..], or {"name","arguments"}."""
    thought = ""
    if isinstance(obj, dict) and "actions" in obj:
        thought = str(obj.get("thought", "") or "")
        items = obj.get("actions") or []
    elif isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = [obj]
    else:
        return [], ""
    calls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("tool") or it.get("name") or (it.get("function") or {}).get("name")
        args = it.get("args") or it.get("arguments") or it.get("parameters") or \
            (it.get("function") or {}).get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if name in TOOL_NAMES and isinstance(args, dict):
            calls.append(ToolCall(name=name, args=args))
    return calls, thought


QWEN_TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)


class LLMClient:
    def __init__(self, settings):
        self.settings = settings
        self._json_fallback: set[str] = set()   # models that failed native tools
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(settings.get("model.request_timeout_s"), connect=5.0))

    async def close(self):
        await self._client.aclose()

    def _endpoint(self, provider: dict | None = None) -> tuple[str, dict]:
        p = provider or self.settings.provider()
        headers = {"Content-Type": "application/json"}
        if p.get("api_key"):
            headers["Authorization"] = f"Bearer {p['api_key']}"
        if p.get("id") == "openrouter":
            headers["X-Title"] = "Athena"
        return p["base_url"].rstrip("/"), headers

    def model_id(self) -> str:
        return self.settings.provider().get("model") or ""

    def effective_mode(self) -> str:
        mode = self.settings.get("model.tool_mode")
        if mode == "auto":
            return "json" if self.model_id() in self._json_fallback else "native"
        return mode

    async def list_models(self, provider: dict | None = None) -> list[dict]:
        base, headers = self._endpoint(provider)
        try:
            r = await self._client.get(base + "/models", headers=headers, timeout=10.0)
        except httpx.ConnectError as exc:
            raise LLMError(f"Can't reach {base}. Is the model server (e.g. LM Studio's local server) running?") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"{base} didn't respond in time.") from exc
        if r.status_code in (401, 403):
            raise LLMError("The server rejected the API key.")
        r.raise_for_status()
        data = r.json().get("data", [])
        out = []
        for m in data:
            mid = m.get("id")
            if not mid:
                continue
            spec = m.get("model_spec") or {}
            caps = spec.get("capabilities") or {}
            out.append({"id": mid,
                        "vision": caps.get("supportsVision"),
                        "tools": caps.get("supportsFunctionCalling"),
                        "type": m.get("type") or spec.get("type")})
        return out

    async def chat(self, messages: list[dict], mode: str | None = None, max_tokens: int | None = None) -> LLMResult:
        mode = mode or self.effective_mode()
        model = self.model_id()
        if not model:
            raise LLMError("No model selected. Open Athena settings → Model and pick one.")
        base, headers = self._endpoint()
        body: dict = {"model": model, "messages": messages, "stream": False}
        # Sampling settings left at "server default" (None) are not sent at all,
        # so LM Studio / the provider applies the model's own preset.
        for key in ("temperature", "top_p"):
            val = self.settings.get(f"model.{key}")
            if val is not None:
                body[key] = val
        mt = max_tokens or self.settings.get("model.max_tokens")
        if mt:
            body["max_tokens"] = int(mt)
        if mode == "native":
            body["tools"] = openai_tools()
        try:
            r = await self._client.post(base + "/chat/completions", headers=headers, json=body)
        except httpx.ConnectError as exc:
            raise LLMError(f"Can't reach the model server at {base}. Is it running?") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("The model took too long to respond.") from exc
        if r.status_code >= 400:
            txt = r.text[:500]
            if mode == "native" and self.settings.get("model.tool_mode") == "auto" and r.status_code in (400, 422, 500) \
                    and re.search(r"tool|function", txt, re.I):
                log.warning("native tools rejected by %s; switching to JSON protocol", model)
                self._json_fallback.add(model)
                raise _NeedJsonMode()
            raise LLMError(f"Model server error {r.status_code}: {txt}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected response: {str(data)[:300]}")
        content = msg.get("content") or ""
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        text = strip_think(content)
        calls: list[ToolCall] = []
        thought = ""

        if mode == "native":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = extract_json(args) or {}
                if fn.get("name") in TOOL_NAMES:
                    calls.append(ToolCall(name=fn["name"], args=args, id=tc.get("id") or ToolCall("x", {}).id))
            if not calls and text:
                # Model wrote its tool call as text (common with local chat templates).
                leaked = QWEN_TOOLCALL_RE.findall(text)
                obj = [extract_json(x) for x in leaked] if leaked else extract_json(text)
                if obj:
                    calls, thought = calls_from_json(obj)
                    if calls:
                        text = ""
                        if self.settings.get("model.tool_mode") == "auto":
                            self._json_fallback.add(model)
        elif mode == "text":
            pass  # plain prose requested (e.g. writing the detailed answer); no tool parsing
        else:
            obj = extract_json(text)
            calls, thought = calls_from_json(obj) if obj is not None else ([], "")
            if calls:
                text = ""
        return LLMResult(text=text, calls=calls, thought=thought or strip_think(reasoning)[:400],
                         mode=mode, usage=data.get("usage"))


class _NeedJsonMode(Exception):
    pass


async def chat_with_fallback(client: LLMClient, build_messages) -> LLMResult:
    """build_messages(mode) -> messages. Retries once in JSON mode if native is rejected."""
    mode = client.effective_mode()
    try:
        return await client.chat(build_messages(mode), mode=mode)
    except _NeedJsonMode:
        return await client.chat(build_messages("json"), mode="json")
