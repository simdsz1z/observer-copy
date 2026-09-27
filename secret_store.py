"""Store one API key with Windows user-scoped DPAPI encryption."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Saving an API key currently requires Windows DPAPI.")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    input_blob = DATA_BLOB(len(data), buffer)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        func = crypt32.CryptProtectData
        func.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        success = func(ctypes.byref(input_blob), "Observer MiniMax key", None, None, None, 1, ctypes.byref(output_blob))
    else:
        func = crypt32.CryptUnprotectData
        func.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        success = func(ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob))
    func.restype = ctypes.wintypes.BOOL
    if not success:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def save(path: Path, key: str) -> None:
    key = key.strip()
    if not key or len(key) > 2048 or "\n" in key or "\r" in key:
        raise ValueError("Enter a valid MiniMax API key.")
    encrypted = _crypt(key.encode("utf-8"), protect=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_bytes(encrypted)
    pending.replace(path)


def load(path: Path) -> str | None:
    if not path.exists():
        return None
    return _crypt(path.read_bytes(), protect=False).decode("utf-8")


def clear(path: Path) -> None:
    path.unlink(missing_ok=True)
