"""Show a project file in the machine's own file manager (§6.2).

The counterpart to :mod:`voxarium_studio.core.picker`: that one asks the desktop a question, this
one hands it a location. Both exist because the Studio is a local tool whose browser and
server are the same computer — a page cannot open Explorer, and it should not have to
when the backend is already sitting on that desktop.

Two rules, the same ones the picker follows:

* **Loopback only**, enforced by the caller. The window appears on the desktop of whoever
  runs the server; on a Studio bound to the network that is not the person clicking, and
  opening windows on someone else's screen is not a feature.
* **Never a shell string.** The path is user-controlled data that has already been through
  ``ExplorerService.resolve()``; it is passed as an argument vector so that nothing in a
  filename can be read as syntax. A game project's asset tree is full of spaces, ampersands
  and non-ASCII, which is the ordinary case here rather than the adversarial one.

A file is revealed *selected in its containing folder*, which is what "open the location
of this file" means; a directory opens as itself. Selection is unavailable on Linux, where
the desktop-independent tool takes a directory and nothing else — the folder still opens,
and :data:`RevealResult.selected` says whether the file is highlighted in it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath

# The picker already drives PowerShell on Windows and already knows how to quote for it.
# Importing rather than re-deriving is deliberate: `_ps_quote`'s doubling rule is exactly
# the kind of thing that goes subtly wrong when it exists twice.
from .picker import _powershell, _ps_quote

log = logging.getLogger(__name__)

SPAWN_GRACE_SECONDS = 0.4
"""How long to watch for an immediate failure before answering.

The file manager outlives the request — waiting for it to exit would mean holding the
connection open until the user closes the window.
"""

FOREGROUND_TIMEOUT_SECONDS = 3.0
"""How long to wait for the window to exist before giving up on raising it."""

FOREGROUND_POLL_SECONDS = 0.1

FOREGROUND_GRACE_SECONDS = 5.0
"""Headroom over the timeout for PowerShell's own start-up, so the wait is never the
thing that kills a search that was about to succeed."""


class RevealError(RuntimeError):
    """No file manager could be started on this machine."""


@dataclass(frozen=True, slots=True)
class RevealResult:
    path: Path
    """What was actually opened — the file's parent when a file was revealed."""

    selected: bool
    """Whether the file itself is highlighted, or only its folder was opened."""

    def to_json(self) -> dict[str, object]:
        return {"path": str(self.path), "selected": self.selected}


