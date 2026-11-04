"""Process lifetime helpers for agent cancellation and desktop exit (§2.7, §10.6)."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess


async def kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Reap an agent and its tools; POSIX callers must use start_new_session."""
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()
