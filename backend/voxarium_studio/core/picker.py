"""The machine's own "choose a folder" dialog, opened by the backend.

The Studio is a local tool: the browser and the server are the same computer, so picking
a project can mean the real Explorer dialog rather than a web reimplementation of one.
The page cannot do this itself — the File System Access API hands back an opaque handle
and never a filesystem path, which is exactly what opening a project needs — so the
dialog is spawned by the backend as a subprocess and its answer comes back over REST.

Two rules keep that honest:

* **Loopback only.** The dialog appears on the desktop of whoever is running the server.
  On a bound-to-the-network Studio that is not the person clicking, so the endpoint
  declines and the UI falls back to its own browser (:mod:`voxarium_studio.core.browse`).
* **One at a time.** Two modal windows stacked behind each other is worse than one, so a
  second request takes the first one down before opening its own rather than refusing.
  Refusing was the original rule and it was wrong: the dialog nobody can see is precisely
  the one that gets asked for twice, and the refusal made that state permanent.

Nothing here may be left to the timeout to clean up. A dialog outlives the HTTP request
that spawned it — uvicorn keeps running a handler after its client disconnects, so a
reloaded page leaves a modal window on the desktop with nobody waiting for its answer.
That is what :func:`_close_when_abandoned` is for.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TITLE = "Select a project folder to open in Voxarium Studio"
DIALOG_TIMEOUT_SECONDS = 900.0
ABANDON_POLL_SECONDS = 1.0
LOCK_WAIT_SECONDS = 10.0

_dialog_lock = asyncio.Lock()
_active: _ActiveDialog | None = None


@dataclass(frozen=True, slots=True)
class PickResult:
    path: Path | None = None
    cancelled: bool = False
    available: bool = True
    reason: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "cancelled": self.cancelled,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(slots=True)
class _ActiveDialog:
    """The dialog currently on the desktop, and the only handle anyone else has on it.

    `stopped` is both the flag and the answer: once it is set the window is coming down,
    and its text is what the request still waiting on that window reports back. A killed
    dialog writes nothing to stdout, so without it the caller would get the generic "closed
    without returning a folder" for something the Studio did on purpose.
    """

    process: asyncio.subprocess.Process
    stopped: str = ""

    def stop(self, reason: str) -> None:
        if self.stopped:
            return
        self.stopped = reason
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.kill()


def dialog_supported() -> tuple[bool, str]:
    """Whether *this machine* can show a folder dialog at all, and why not if it cannot."""
    if os.name == "nt":
        return (True, "") if _powershell() else (False, "PowerShell was not found on PATH.")
    if _tk_available():
        return True, ""
    return False, "No native folder dialog is available (tkinter is not installed)."


async def pick_directory(
    *,
    start: Path | None = None,
    title: str = DEFAULT_TITLE,
    timeout: float = DIALOG_TIMEOUT_SECONDS,
    abandoned: Callable[[], Awaitable[bool]] | None = None,
) -> PickResult:
    """Show the dialog and wait for the user. Returns cancelled if they dismiss it.

    `abandoned` is asked periodically whether the client that wants this answer is still
    connected; when it is not, the window comes down with it.
    """
    supported, reason = dialog_supported()
    if not supported:
        return PickResult(available=False, reason=reason)

    # Whoever clicked last is the one at the keyboard: a dialog already up belongs to a
    # click that is now history, and may never have surfaced at all.
    _supersede_active()

    try:
        await asyncio.wait_for(_dialog_lock.acquire(), timeout=LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        # Superseding did not free it, so something is genuinely stuck holding the dialog.
        return PickResult(
            available=False,
            reason="A folder dialog is already open — finish or cancel that one first.",
        )
    try:
        argv = (
            _windows_command(title, start)
            if os.name == "nt"
            else _tk_command(title, start)
        )
        try:
            return await _run_dialog(argv, timeout, abandoned)
        except OSError as exc:
            log.warning("Could not open the native folder dialog", exc_info=True)
            return PickResult(available=False, reason=str(exc))
    finally:
        _dialog_lock.release()


def _supersede_active() -> None:
    if _active is not None:
        _active.stop("The folder dialog was replaced by a newer one.")


async def _close_when_abandoned(
    active: _ActiveDialog, abandoned: Callable[[], Awaitable[bool]]
) -> None:
    """Take the window down when the page that asked for it goes away.

    Not tidiness: uvicorn does not cancel a handler when its client disconnects, so a
    reloaded Open Project tab otherwise leaves a modal dialog on the desktop and the lock
    behind it held for the full :data:`DIALOG_TIMEOUT_SECONDS`.
    """
    while active.process.returncode is None and not active.stopped:
        await asyncio.sleep(ABANDON_POLL_SECONDS)
        try:
            gone = await abandoned()
        except Exception:  # A check that cannot answer must not close a live dialog.
            log.debug("Could not tell whether the folder dialog was abandoned", exc_info=True)
            return
        if gone:
            log.info("Closing the folder dialog: the page that asked for it went away.")
            active.stop("The page that opened the folder dialog went away.")
            return


async def _run_dialog(
    argv: list[str],
    timeout: float,
    abandoned: Callable[[], Awaitable[bool]] | None = None,
) -> PickResult:
    global _active

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    active = _ActiveDialog(process)
    _active = active
    watcher = (
        asyncio.create_task(_close_when_abandoned(active, abandoned))
        if abandoned is not None
        else None
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        active.stop("The folder dialog timed out.")
        await process.wait()
        return PickResult(cancelled=True, reason="The folder dialog timed out.")
    except asyncio.CancelledError:
        # The dialog must never outlive the request: a window with nobody behind it is
        # exactly what this module exists to avoid.
        active.stop("The folder dialog was cancelled.")
        await process.wait()
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
        if _active is active:
            _active = None

    if active.stopped:
        # We killed it, so there is no answer to parse and the reason is ours to give.
        return PickResult(cancelled=True, reason=active.stopped)

    answer = stdout.decode("utf-8", "replace").strip()

    # The script reports its own failures on stdout, because what a failing PowerShell
    # puts on *stderr* is CLIXML markup rather than a message anyone can read.
    if answer.startswith("ERROR:"):
        detail = _decode(answer[len("ERROR:") :]) or "The folder dialog failed."
        log.warning("Folder dialog reported an error: %s", detail)
        return PickResult(available=False, reason=detail)

    if answer.startswith("PICKED:"):
        selected = _decode(answer[len("PICKED:") :])
        if not selected:
            return PickResult(available=False, reason="The dialog returned an unreadable path.")
        return PickResult(path=Path(selected))

    if answer == "CANCELLED":
        return PickResult(cancelled=True)

    # No marker at all: the process died before it could answer — killed from Task
    # Manager, or the interpreter itself refused to start.
    detail = _clean_stderr(stderr)
    log.warning(
        "Folder dialog exited with %s and no answer: %s", process.returncode, detail or "(silent)"
    )
    return PickResult(
        available=False,
        reason=detail or "The folder dialog closed without returning a folder.",
    )


def _decode(payload: str) -> str:
    try:
        return base64.b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _clean_stderr(raw: bytes) -> str:
    """Windows PowerShell serialises its non-stdout streams as CLIXML once they are
    redirected, so those bytes are markup, not something to show anybody."""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("#< CLIXML"):
        return ""
    return " ".join(text.split())[:300]


# ---------------------------------------------------------------- Windows


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("powershell.exe") or shutil.which("pwsh")


def _ps_quote(value: str) -> str:
    """PowerShell single-quoted literal: nothing expands inside, `'` doubles to escape."""
    return "'" + value.replace("'", "''") + "'"


def _windows_script(title: str, start: Path | None) -> str:
    return f"""
