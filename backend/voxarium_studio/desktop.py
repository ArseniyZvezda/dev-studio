"""Electron lifetime supervision on Windows (dev spec §2.7)."""
from __future__ import annotations

import ctypes
import json
import os
import threading
from pathlib import Path

SHUTDOWN_GRACE_SECONDS = 15


def _contain_process_tree(kernel) -> int:
    """Keep every descendant in a kill-on-close Windows job (dev spec §2.7).

    The handle is deliberately kept until process exit, including crashes. It is
    non-inheritable: a surviving tool cannot keep the job alive after the backend.
    Explicit breakaway is reserved for the update relauncher (§12.1).
    """
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000 | 0x0800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    os.environ["VOXARIUM_STUDIO_DESKTOP_JOB"] = "1"
    return job


def _write_owner(kernel, stop_file: str) -> None:
    """Let Electron wait for this process even after HTTP stops accepting requests."""
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, *([ctypes.c_void_p] * 4)]
    kernel.GetProcessTimes.restype = ctypes.c_int
    times = [ctypes.c_ulonglong() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    Path(stop_file + ".owner.json").write_text(json.dumps({
        "pid": os.getpid(), "created": str(times[0].value),
    }), encoding="utf-8")


def supervise(server) -> None:
    """Request normal ASGI shutdown when the desktop exits or requests a stop.

    A process handle, rather than polling a PID, also handles a crashed Electron
    and cannot confuse a recycled PID with the original parent.
    """
    parent = os.environ.get("VOXARIUM_STUDIO_DESKTOP_PARENT")
    stop_file = os.environ.get("VOXARIUM_STUDIO_DESKTOP_STOP_FILE")
    if os.name != "nt" or not parent or not stop_file:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    job = _contain_process_tree(kernel)
    _write_owner(kernel, stop_file)
    handle = kernel.OpenProcess(0x00100000, False, int(parent))  # SYNCHRONIZE
    if not handle:
        server.should_exit = True

    def watch() -> None:
        try:
            while handle and kernel.WaitForSingleObject(handle, 500) == 258:  # WAIT_TIMEOUT
                if Path(stop_file).exists():
                    break
            server.should_exit = True
            # ASGI can hang waiting for a tool or a request. This daemon survives
            # an Electron crash too; normal backend exit closes the job first.
            threading.Event().wait(SHUTDOWN_GRACE_SECONDS)
            kernel.TerminateJobObject(job, 1)
        finally:
            if handle:
                kernel.CloseHandle(handle)

    threading.Thread(target=watch, name="desktop-lifetime", daemon=True).start()
