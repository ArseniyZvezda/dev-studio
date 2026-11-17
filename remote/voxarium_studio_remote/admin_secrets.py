"""One-time secret references and in-process Windows clipboard (dev spec §16.9).

Best-effort reference clearing is not Python memory zeroization. Clipboard history,
cloud sync and other applications are outside this process's control.
"""
from __future__ import annotations

import os
import threading
import time

SECRET_TTL = 60.0


class ClipboardUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Clipboard unavailable.")


class WindowsClipboard:
    def __init__(self):
        if os.name != "nt": raise ClipboardUnavailable()
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        self.user.CreateWindowExW.restype = wintypes.HWND
        self.user.DestroyWindow.argtypes = [wintypes.HWND]
        self.user.DestroyWindow.restype = wintypes.BOOL
        self.user.OpenClipboard.argtypes = [wintypes.HWND]
        self.user.OpenClipboard.restype = wintypes.BOOL
        self.user.CloseClipboard.restype = wintypes.BOOL
        self.user.EmptyClipboard.restype = wintypes.BOOL
        self.user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user.SetClipboardData.restype = wintypes.HANDLE
        self.user.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self.kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalLock.restype = ctypes.c_void_p
        self.kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalFree.restype = wintypes.HGLOBAL

    def write(self, value: str) -> int:
        data = (value + "\0").encode("utf-16-le")
        handle = self.kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle: raise ClipboardUnavailable()
        opened = False
        owner = None
        try:
            pointer = self.kernel.GlobalLock(handle)
            if not pointer: raise ClipboardUnavailable()
            try: self.ctypes.memmove(pointer, data, len(data))
            finally: self.kernel.GlobalUnlock(handle)
            # EmptyClipboard followed by SetClipboardData needs a non-NULL
            # owner HWND. Use a hidden in-process message-only system window,
            # not a subprocess or an assumption about a console window.
            owner = self.user.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0,
                                               self.ctypes.c_void_p(-3), None, None, None)
            if not owner: raise ClipboardUnavailable()
            opened = bool(self.user.OpenClipboard(owner))
            if not opened or not self.user.EmptyClipboard(): raise ClipboardUnavailable()
            if not self.user.SetClipboardData(13, handle): raise ClipboardUnavailable()  # CF_UNICODETEXT
            handle = None  # ownership transferred to Windows
            try:
                sequence = self.user.GetClipboardSequenceNumber()
                if not sequence: raise ClipboardUnavailable()
            except Exception:
                # Transfer succeeded, but there is no sequence to track for TTL.
                # The ORIGINAL lock is still held, so the current item is ours:
                # remove it now, never unlock/reopen and risk a foreign update.
                try: self.user.EmptyClipboard()
                except Exception: pass
                raise ClipboardUnavailable() from None
            return sequence
        finally:
            if opened: self.user.CloseClipboard()
            if owner: self.user.DestroyWindow(owner)
            if handle: self.kernel.GlobalFree(handle)

    def clear_if_sequence(self, sequence: int) -> None:
        # Check and empty while holding the clipboard lock: no TOCTOU overwrite
        # of a subsequent clipboard item owned by another application.
        if not self.user.OpenClipboard(None): return
        try:
            if self.user.GetClipboardSequenceNumber() == sequence:
                self.user.EmptyClipboard()
        finally: self.user.CloseClipboard()


class EphemeralSecret:
    """Hidden by default; reveal/copy never rotate or recover a stored key."""
    def __init__(self, key: str, *, clipboard=None, clock=time.monotonic, ttl=SECRET_TTL):
        self._key = key
        self._clock = clock
        self.expires_at = clock() + min(ttl, SECRET_TTL)
        self._clipboard = clipboard
        self._sequence = None
        self._lock = threading.RLock()
        self._timer = threading.Timer(min(ttl, SECRET_TTL), self.close)
        self._timer.daemon = True
        self._timer.start()

    def __repr__(self):
        return "<EphemeralSecret redacted>"

    @property
    def remaining(self):
        return max(0.0, self.expires_at - self._clock())

    def reveal(self) -> str | None:
        with self._lock:
            if self.remaining <= 0:
                self.close()
            return self._key

    def copy(self) -> bool:
        with self._lock:
            key = self.reveal()
            if key is None: return False
            try:
                if self._clipboard is None: self._clipboard = WindowsClipboard()
                self._sequence = self._clipboard.write(key)
            except Exception:
                # Adapter/OS exceptions may contain arbitrary secret-bearing text.
                return False
            return True

    def close(self):
        with self._lock:
            self._key = None
            self._timer.cancel()
            if self._sequence is not None:
                try: self._clipboard.clear_if_sequence(self._sequence)
                except Exception: pass
                self._sequence = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
