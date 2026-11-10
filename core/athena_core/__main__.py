"""Entry point: python -m athena_core --port 47321 --token XYZ --parent-pid 1234"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys

# DPI awareness must be set before anything touches the screen.
from . import win32

win32.set_dpi_awareness()

from .bus import Bus  # noqa: E402
from .config import LOGS_DIR, Settings  # noqa: E402
from .controller import Controller  # noqa: E402


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOGS_DIR / "core.log", maxBytes=2_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def amain(args) -> None:
    settings = Settings()
    token = args.token or os.environ.get("ATHENA_TOKEN", "")
    if not token:
        logging.warning("No token supplied; using an insecure dev token.")
        token = "dev"
    port = args.port or int(settings.get("shell.ws_port"))
    bus = Bus(port, token)
    ctrl = Controller(settings, bus)
    stop = asyncio.Event()

    @bus.method("app.shutdown")
    async def _(p):
        asyncio.get_running_loop().call_later(0.2, stop.set)
        return True

    await bus.start()
    await ctrl.start()
    logging.info("Athena core ready (port %d)", port)
    print("ATHENA_READY", flush=True)

    async def watchdog():
        while not stop.is_set():
            await asyncio.sleep(3)
            if not parent_alive(args.parent_pid):
                logging.info("shell process gone; exiting")
                stop.set()

    asyncio.ensure_future(watchdog())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await ctrl.shutdown()
        await bus.stop()


def main() -> None:
    p = argparse.ArgumentParser("athena_core")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--token", default="")
    p.add_argument("--parent-pid", type=int, default=0)
    args = p.parse_args()
    setup_logging()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
