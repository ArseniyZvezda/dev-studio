"""OpenSSH/Docker administration of the separate Remote server (dev spec §16).

No SSH library, local shell or password prompt. Explicit installation can bootstrap
Ubuntu/Debian using root or passwordless sudo. New host keys are enrolled on first
use; an existing host's changed key is never accepted automatically.
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import threading
from uuid import uuid4
from dataclasses import dataclass
from .admin_bundle import ssh_executable

from .admin_bootstrap import (
    CADDY_CONFIG, CADDY_CONTAINER, CADDY_DATA, CADDY_DIRECTORY,
    DOCKER_HELPER, bootstrap_script, port_check, tls_script, validate_domain,
)


LABEL = "io.voxarium.studio.remote"
IMAGE = "voxarium-studio-remote:admin"
CONTAINER = "voxarium-studio-remote"
VOLUME = "voxarium-studio-remote-data"
MAX_CONTEXT_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_INSPECT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"status":{{json .State.Status}},"image":{{json .Config.Image}},'
    '"labels":{{json .Config.Labels}}}'
)


class AdminError(RuntimeError):
    """An actionable connection, deployment or inspection failure."""


@dataclass
class Connection:
    host: str
    username: str
    key_path: str
    port: int = 22

    def validate(self) -> None:
        if not isinstance(self.host, str) or not self.host or "%" in self.host:
            raise ValueError("Enter a hostname or IP address, without a URL or username.")
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            if len(self.host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in self.host.removesuffix(".").split(".")
            ):
                raise ValueError("Invalid hostname or IP address.") from None
        if not isinstance(self.username, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?", self.username
        ):
            raise ValueError("Invalid SSH username.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be an integer between 1 and 65535.")
        if not isinstance(self.key_path, str) or not self.key_path or any(
            ord(char) < 32 or ord(char) == 127 for char in self.key_path
        ):
            raise ValueError("Choose an existing SSH private-key file.")
        try:
            if not Path(self.key_path).expanduser().is_file():
                raise ValueError("SSH private-key file does not exist.")
        except OSError as exc:
            raise ValueError("Cannot access the SSH private-key file.") from exc


@dataclass
class Installation:
    id: str
    name: str
    status: str
    image: str


def _source_archive(source: Path) -> bytes:
    """A small, explicit build context, never the operator's entire directory."""
    root = Path(source).expanduser().resolve()
    package = root / "voxarium_studio_remote"
    if not root.is_dir() or not package.is_dir():
        raise AdminError("Source must be the remote directory containing voxarium_studio_remote.")
    files = [root / name for name in ("Dockerfile", "pyproject.toml", "README.md")]
    if package.is_symlink() or package.resolve().parent != root:
        raise AdminError("Source package must not be a symlink or external directory.")
    for directory, dirs, names in os.walk(package, followlinks=False):
        # Hidden/cache/test directories cannot become part of the deployment context.
        dirs[:] = sorted(d for d in dirs if d.isidentifier() and d not in {"__pycache__", "tests"})
        for name in dirs:
            child = Path(directory) / name
            if child.is_symlink() or not child.resolve().is_relative_to(root):
                raise AdminError("Source context must not contain symlink directories.")
        files.extend(Path(directory) / name for name in sorted(names) if name.endswith(".py"))
    if package / "__init__.py" not in files:
        raise AdminError("Source package is missing __init__.py.")
    buffer = io.BytesIO()
    total = 0
    try:
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path in files:
                if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                    raise AdminError(f"Missing or unsafe source file: {path.relative_to(root)}")
                if path.stat().st_size > MAX_CONTEXT_BYTES - total:
                    raise AdminError("Source context exceeds the 16 MiB limit.")
                data = path.read_bytes()
                total += len(data)
                if total > MAX_CONTEXT_BYTES:
                    raise AdminError("Source context exceeds the 16 MiB limit.")
                info = tarfile.TarInfo(path.relative_to(root).as_posix())
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    except OSError as exc:
        raise AdminError(f"Cannot read source context: {exc}") from exc
    return buffer.getvalue()


