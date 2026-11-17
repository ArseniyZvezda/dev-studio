"""Locate the administrator's embedded deployment resources (dev spec §16)."""

from pathlib import Path
import hashlib
import json
import sys


# The deployment context is a separate resource from the frozen Admin runtime.
# Both staging and resolution must require a COMPLETE server, not just metadata.
REQUIRED_REMOTE_FILES = (
    "Dockerfile", "pyproject.toml", "README.md",
    *(f"voxarium_studio_remote/{name}.py" for name in (
        "__init__", "admin", "admin_bootstrap", "admin_bundle", "admin_rpc",
        "admin_secrets", "admin_transport", "admin_ui", "app", "auth", "cli",
        "hub", "migrations", "protocol", "store",
    )),
)


def _assets() -> Path:
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        raise RuntimeError("Embedded administrator resources are unavailable. Rebuild the EXE.")
    return Path(root) / "admin_assets"


def verify_assets() -> dict:
    """Fail closed on an incomplete/tampered embedded candidate, not a release gate."""
    root = _assets()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("format") != 1 or manifest.get("wire_protocol") != 2
                or manifest.get("schema_version") != 2 or manifest.get("admin_rpc_version") != 1
                or not isinstance(manifest.get("files"), dict)):
            raise ValueError("Invalid manifest")
        required = {f"remote/{name}" for name in REQUIRED_REMOTE_FILES} | {"openssh/ssh.exe"}
        if not required <= manifest["files"].keys():
            raise ValueError("Incomplete deployment context")
        for name, digest in manifest["files"].items():
            path = root / name
            if (not isinstance(name, str) or "\\" in name or
                    not path.resolve().is_relative_to(root.resolve()) or
                    not path.is_file() or path.is_symlink() or
                    hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                raise ValueError("Invalid resource")
        return manifest
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError("Embedded resources failed verification. Rebuild the EXE.") from None


def default_source() -> Path:
    """Return the Remote build context, independent of the working directory."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[1]
    verify_assets()
    source = _assets() / "remote"
    for name in REQUIRED_REMOTE_FILES:
        if not (source / name).is_file():
            raise RuntimeError(f"Embedded Remote source is missing {name}. Rebuild the EXE.")
    return source


def ssh_executable() -> str:
    """Frozen builds never silently depend on an external SSH installation."""
    if not getattr(sys, "frozen", False):
        return "ssh"
    verify_assets()
    ssh = _assets() / "openssh" / "ssh.exe"
    if not ssh.is_file():
        raise RuntimeError("Embedded OpenSSH is missing. Rebuild the EXE with Windows OpenSSH installed.")
    return str(ssh)


def offline_runtime_check() -> dict:
    """Exercise packaged RPC/secret code with an in-memory fake SSH hop, never a server.

    The disposable key stays inside this function. No real clipboard, profile, file,
    subprocess or connection is used; only the fixed version result is returned.
    """
    from uuid import uuid4
    from .admin_rpc import dispatch, parse_request
    from .admin_transport import Installation, RemoteAdmin
    from .admin_secrets import EphemeralSecret
    from .store import Store

    store = Store(":memory:")
    admin = object.__new__(RemoteAdmin)
    admin._rpc_prefix = lambda _: ["in-memory-fixture"]

    def fake_hop(_args, data):
        request = parse_request(data)
        result = dispatch(store, request["operation"], request["payload"])
        return json.dumps({"admin_rpc_version": 1, "request_id": request["request_id"],
                           "result": result}).encode("utf-8")

    class Clipboard:
        sequence = 0
        value = None

        def write(self, value):
            self.value = value
            self.sequence += 1
            return self.sequence

        def clear_if_sequence(self, sequence):
            if self.sequence == sequence:
                self.value = None

    admin._rpc_run = fake_hop
    fixture = Installation("a" * 64, "in-memory", "running", "fixture")
    try:
        capability = admin.rpc(fixture, "capabilities")
        if (capability["protocol"], capability["schema_version"], capability["admin_rpc_version"]) != (2, 2, 1):
            raise ValueError("Candidate version mismatch")
        user_id = uuid4().hex
        issued = admin.rpc(fixture, "users.create", {"user_id": user_id, "display_name": "Bundle fixture"})
        clipboard = Clipboard()
        with EphemeralSecret(issued.pop("issued_key"), clipboard=clipboard) as secret:
            if not secret.copy() or not clipboard.value:
                raise ValueError("Ephemeral copy check failed")
        if clipboard.value is not None or secret.reveal() is not None:
            raise ValueError("Ephemeral close check failed")
        current = admin.rpc(fixture, "users.get", {"user_id": user_id})
        if "issued_key" in current or current["user"]["id"] != user_id:
            raise ValueError("Secret recovery check failed")
        return {"protocol": 2, "schema_version": 2, "admin_rpc_version": 1}
    finally:
        store.close()