def current_platform() -> str:
    """``windows`` | ``macos`` | ``freedesktop`` — which file manager convention applies."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "freedesktop"


def file_manager_available() -> tuple[bool, str]:
    """Whether this machine has something to open a folder with, and why not if it has not."""
    system = current_platform()
    if system in ("windows", "macos"):
        # `explorer.exe` and `open` ship with their systems; there is no version without.
        return True, ""
    if shutil.which("xdg-open") is None:
        return False, "xdg-open was not found on PATH."
    return True, ""


def _command(
    target: PurePath, *, is_dir: bool, platform: str | None = None
) -> tuple[list[str], PurePath, bool]:
    """The argument vector, what it opens, and whether the entry ends up selected.

    ``platform`` is a parameter rather than a module-level lookup so the other systems'
    branches can be tested from any one of them. Patching ``os.name`` instead would be a
    process-wide change — ``pathlib`` reads it to decide which ``Path`` subclass to build,
    so a test that patched it could not construct the path it wanted to pass in.
    """
    system = platform or current_platform()
    if is_dir:
        opened = target
    else:
        opened = target.parent

    if system == "windows":
        # `/select,<path>` is one token: a space after the comma makes explorer.exe ignore
        # the argument and open Documents instead, which looks like the feature silently
        # picking the wrong folder. Backslashes for the same reason — it does not take
        # forward slashes here even though the rest of Windows does.
        if is_dir:
            return ["explorer.exe", str(target)], opened, False
        return ["explorer.exe", f"/select,{target}"], opened, True

    if system == "macos":
        if is_dir:
            return ["open", str(target)], opened, False
        return ["open", "-R", str(target)], opened, True

    # Linux and the BSDs: xdg-open dispatches to whatever desktop is installed, and takes
    # a location rather than a selection, so a file reveals as its folder.
    return ["xdg-open", str(opened)], opened, False


async def reveal(target: Path) -> RevealResult:
    """Open ``target``'s location in the machine's file manager.

    Raises :class:`RevealError` when nothing could be started. Anything the file manager
    does after that is its own business — see :func:`_reap`.
    """
    available, reason = file_manager_available()
    if not available:
        raise RevealError(reason)

    is_dir = target.is_dir()
    command, opened, selected = _command(target, is_dir=is_dir)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RevealError(f"{command[0]} is not available on this machine") from exc
    except OSError as exc:
        raise RevealError(f"Could not start {command[0]}: {exc}") from exc

    # Give it a moment to fall over, then stop caring. `xdg-open` reports a missing
    # handler this way rather than by failing to start, and that is worth telling the user
    # about; a window that opened successfully simply has not exited yet.
    with contextlib.suppress(asyncio.TimeoutError):
        code = await asyncio.wait_for(process.wait(), SPAWN_GRACE_SECONDS)
        if code != 0 and current_platform() != "windows":
            # explorer.exe is exempt: it exits 1 on a perfectly successful reveal, which is
            # long-standing Windows behaviour and not something to report as a failure.
            detail = ""
            if process.stderr is not None:
                with contextlib.suppress(Exception):
                    detail = (await process.stderr.read()).decode("utf-8", "replace").strip()
            raise RevealError(detail or f"{command[0]} exited with code {code}")

    asyncio.create_task(_reap(process))

    # Detached from the response deliberately. Raising the window takes up to two seconds
    # of polling, and nothing on the page depends on the answer — the user is watching the
    # desktop by then, not the browser. Elsewhere this is not needed at all: `open` on
    # macOS activates Finder itself, and a freedesktop file manager raises its own window.
    if current_platform() == "windows":
        asyncio.create_task(_focus_window(opened))

    log.info("Revealed %s in the system file manager", opened)
    return RevealResult(path=opened, selected=selected)


# --------------------------------------------------------------- foreground (Windows)
#
# A window opened by a *detached backend* does not come to the front on its own. Windows
# refuses the foreground to a process that did not receive the last input — the browser
# did — so `explorer.exe` opens its window behind everything and the click looks like it
# did nothing. That is the same failure the folder dialog had (§5.1), and it cost a
# session to diagnose there precisely because the window *was* on screen the whole time.
#
# `picker.py` solves it by owning the window: an off-screen topmost form is the dialog's
# owner, and topmost is not subject to the foreground rules. Nothing here owns anything —
# the window belongs to the shell — so the window has to be found and raised instead.
#
# Found by *newness*: every Explorer folder window is snapshotted before the spawn, and
# whatever appears afterwards is ours. Matching on the title instead would be guesswork,
# because Explorer titles a window with the folder's display name unless the user turned
# on full paths, and two folders may share a leaf name.


def _find_script(folder: PurePath) -> str:
    """PowerShell that waits for a window showing ``folder`` and prints its handle.

    The shell's own window list is asked rather than the desktop's, because it is the only
    thing that maps a *folder* to a window. Enumerating top-level windows and taking
    whichever one is new does not survive contact with **Windows 11 tabs**: a reveal can
    land as a tab in a window that already existed, and then nothing new appears at all.
    Measured on this machine — two open folders, one window handle between them.

    The loop lives here so that one process answers the question, however long it takes;
    polling from Python would mean a PowerShell start-up per attempt.
    """
    return f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$target = {_ps_quote(str(folder))}
$deadline = (Get-Date).AddSeconds({FOREGROUND_TIMEOUT_SECONDS})
$shell = New-Object -ComObject Shell.Application
while ((Get-Date) -lt $deadline) {{
  foreach ($window in @($shell.Windows())) {{
    try {{
      # A tab and a window are both entries here; both carry the handle to raise.
      if ($window.Document.Folder.Self.Path -eq $target) {{
        [Console]::Out.Write([string]$window.HWND)
        exit 0
      }}
    }} catch {{ }}
  }}
  Start-Sleep -Milliseconds {int(FOREGROUND_POLL_SECONDS * 1000)}
}}
"""


async def _find_window(folder: PurePath) -> int | None:
    """The handle of the Explorer window showing ``folder``, once it exists."""
    program = _powershell()
    if program is None:
        log.debug("No PowerShell on PATH; cannot locate the Explorer window to raise")
        return None

    encoded = base64.b64encode(_find_script(folder).encode("utf-16-le")).decode("ascii")
    try:
        process = await asyncio.create_subprocess_exec(
            program,
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(
            process.communicate(), FOREGROUND_TIMEOUT_SECONDS + FOREGROUND_GRACE_SECONDS
        )
    except (OSError, asyncio.TimeoutError) as exc:
        log.debug("Could not locate the Explorer window: %s", exc)
        return None

    handle = raw.decode("ascii", "ignore").strip()
    return int(handle) if handle.isdigit() and int(handle) else None


def _raise_window(hwnd: int) -> bool:
    """Bring a window someone else owns to the front.

    `SetForegroundWindow` alone is refused here for the reason above. Attaching this
    thread's input queue to the current foreground window's thread makes Windows treat the
    two as one input context, which is what makes the call permissible — the documented
    escape hatch, and what every launcher and IDE ends up doing.
    """
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    SW_RESTORE = 9
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        foreground = user32.GetForegroundWindow()
        other = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        mine = kernel32.GetCurrentThreadId()

        attached = bool(other) and other != mine and bool(user32.AttachThreadInput(mine, other, True))
        try:
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(mine, other, False)
    except OSError as exc:  # pragma: no cover - defensive
        log.debug("Could not raise window %s: %s", hwnd, exc)
        return False


async def _focus_window(folder: PurePath) -> None:
    """Find the window showing ``folder`` and put it in front. Best effort."""
    hwnd = await _find_window(folder)
    if hwnd is None:
        log.warning("Revealed %s but never found its window to raise", folder)
        return
    if not _raise_window(hwnd):
        log.warning("Revealed %s but could not bring its window to the front", folder)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Collect the child so it does not linger as a zombie on POSIX.

    Detached from the request deliberately: the file manager exits when the user closes
    it, which may be never, and the answer to "did it open" was already sent.
    """
    with contextlib.suppress(Exception):
        await process.wait()


__all__ = [
    "RevealError",
    "RevealResult",
    "current_platform",
    "file_manager_available",
    "reveal",
]
