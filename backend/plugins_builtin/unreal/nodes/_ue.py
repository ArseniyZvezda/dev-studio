"""Getting Python into an Unreal editor, and the result back out.

``unreal`` is only importable inside the editor process, so a node cannot call the API
directly — it has to ship a script across. Two routes, both official:

* **Remote Execution.** Unreal's Python plugin announces itself on a UDP multicast group;
  a client pings, picks an editor, asks it to open a TCP connection back, and sends
  commands over that. Keeps the artist's editor open and costs no start-up. Off by
  default in Unreal — Project Settings -> Plugins -> Python -> Enable Remote Execution.
* **Commandlet.** ``UnrealEditor-Cmd <project> -run=pythonscript -script=<file>`` boots a
  headless editor, runs the script and exits. Slower, but needs nothing running, which is
  what a CI machine has.

Results come back the same way for both: the script prints one ``::voxarium_studio:ue::{...}``
line, which is scraped out of the editor's log. A shared file would have been simpler but
would tie the two machines together, and Remote Execution is explicitly allowed to be
talking to another host.

Stdlib only — this runs in the Studio's own interpreter, not in Unreal's.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

RESULT_MARKER = "::voxarium_studio:ue::"

PROTOCOL_VERSION = 1
PROTOCOL_MAGIC = "ue_py"

TYPE_PING = "ping"
TYPE_PONG = "pong"
TYPE_OPEN_CONNECTION = "open_connection"
TYPE_CLOSE_CONNECTION = "close_connection"
TYPE_COMMAND = "command"
TYPE_COMMAND_RESULT = "command_result"

MODE_EXECUTE_FILE = "ExecuteFile"

DEFAULT_COMMANDLET_ARGS = ["-run=pythonscript", "-script={script}", "-unattended", "-nopause", "-nosplash"]


class UnrealError(RuntimeError):
    """Anything that stops a script reaching the editor, or that the editor rejected."""


@dataclass
class UnrealSettings:
    """Resolved from the plugin's settings page, with environment fallbacks."""

    project: str = ""
    """Absolute path to the .uproject."""

    editor_cmd: str = ""
    """Absolute path to UnrealEditor-Cmd(.exe). Only needed for the headless route."""

    transport: str = "auto"
    multicast_group: str = "239.0.0.1"
    multicast_port: int = 6766
    multicast_bind_address: str = "0.0.0.0"
    multicast_ttl: int = 0
    command_host: str = "127.0.0.1"
    command_port: int = 6776
    discovery_timeout: float = 2.0
    commandlet_args: list[str] = field(default_factory=lambda: list(DEFAULT_COMMANDLET_ARGS))

    @classmethod
    def from_payload(cls, settings: dict[str, Any], overrides: dict[str, Any]) -> "UnrealSettings":
        def pick(key: str, default: Any) -> Any:
            for source in (overrides, settings):
                value = source.get(key)
                if value not in (None, ""):
                    return value
            return default

        args = pick("commandlet_args", None)
        if isinstance(args, str):
            args = args.split()

        return cls(
            project=str(pick("project", os.environ.get("VOXARIUM_STUDIO_UNREAL_PROJECT", ""))),
            editor_cmd=str(pick("editor_cmd", os.environ.get("VOXARIUM_STUDIO_UNREAL_EDITOR", ""))),
            transport=str(pick("transport", "auto")),
            multicast_group=str(pick("multicast_group", "239.0.0.1")),
            multicast_port=int(pick("multicast_port", 6766)),
            multicast_bind_address=str(pick("multicast_bind_address", "0.0.0.0")),
            multicast_ttl=int(pick("multicast_ttl", 0)),
            command_host=str(pick("command_host", "127.0.0.1")),
            command_port=int(pick("command_port", 6776)),
            discovery_timeout=float(pick("discovery_timeout", 2.0)),
            commandlet_args=list(args or DEFAULT_COMMANDLET_ARGS),
        )


# --------------------------------------------------------------------------- protocol


def _encode(source: str, kind: str, *, dest: str | None = None, data: Any = None) -> bytes:
    message: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "magic": PROTOCOL_MAGIC,
        "source": source,
        "type": kind,
    }
    if dest is not None:
        message["dest"] = dest
    if data is not None:
        message["data"] = data
    return json.dumps(message).encode("utf-8")


def _decode(raw: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict) or message.get("magic") != PROTOCOL_MAGIC:
        return None
    return message


