"""Starts the Studio again once it has stopped, from outside its process tree (§12.1).

The last step of an update is a restart, and the process that has to survive it is the one
the Studio itself spawned. Two things would otherwise kill it:

* ``run.ps1`` restarts the Studio by walking the recorded backend's **process tree** and
  killing it children-first. A relauncher spawned by the backend is one of those children.
* Whatever is left of the backend still holds the port the new one has to bind.

So this module runs twice. The first copy — a child of the backend — immediately spawns a
second, detached copy of itself and exits, which orphans the second one: its parent is a
dead pid, so no tree walk can reach it. The second copy waits for the old server to let go
of the port and then runs the launch command.

Deliberately **stdlib only, and never imported by the running app**. It is executed by
path (``python .../relauncher.py <payload.json>``) *after* the pull that replaced the
source tree, so it must not depend on the package importing cleanly — an update that adds
a dependency would otherwise leave the Studio stopped with nothing able to start it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DETACH_FLAG = "--detached"

PORT_FREE_TIMEOUT = 90.0
"""Seconds to wait for the old server to release the port before starting anyway.

Starting anyway is the right end to this wait: on Windows the launch command is
``run.ps1``, which kills whatever still holds the port, so a hung old process is a reason
to hand over sooner rather than to give up on the restart entirely.
"""

SETTLE_SECONDS = 2.0
"""Grace after the port closes. uvicorn stops listening at the *start* of its shutdown,
so the old process is still closing the database for a moment after the socket is gone."""

POLL_SECONDS = 0.5


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: relauncher.py <payload.json> [--detached]", file=sys.stderr)
        return 2

    payload_path = Path(argv[0])
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"unreadable relaunch payload {payload_path}: {exc}", file=sys.stderr)
        return 2

    if DETACH_FLAG not in argv[1:]:
        return _respawn_detached(payload_path, payload)

    delay = float(payload.get("delay", 0.0))
    if delay > 0:
        time.sleep(delay)

    host = str(payload.get("host") or "127.0.0.1")
    port = int(payload.get("port") or 0)
    if port:
        if _wait_for_port(host, port):
            print(f"port {host}:{port} is free")
        else:
            print(f"port {host}:{port} is still held after {PORT_FREE_TIMEOUT}s — starting anyway")
        time.sleep(SETTLE_SECONDS)

    return _launch(payload)


# ---------------------------------------------------------------- the two hops


def _respawn_detached(payload_path: Path, payload: dict) -> int:
    """Hop out of the backend's process tree, then leave immediately.

    Exiting is the load-bearing half: it is what makes the second copy an orphan, and an
    orphan is what ``run.ps1``'s children-first kill cannot find.
    """
    command = [sys.executable, str(Path(__file__).resolve()), str(payload_path), DETACH_FLAG]
    try:
        _spawn(command, cwd=payload.get("cwd"), log=payload.get("log"), detach=True)
    except OSError as exc:
        print(f"could not spawn the detached relauncher: {exc}", file=sys.stderr)
        return 1
    return 0


def _launch(payload: dict) -> int:
    """Run the launch command. Waits for it only when it is one that comes back."""
    command = [str(part) for part in payload.get("command") or []]
    if not command:
        print("nothing to launch — the payload carries no command", file=sys.stderr)
        return 2

    print(f"launching: {' '.join(command)}")
    try:
        process = _spawn(
            command,
            cwd=payload.get("cwd"),
            log=payload.get("log"),
            detach=not payload.get("wait"),
        )
    except OSError as exc:
        print(f"could not start the Studio: {exc}", file=sys.stderr)
        return 1

    # `run.ps1` starts the server detached and returns, so its exit code says whether the
    # Studio came back up — worth waiting for and recording. A bare `voxarium_studio run` never
    # returns, and waiting on it would keep this process alive for the whole session.
    if payload.get("wait"):
        code = process.wait()
        print(f"launcher exited with {code}")
        return code
    return 0


def _spawn(
    command: list[str], *, cwd: str | None, log: str | None, detach: bool
) -> subprocess.Popen:
    """Start a process that outlives this one, with its output appended to ``log``."""
    creationflags = 0
    start_new_session = False
    if detach:
        if os.name == "nt":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            if os.environ.get("VOXARIUM_STUDIO_DESKTOP_JOB") == "1":
                # Only the restart handoff may outlive the desktop backend's job (§12.1).
                creationflags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
        else:
            start_new_session = True

    stream = None
    if log:
        # Appended, never truncated: this file is the only record of a restart, and the
        # two hops both write to it.
        stream = open(log, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    try:
        return subprocess.Popen(
            command,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=stream or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if stream else subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    finally:
        # The child holds its own duplicate of the handle; ours would only pin the file.
        if stream is not None:
            stream.close()


# ---------------------------------------------------------------- waiting for the port


def _wait_for_port(host: str, port: int, timeout: float = PORT_FREE_TIMEOUT) -> bool:
    """Block until nothing is listening on the port. False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_listening(host, port):
            return True
        time.sleep(POLL_SECONDS)
    return not _is_listening(host, port)


def _is_listening(host: str, port: int) -> bool:
    # A bind address is not always a connectable one — `0.0.0.0` names every interface, so
    # the question "has the old server let go" is asked on the loopback it also covers.
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            return sock.connect_ex((target, port)) == 0
        except OSError:
            return False


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main(sys.argv[1:]))
