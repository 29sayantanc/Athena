"""Local WebSocket bridge between the Python core and the Electron shell.

Protocol (JSON text frames)
  core  -> shell : {"type":"event","event":"state","data":{...}}
  shell -> core  : {"type":"rpc","id":7,"method":"settings.get","params":{...}}
  core  -> shell : {"type":"rpc_result","id":7,"result":...} | {"type":"rpc_result","id":7,"error":"..."}
  shell -> core  : {"type":"event","event":"popup.action","data":{...}}
Only 127.0.0.1, and the shell must present the per-launch token.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve

log = logging.getLogger("athena.bus")

Handler = Callable[[dict], Awaitable[Any]]


class Bus:
    def __init__(self, port: int, token: str):
        self.port = port
        self.token = token
        self.clients: set[ServerConnection] = set()
        self.rpc: dict[str, Handler] = {}
        self.events: dict[str, Callable[[dict], Any]] = {}
        self._server = None
        self.on_connect: Callable[[], Any] | None = None

    def method(self, name: str):
        def deco(fn: Handler):
            self.rpc[name] = fn
            return fn
        return deco

    def on(self, name: str, fn: Callable[[dict], Any]) -> None:
        self.events[name] = fn

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", self.port, max_size=16 * 1024 * 1024,
                                   ping_interval=20, ping_timeout=20)
        log.info("bus listening on ws://127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def emit(self, event: str, data: Any = None) -> None:
        if not self.clients:
            return
        msg = json.dumps({"type": "event", "event": event, "data": data})
        for ws in list(self.clients):
            asyncio.ensure_future(self._safe_send(ws, msg))

    @staticmethod
    async def _safe_send(ws: ServerConnection, msg: str) -> None:
        try:
            await ws.send(msg)
        except Exception:
            pass

    async def _handle(self, ws: ServerConnection) -> None:
        q = parse_qs(urlparse(ws.request.path).query)
        if not hmac.compare_digest((q.get("token") or [""])[0], self.token):
            await ws.close(4001, "bad token")
            return
        self.clients.add(ws)
        log.info("shell connected")
        if self.on_connect:
            try:
                r = self.on_connect()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("on_connect failed")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "rpc":
                    asyncio.ensure_future(self._dispatch(ws, msg))
                elif msg.get("type") == "event":
                    fn = self.events.get(msg.get("event"))
                    if fn:
                        try:
                            r = fn(msg.get("data") or {})
                            if asyncio.iscoroutine(r):
                                asyncio.ensure_future(r)
                        except Exception:
                            log.exception("event handler %s failed", msg.get("event"))
        finally:
            self.clients.discard(ws)
            log.info("shell disconnected")

    async def _dispatch(self, ws: ServerConnection, msg: dict) -> None:
        fn = self.rpc.get(msg.get("method"))
        reply: dict = {"type": "rpc_result", "id": msg.get("id")}
        if fn is None:
            reply["error"] = f"unknown method {msg.get('method')}"
        else:
            try:
                reply["result"] = await fn(msg.get("params") or {})
            except Exception as exc:
                if type(exc).__name__ in ("LLMError", "ValueError", "ConnectError", "TimeoutException"):
                    log.warning("rpc %s failed: %s", msg.get("method"), exc)
                else:
                    log.exception("rpc %s failed", msg.get("method"))
                reply["error"] = str(exc) or type(exc).__name__
        await self._safe_send(ws, json.dumps(reply, default=str))