def _multicast_socket(settings: UnrealSettings) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((settings.multicast_bind_address, settings.multicast_port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, settings.multicast_ttl)
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        struct.pack("4s4s",
                    socket.inet_aton(settings.multicast_group),
                    socket.inet_aton(settings.multicast_bind_address)),
    )
    sock.settimeout(0.25)
    return sock


def discover_editors(settings: UnrealSettings) -> list[dict[str, Any]]:
    """Ping the multicast group and collect whoever answers.

    Each entry is the editor's ``pong`` payload — engine version, project name and so on
    — plus the ``node_id`` needed to address it.
    """
    source = str(uuid.uuid4())
    found: dict[str, dict[str, Any]] = {}
    try:
        sock = _multicast_socket(settings)
    except OSError as exc:
        raise UnrealError(f"Could not open the discovery socket: {exc}") from exc

    try:
        sock.sendto(_encode(source, TYPE_PING), (settings.multicast_group, settings.multicast_port))
        deadline = time.monotonic() + settings.discovery_timeout
        while time.monotonic() < deadline:
            try:
                raw, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            message = _decode(raw)
            if message is None or message.get("type") != TYPE_PONG:
                continue
            node_id = message.get("source")
            if node_id and node_id != source:
                found[node_id] = {"node_id": node_id, **(message.get("data") or {})}
    finally:
        sock.close()
    return list(found.values())


class _CommandConnection:
    """One editor connection: we listen, it dials back, commands go over that socket."""

    def __init__(self, settings: UnrealSettings, node_id: str) -> None:
        self.settings = settings
        self.node_id = node_id
        self.source = str(uuid.uuid4())
        self._listener: socket.socket | None = None
        self._command: socket.socket | None = None

    def __enter__(self) -> "_CommandConnection":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.settings.command_host, self.settings.command_port))
        except OSError as exc:
            listener.close()
            raise UnrealError(
                f"Could not listen on {self.settings.command_host}:{self.settings.command_port} "
                f"for the editor to connect back: {exc}"
            ) from exc
        listener.listen(1)
        listener.settimeout(self.settings.discovery_timeout + 3.0)
        self._listener = listener

        multicast = _multicast_socket(self.settings)
        try:
            multicast.sendto(
                _encode(
                    self.source,
                    TYPE_OPEN_CONNECTION,
                    dest=self.node_id,
                    data={
                        "command_ip": self.settings.command_host,
                        "command_port": self.settings.command_port,
                    },
                ),
                (self.settings.multicast_group, self.settings.multicast_port),
            )
        finally:
            multicast.close()

        try:
            self._command, _ = listener.accept()
        except socket.timeout as exc:
            self.__exit__(None, None, None)
            raise UnrealError(
                "The editor was discovered but never opened a command connection. "
                "Check that command_host is an address it can reach."
            ) from exc
        self._command.settimeout(None)
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._command is not None:
            try:
                self._command.close()
            finally:
                self._command = None
        if self._listener is not None:
            try:
                self._listener.close()
            finally:
                self._listener = None
        multicast = None
        try:
            multicast = _multicast_socket(self.settings)
            multicast.sendto(
                _encode(self.source, TYPE_CLOSE_CONNECTION, dest=self.node_id),
                (self.settings.multicast_group, self.settings.multicast_port),
            )
        except OSError:
            pass
        finally:
            if multicast is not None:
                multicast.close()

    def run(self, script_path: str, timeout: float) -> dict[str, Any]:
        assert self._command is not None
        self._command.sendall(
            _encode(
                self.source,
                TYPE_COMMAND,
                dest=self.node_id,
                data={
                    "command": script_path,
                    "unattended": True,
                    "exec_mode": MODE_EXECUTE_FILE,
                },
            )
        )

        # The reply carries the whole log, so it routinely exceeds one recv.
        self._command.settimeout(timeout)
        buffer = b""
        while True:
            try:
                chunk = self._command.recv(65536)
            except socket.timeout as exc:
                raise UnrealError(f"The editor did not answer within {timeout}s") from exc
            if not chunk:
                raise UnrealError("The editor closed the connection before replying")
            buffer += chunk
            message = _decode(buffer)
            if message is not None and message.get("type") == TYPE_COMMAND_RESULT:
                return message.get("data") or {}


