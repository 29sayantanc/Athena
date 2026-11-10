"""API keys at rest: Windows DPAPI (current user) when available, plaintext elsewhere."""
from __future__ import annotations

import base64
import sys

PREFIX = "dpapi:"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(value: str) -> str:
    if not value or sys.platform != "win32" or value.startswith(PREFIX):
        return value
    try:
        return PREFIX + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")
    except Exception:
        return value


def unprotect(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        return _dpapi(base64.b64decode(value[len(PREFIX):]), False).decode("utf-8")
    except Exception:
        return ""