class RemoteAdmin:
    def __init__(self, connection: Connection):
        connection.validate()
        self.connection = connection

    def _ssh_argv(self, args: list[str]) -> list[str]:
        self.connection.validate()
        c = self.connection
        return [ssh_executable(), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", "-p", str(c.port),
                "-i", str(Path(c.key_path).expanduser().resolve()), "-l", c.username, c.host, shlex.join(args)]

    def _run(self, args: list[str], *, data: bytes | None = None, timeout: int = 45) -> str:
        self.connection.validate()
        connection = self.connection
        try:
            ssh = ssh_executable()
        except RuntimeError as exc:
            raise AdminError(str(exc)) from exc
        command = [
            # OpenSSH persists new hosts (including non-default ports) in its
            # normal known_hosts file, but still refuses changed/revoked keys.
            # Never retry with checking disabled or delete an existing key.
            ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2", "-p", str(connection.port),
            "-i", str(Path(connection.key_path).expanduser().resolve()),
            "-l", connection.username, connection.host, shlex.join(args),
        ]
        try:
            result = subprocess.run(
                command, input=data or b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdminError(
                "SSH operation timed out. Reconnect and discover before retrying installation; "
                "a remote operation may still be finishing."
            ) from exc
        except OSError as exc:
            raise AdminError(f"Cannot run OpenSSH (ssh): {exc}") from exc
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-2000:]
            raise AdminError(
                f"Remote command failed (exit {result.returncode}): {detail or 'no diagnostic'}"
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def test_connection(self) -> None:
        """Verify SSH authentication/remote execution without requiring Docker."""
        self._run(["true"])

    def _docker(self, args: list[str]) -> str:
        """Read-only operations retry only Docker socket permission failures.

        SSH authentication/host-key errors must never trigger a retry. Discovery
        must not install packages, start daemons, or change socket permissions.
        """
        try:
            return self._run(["docker", *args])
        except AdminError as exc:
            diagnostic = str(exc).lower()
            if "exit 127" in diagnostic and "docker" in diagnostic:
                raise AdminError("Docker is not installed. Choose Install to provision Ubuntu/Debian; discovery made no changes.") from exc
            if ("permission denied" not in diagnostic or "exit 255" in diagnostic
                    or not any(word in diagnostic for word in ("docker daemon", "docker.sock", "unix://"))):
                raise
            try:
                return self._run(["sudo", "-n", "--", "docker", *args])
            except AdminError as elevated:
                raise AdminError(
                    "Docker access requires root or passwordless sudo -n. No groups/socket permissions were changed. "
                    + str(elevated)
                ) from elevated

    def discover(self) -> list[Installation]:
        """Inspect every container, including stopped ones and Compose deployments."""
        ids = self._docker(["ps", "-a", "--no-trunc", "--format", "{{.ID}}"])
        if not ids:
            return []
        identifiers = ids.splitlines()
        if not all(_ID.fullmatch(value) for value in identifiers):
            raise AdminError("Docker returned malformed container IDs.")
        found = []
        # Bound argv length on hosts with many unrelated containers.
        for offset in range(0, len(identifiers), 100):
            output = self._docker(["inspect", "--format", _INSPECT, *identifiers[offset:offset + 100]])
            try:
                for line in output.splitlines():
                    item = json.loads(line)
                    image = item["image"]
                    labels = item.get("labels") or {}
                    # Compose's generated container name is deliberately irrelevant.
                    # A service called 'remote' alone is not evidence of this product.
                    image_name = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
                    if image_name != "voxarium-studio-remote" and labels.get(LABEL) != "true":
                        continue
                    if not _ID.fullmatch(item["id"]) or not all(
                        isinstance(item[key], str) for key in ("name", "status", "image")
                    ):
                        raise ValueError("invalid installation")
                    found.append(Installation(item["id"], item["name"].lstrip("/"), item["status"], image))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise AdminError("Docker returned malformed inspection data.") from exc
        return sorted(found, key=lambda installation: installation.name)

    @staticmethod
    def _container(installation: Installation) -> str:
        if not isinstance(installation.id, str) or not _ID.fullmatch(installation.id):
            raise AdminError("Invalid container ID; discover installations again.")
        return installation.id

    def install(self, source: Path, domain: str | None = None) -> str:
        domain = validate_domain(domain)
        archive = _source_archive(source)
        # Separate SSH call: apt/sudo must never consume the tar upload on stdin.
        self._run(["sh", "-c", bootstrap_script(domain)], timeout=1050)
        if self.discover():
            raise AdminError("Remote is already installed (possibly stopped); no containers or data were replaced.")
        # Only this invocation's mktemp directory is removed. Failed containers and
        # volumes are retained for diagnosis; never delete existing user data (§16.4).
        health = """import json, time, urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3) as response:
            result = json.load(response)
        if result.get('status') == 'ok' and result.get('service') == 'voxarium_studio_remote':
            print('Remote health check passed')
            break
    except (OSError, ValueError):
        pass
    time.sleep(1)
else:
    raise SystemExit('Remote did not become healthy; container and data preserved')
"""
        script = "\n".join([
            "set -eu", "umask 077", DOCKER_HELPER,
            port_check(domain),
            "names=$(docker_cmd ps -a --format '{{.Names}}')",
            *[f"if printf '%s\\n' \"$names\" | grep -Fx {shlex.quote(name)}; then fail 'Existing container found; nothing replaced.'; fi"
              for name in ([CONTAINER, CADDY_CONTAINER] if domain else [CONTAINER])],
            "volumes=$(docker_cmd volume ls --format '{{.Name}}')",
            *[f"if printf '%s\\n' \"$volumes\" | grep -Fx {shlex.quote(volume)}; then fail 'Existing data volume found; refusing to reuse or replace it.'; fi"
              for volume in ([VOLUME, CADDY_DATA, CADDY_CONFIG] if domain else [VOLUME])],
            *([f"if run_root test -e {CADDY_DIRECTORY} || run_root test -L {CADDY_DIRECTORY}; then fail 'Existing Caddy configuration found; nothing replaced.'; fi"] if domain else []),
            "images=$(docker_cmd image ls --format '{{.Repository}}:{{.Tag}}')",
            f"if printf '%s\\n' \"$images\" | grep -Fx {shlex.quote(IMAGE)}; then fail 'Existing Remote image found; refusing to overwrite it. Inspect the previous installation.'; fi",
            'work=$(mktemp -d /tmp/voxarium-remote-admin.XXXXXXXXXX)',
            'case "$work" in /tmp/voxarium-remote-admin.*) ;; *) exit 1 ;; esac',
            "trap 'rm -rf -- \"$work\"' EXIT",
            "trap 'exit 1' HUP INT TERM",
            'tar -xf - -C "$work"',
            f'docker_cmd build --tag {shlex.quote(IMAGE)} "$work"',
            shlex.join([
                "docker_cmd", "run", "--detach", "--name", CONTAINER,
                "--label", f"{LABEL}=true", "--restart", "unless-stopped",
                # Host networking avoids pre-Docker-28 localhost publishing leaks.
                "--network", "host", "--volume", f"{VOLUME}:/data", IMAGE,
                "voxarium-studio-remote", "serve", "--host", "127.0.0.1", "--port", "8787",
            ]),
            shlex.join(["docker_cmd", "exec", CONTAINER, "python", "-c", health]),
            tls_script(domain) if domain else "",
        ])
        try:
            output = self._run(
                ["timeout", "--signal=TERM", "--kill-after=10", "900", "sh", "-c", script],
                data=archive, timeout=930,
            )
        except AdminError as exc:
            raise AdminError(
                "Deployment did not complete. Packages/services may be configured; any created containers, "
                "volumes and TLS configuration were preserved. Discover/inspect before retrying. " + str(exc)
            ) from exc
        if domain:
            return output + f"\nHTTPS: https://{domain}. Keep DNS A/AAAA and external/cloud firewall ports 80/443 configured."
        return output + (
            "\nInstalled on 127.0.0.1:8787 only. Use an SSH tunnel (-L 8787:127.0.0.1:8787) "
            "or configure a TLS reverse proxy before external access; no firewall ports were opened."
        )

    def telemetry(self, installation: Installation) -> str:
        container = self._container(installation)
        disk = self._run(["df", "-h", "/"])
        memory = self._run(["cat", "/proc/meminfo"])
        try:
            fields = {line.split(":", 1)[0]: int(line.split()[1]) for line in memory.splitlines() if ":" in line}
            total, available = fields["MemTotal"], fields["MemAvailable"]
            if not 0 <= available <= total or total <= 0:
                raise ValueError("invalid memory counters")
            ram = f"used {total - available:,} / total {total:,} KiB; available {available:,} KiB (used = total - available)"
        except (KeyError, ValueError, IndexError) as exc:
            raise AdminError("Cannot read host RAM: /proc/meminfo must contain MemTotal and MemAvailable.") from exc
        sections = [
            "Host filesystem usage (root filesystem, not container data):\n" + disk,
            "Host RAM:\n" + ram,
        ]
        if installation.status != "running":
            sections.append("Container is stopped: RAM and /data usage unavailable; it was not started.")
        else:
            sections.extend([
                "Container RAM (usage / limit):\n" + self._docker([
                    "stats", "--no-stream", "--format", "{{.MemUsage}}", container,
                ]),
                "Container data disk usage (/data, KiB; not image size):\n" + self._docker([
                    "exec", container, "du", "-sk", "/data",
                ]),
            ])
        return "\n\n".join(sections)

    def _bounded_ssh(self, args: list[str], data: bytes, *, timeout: int,
                     stdout_limit: int, stderr_limit: int) -> tuple[int, bytes, bytes]:
        """Bound both pipes while running, not only after communicate allocated them.

        No exception chaining: subprocess exceptions contain stdout/stderr/argv.
        This path never sends remote diagnostic text to the UI or a logger.
        """
        proc = None
        readers = []
        output = bytearray()
        diagnostic = bytearray()
        overflow = threading.Event()

        def drain(stream, limit, target):
            count = 0
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk: break
                    count += len(chunk)
                    if count > limit:
                        overflow.set()
                        proc.kill()
                        break
                    target.extend(chunk)
            except (OSError, ValueError):
                overflow.set()
            finally:
                stream.close()

        try:
            command = self._ssh_argv(args)
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, shell=False)
            for stream, limit, target in ((proc.stdout, stdout_limit, output), (proc.stderr, stderr_limit, diagnostic)):
                reader = threading.Thread(target=drain, args=(stream, limit, target), daemon=True)
                readers.append(reader)
                reader.start()
            def send_input():
                try:
                    proc.stdin.write(data)
                    proc.stdin.close()
                except (OSError, ValueError):
                    overflow.set()
            writer = threading.Thread(target=send_input, daemon=True)
            readers.append(writer)
            writer.start()
            code = proc.wait(timeout=timeout)
            for reader in readers: reader.join(timeout=2)
            if overflow.is_set() or any(reader.is_alive() for reader in readers):
                raise RPCFailure("unknown_outcome", unknown=True)
            return code, bytes(output), bytes(diagnostic)
        except (KeyboardInterrupt, EOFError):
            raise RPCFailure("unknown_outcome", unknown=True) from None
        except Exception:
            raise RPCFailure("unknown_outcome", unknown=True) from None
        finally:
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)
                    for reader in readers: reader.join(timeout=2)
                except Exception:
                    # Never let cleanup replace a redacted error with OS output.
                    pass

    def _rpc_run(self, args: list[str], data: bytes, timeout: int = 45) -> bytes:
        from .admin_rpc import MAX_RESPONSE_BYTES
        code, output, _ = self._bounded_ssh(args, data, timeout=timeout,
                                            stdout_limit=MAX_RESPONSE_BYTES, stderr_limit=16 * 1024)
        if code:
            raise RPCFailure("unknown_outcome", unknown=True)
        return output

    def _rpc_probe(self, args: list[str]) -> str:
        """Read-only probe classification; bounded diagnostics NEVER leave here."""
        try:
            code, output, diagnostic = self._bounded_ssh(
                args, b"", timeout=15, stdout_limit=64, stderr_limit=4096)
        except RPCFailure:
            return "unavailable"
        if code == 0:
            # Docker appends one line ending. No arbitrary banners/whitespace,
            # stopped-state 'false', or partial responses qualify as a probe.
            return "available" if output in (b"true", b"true\n", b"true\r\n") else "unavailable"
        diagnostic = diagnostic.lower()
        if code != 255 and b"permission denied" in diagnostic and any(
            marker in diagnostic for marker in (b"docker.sock", b"docker daemon", b"unix://")
        ):
            return "permission_denied"
        return "unavailable"

    def _rpc_prefix(self, installation: Installation) -> list[str]:
        container = self._container(installation)
        if installation.status != "running": raise RPCUnavailable("Container is stopped.")
        cache = getattr(self, "_rpc_modes", {})
        if container not in cache:
            # Bounded read-only probe BEFORE any RPC, never retry the mutation.
            inspect = ["inspect", "--format", "{{.State.Running}}", container]
            mode = self._rpc_probe(["docker", *inspect])
            if mode == "available":
                cache[container] = ["docker"]
            elif mode == "permission_denied":
                if self._rpc_probe(["sudo", "-n", "--", "docker", *inspect]) != "available":
                    raise RPCUnavailable("RPC requires root or passwordless sudo -n.") from None
                cache[container] = ["sudo", "-n", "--", "docker"]
            else:
                raise RPCUnavailable("RPC unavailable. Check SSH/Docker; update the server if required.") from None
            self._rpc_modes = cache
        return [*cache[container], "exec", "-i", container, "voxarium-studio-remote", "admin-rpc"]

    def rpc(self, installation: Installation, operation: str, payload: dict | None = None, *, request_id: str | None = None) -> dict:
        from .admin_rpc import parse_request, validate_response
        request_id = uuid4().hex if request_id is None else request_id
        envelope = {"admin_rpc_version": 1, "request_id": request_id, "operation": operation, "payload": payload if payload is not None else {}}
        try:
            data = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            parse_request(data)
        except Exception:
            raise RPCFailure("invalid_request") from None
        args = self._rpc_prefix(installation)
        try:
            raw = self._rpc_run(args, data)
            response = validate_response(raw, request_id, operation)
            if "result" in response:
                result = response["result"]
                if "user" in result and result["user"]["id"] != payload["user_id"]:
                    raise ValueError()
                if "project" in result and result["project"]["id"] != payload["project_id"]:
                    raise ValueError()
                if operation == "users.create" and (result["user"]["credential_version"] != 1 or result["user"]["metadata_revision"] != 1):
                    raise ValueError()
                if operation == "users.rotate" and (result["user"]["credential_version"] != payload["expected_credential_version"] + 1 or result["user"]["metadata_revision"] != payload["expected_metadata_revision"] + 1):
                    raise ValueError()
                if operation in ("users.create", "users.rotate") and result["user"]["status"] != "active":
                    raise ValueError()
        except Exception:
            if operation == "capabilities":
                raise RPCUnavailable("RPC is absent or incompatible; update the server. Read-only fallback only.") from None
            raise RPCFailure("unknown_outcome", unknown=True) from None
        if "error" in response:
            code = response["error"]["code"]
            if code in ("unsupported_schema", "unsupported_version"):
                raise RPCUnavailable("Server update or offline migration required; read-only fallback only.")
            raise RPCFailure(code)
        return response["result"]

    def users(self, installation: Installation) -> str:
        container = self._container(installation)
        if installation.status != "running":
            raise AdminError("Container is stopped. Start it manually to list users; no changes were made.")
        # The existing CLI prints metadata only, never tokens or stored token hashes.
        return self._docker(["exec", container, "voxarium-studio-remote", "user", "list"])


class RPCUnavailable(AdminError):
    """RPC is absent/incompatible: legacy read-only display is the only fallback."""


class RPCFailure(AdminError):
    def __init__(self, code: str, *, unknown: bool = False):
        from .admin_rpc import ERROR_MESSAGES
        self.code = code
        self.unknown = unknown
        super().__init__(ERROR_MESSAGES.get(code, "RPC result unknown. Read the original UUID before another explicit action."))
