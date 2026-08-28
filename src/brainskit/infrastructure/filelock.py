"""Blocking shared/exclusive file locks over the platform standard library."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, ClassVar


@contextmanager
def file_lock(path: Path, *, shared: bool) -> Iterator[None]:
    """Hold one blocking lock until the context exits.

    POSIX uses whole-file ``flock`` semantics. Windows exposes byte-range locks,
    so the first byte (including beyond the empty file's end) is the stable lock
    region. Both paths preserve the vault's existing blocking shared/exclusive
    contract.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            windows_lock = _lock_windows(handle, shared=shared)
        else:
            _lock_posix(handle, shared=shared)
        try:
            yield
        finally:
            if os.name == "nt":
                _unlock_windows(windows_lock)
            else:
                _unlock_posix(handle)


def _lock_posix(handle: BinaryIO, *, shared: bool) -> None:
    import fcntl

    api = vars(fcntl)
    api["flock"](handle.fileno(), api["LOCK_SH"] if shared else api["LOCK_EX"])


def _unlock_posix(handle: BinaryIO) -> None:
    import fcntl

    api = vars(fcntl)
    api["flock"](handle.fileno(), api["LOCK_UN"])


def _windows_api() -> tuple[Any, Any, type[Any], Any, Any]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("Internal", wintypes.WPARAM),
            ("InternalHigh", wintypes.WPARAM),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)
    lock_file_ex = kernel32.LockFileEx
    lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock_file_ex.restype = wintypes.BOOL
    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    unlock_file_ex.restype = wintypes.BOOL
    return ctypes, msvcrt, Overlapped, lock_file_ex, unlock_file_ex


def _lock_windows(handle: BinaryIO, *, shared: bool) -> tuple[Any, int, Any]:
    ctypes, msvcrt, overlapped_type, lock_file_ex, unlock_file_ex = _windows_api()
    overlapped = overlapped_type()
    os_handle = msvcrt.get_osfhandle(handle.fileno())
    flags = 0x00000002 if not shared else 0
    if not lock_file_ex(os_handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        raise vars(ctypes)["WinError"](vars(ctypes)["get_last_error"]())
    return unlock_file_ex, os_handle, overlapped


def _unlock_windows(lock: tuple[Any, int, Any]) -> None:
    import ctypes

    unlock_file_ex, os_handle, overlapped = lock
    if not unlock_file_ex(os_handle, 0, 1, 0, ctypes.byref(overlapped)):
        raise vars(ctypes)["WinError"](vars(ctypes)["get_last_error"]())