def _run_remote(
    settings: UnrealSettings, script_path: Path, timeout: float, log: Callable[[str], None]
) -> tuple[list[str], bool]:
    editors = discover_editors(settings)
    if not editors:
        raise UnrealError(
            "No Unreal editor answered on "
            f"{settings.multicast_group}:{settings.multicast_port}. Is one running with "
            "Remote Execution enabled (Project Settings -> Plugins -> Python)?"
        )

    editor = editors[0]
    log(
        f"editor: {editor.get('project_name') or 'unknown project'} "
        f"({editor.get('engine_version') or 'unknown engine'}) on {editor.get('machine') or '?'}"
    )
    if len(editors) > 1:
        log(f"note: {len(editors)} editors answered; using the first")

    with _CommandConnection(settings, editor["node_id"]) as connection:
        result = connection.run(str(script_path), timeout)

    lines = [
        str(entry.get("output", "")).rstrip()
        for entry in (result.get("output") or [])
        if isinstance(entry, dict)
    ]
    return lines, bool(result.get("success"))


def _run_commandlet(
    settings: UnrealSettings, script_path: Path, timeout: float, log: Callable[[str], None]
) -> tuple[list[str], bool]:
    if not settings.editor_cmd:
        raise UnrealError(
            "No editor executable configured. Set it on the Unreal Engine settings page "
            "(UnrealEditor-Cmd.exe on Windows, UnrealEditor-Cmd on Linux/macOS)."
        )
    if not Path(settings.editor_cmd).exists():
        raise UnrealError(f"Editor executable not found: {settings.editor_cmd}")
    if not settings.project:
        raise UnrealError("No .uproject configured on the Unreal Engine settings page.")
    if not Path(settings.project).exists():
        raise UnrealError(f"Project not found: {settings.project}")

    command = [settings.editor_cmd, str(Path(settings.project).resolve())]
    for argument in settings.commandlet_args:
        command.append(
            argument.replace("{script}", str(script_path)).replace("{project}", settings.project)
        )

    log(f"running: {' '.join(command)}")
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnrealError(f"The headless editor did not finish within {timeout}s") from exc
    except OSError as exc:
        raise UnrealError(f"Could not launch the editor: {exc}") from exc

    lines = (process.stdout or "").splitlines() + (process.stderr or "").splitlines()
    if process.returncode != 0:
        tail = "\n".join(lines[-20:]) or "(no output)"
        raise UnrealError(f"The editor exited with code {process.returncode}:\n{tail}")
    return lines, True


def run_script(
    settings: UnrealSettings,
    script: str,
    *,
    work_dir: Path,
    timeout: float,
    log: Callable[[str], None],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Deliver a script to the editor and return (parsed result, log lines).

    The result is whatever the script printed after ``::voxarium_studio:ue::``; ``None`` means it
    printed nothing, which the caller decides how to treat.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / "unreal_command.py"
    script_path.write_text(script, encoding="utf-8")

    transport = settings.transport if settings.transport in ("auto", "remote", "commandlet") else "auto"

    if transport == "commandlet":
        lines, _ = _run_commandlet(settings, script_path, timeout, log)
    elif transport == "remote":
        lines, success = _run_remote(settings, script_path, timeout, log)
        if not success:
            raise UnrealError(_editor_failure(lines))
    else:
        try:
            log("looking for a running editor…")
            lines, success = _run_remote(settings, script_path, timeout, log)
            if not success:
                raise UnrealError(_editor_failure(lines))
        except UnrealError as exc:
            log(f"remote execution unavailable ({exc}); falling back to a headless run")
            lines, _ = _run_commandlet(settings, script_path, timeout, log)

    for line in lines:
        log(line)

    return _extract_result(lines), lines


def _editor_failure(lines: list[str]) -> str:
    errors = [line for line in lines if "Error" in line or "Traceback" in line]
    tail = "\n".join((errors or lines)[-15:]) or "(no output)"
    return f"The editor reported the script as failed:\n{tail}"


def _extract_result(lines: list[str]) -> dict[str, Any] | None:
    """Last marker wins — a re-run inside one editor session appends rather than replaces."""
    for line in reversed(lines):
        index = line.find(RESULT_MARKER)
        if index == -1:
            continue
        try:
            payload = json.loads(line[index + len(RESULT_MARKER):].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def emit_marker(payload: dict[str, Any]) -> str:
    """Build the line an editor-side script prints to hand structured data back."""
    return RESULT_MARKER + json.dumps(payload, default=str)


__all__ = [
    "RESULT_MARKER",
    "UnrealError",
    "UnrealSettings",
    "discover_editors",
    "emit_marker",
    "run_script",
]