$ErrorActionPreference = 'Stop'
# Progress records are what turn a redirected stderr into a CLIXML document; nothing
# here reports progress, so switching them off keeps stderr readable when it matters.
$ProgressPreference = 'SilentlyContinue'
try {{
  Add-Type -AssemblyName System.Windows.Forms | Out-Null
  Add-Type -AssemblyName System.Drawing | Out-Null

  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = {_ps_quote(title)}
  $dialog.ShowNewFolderButton = $true
  $dialog.RootFolder = [System.Environment+SpecialFolder]::MyComputer
  $start = {_ps_quote(str(start)) if start else "''"}
  if ($start -and (Test-Path -LiteralPath $start)) {{ $dialog.SelectedPath = $start }}

  # Spawned from a background process with no window of its own, the dialog would open
  # *behind* the browser and look to the user like nothing happened. A 1x1 topmost form
  # parked off-screen is its owner; it is never visible itself, and an owned window
  # inherits its owner's topmost flag. Off-screen is safe: with the owner nowhere near a
  # monitor the dialog centres itself on the primary screen instead.
  $owner = New-Object System.Windows.Forms.Form
  $owner.ShowInTaskbar = $false
  $owner.FormBorderStyle = 'None'
  $owner.StartPosition = 'Manual'
  $owner.Location = New-Object System.Drawing.Point(-4000, -4000)
  $owner.Size = New-Object System.Drawing.Size(1, 1)
  $owner.Show()

  # TopMost *after* Show(), and that ordering is the whole trick. Set on a form whose
  # handle does not exist yet it is only recorded on the form and never applied to the
  # window, so the owner came up non-topmost, the dialog inherited non-topmost, and it
  # opened behind the browser — the bug this reads like a workaround for. Assigning it to
  # a form that is already shown is what issues SetWindowPos(HWND_TOPMOST), which is also
  # not subject to the foreground-stealing rules that would block Activate() alone.
  $owner.TopMost = $true
  $owner.Activate()
  try {{
    $result = $dialog.ShowDialog($owner)
  }} finally {{
    $owner.Close()
    $owner.Dispose()
  }}

  if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $dialog.SelectedPath) {{
    # Base64 so the answer survives whatever code page the console happens to be in —
    # project paths are routinely non-ASCII, and a mangled one is an unopenable project.
    $bytes = [Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath)
    [Console]::Out.Write('PICKED:' + [Convert]::ToBase64String($bytes))
  }} else {{
    [Console]::Out.Write('CANCELLED')
  }}
}} catch {{
  # Answered on stdout, not raised: an unhandled error would reach the caller as CLIXML.
  $bytes = [Text.Encoding]::UTF8.GetBytes($_.Exception.Message)
  [Console]::Out.Write('ERROR:' + [Convert]::ToBase64String($bytes))
}}
"""


def _windows_command(title: str, start: Path | None) -> list[str]:
    encoded = base64.b64encode(_windows_script(title, start).encode("utf-16-le")).decode("ascii")
    return [
        _powershell() or "powershell",
        "-NoProfile",
        "-STA",  # WinForms dialogs require a single-threaded apartment.
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


# ---------------------------------------------------------------- everything else


_TK_SCRIPT = """\
import base64, sys, tkinter
from tkinter import filedialog

root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
selected = filedialog.askdirectory(title=sys.argv[1], initialdir=sys.argv[2] or None, mustexist=True)
root.destroy()
if selected:
    sys.stdout.write("PICKED:" + base64.b64encode(selected.encode("utf-8")).decode("ascii"))
else:
    sys.stdout.write("CANCELLED")
"""


def _tk_available() -> bool:
    from importlib.util import find_spec

    try:
        return find_spec("tkinter") is not None
    except (ImportError, ValueError):
        return False


def _tk_command(title: str, start: Path | None) -> list[str]:
    return [sys.executable, "-c", _TK_SCRIPT, title, str(start) if start else ""]
