import importlib.util
import hashlib
import json
from pathlib import Path
import sys

import pytest

from voxarium_studio_remote import admin_bundle

spec = importlib.util.spec_from_file_location("build_admin_bundle", Path(__file__).parents[1] / "build_admin_bundle.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def source(root):
    remote = root / "remote"
    package = remote / "voxarium_studio_remote"
    package.mkdir(parents=True)
    for name in admin_bundle.REQUIRED_REMOTE_FILES:
        (remote / name).write_text("content")
    return remote


def test_source_resolver_ignores_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert admin_bundle.default_source() == Path(admin_bundle.__file__).resolve().parents[1]
    assert admin_bundle.ssh_executable() == "ssh"


def test_frozen_resolvers(tmp_path, monkeypatch):
    assets = tmp_path / "admin_assets"
    remote = source(assets)
    (assets / "openssh").mkdir()
    ssh = assets / "openssh" / "ssh.exe"
    ssh.write_bytes(b"ssh")
    for name in ("migrations.py", "admin_rpc.py"):
        (remote / "voxarium_studio_remote" / name).write_text("content")
    (assets / "manifest.json").write_text(json.dumps({
        "format": 1, "wire_protocol": 2, "schema_version": 2, "admin_rpc_version": 1,
        "files": {p.relative_to(assets).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in assets.rglob("*") if p.is_file()},
    }))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.chdir(tmp_path.parent)
    assert admin_bundle.default_source() == remote
    assert admin_bundle.ssh_executable() == str(ssh)


@pytest.mark.parametrize("resolver", [admin_bundle.default_source, admin_bundle.ssh_executable])
def test_frozen_missing_fails_not_external(tmp_path, monkeypatch, resolver):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(RuntimeError, match="Rebuild"):
        resolver()


def test_staging_allowlist(tmp_path, monkeypatch):
    remote = source(tmp_path)
    for name in (".env", "key.pem", "settings.json", "secret.py"):
        (remote / name).write_text("SECRET")
    package = remote / "voxarium_studio_remote"
    (package / "server.py").write_text("pass")
    (package / "key.pem").write_text("SECRET")
    for name in ("__pycache__", "tests", ".hidden"):
        (package / name).mkdir()
        (package / name / "secret.py").write_text("SECRET")
    ssh = tmp_path / "ssh.exe"
    ssh.write_bytes(b"ssh")
    monkeypatch.setattr(builder, "ssh_files", lambda _: [ssh])
    output = tmp_path / "assets"
    builder.stage_assets(tmp_path, output, tmp_path)
    assert sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()) == sorted([
        "manifest.json", "openssh/ssh.exe", "remote/voxarium_studio_remote/server.py",
        *(f"remote/{name}" for name in admin_bundle.REQUIRED_REMOTE_FILES),
    ])
    with pytest.raises(ValueError, match="empty"):
        builder.stage_assets(tmp_path, output, tmp_path)


def test_frozen_manifest_rejects_tamper(tmp_path, monkeypatch):
    assets = tmp_path / "admin_assets"
    source(assets)
    (assets / "openssh").mkdir()
    (assets / "openssh/ssh.exe").write_bytes(b"ssh")
    (assets / "payload.py").write_text("safe")
    (assets / "manifest.json").write_text(json.dumps({
        "format": 1, "wire_protocol": 2, "schema_version": 2, "admin_rpc_version": 1,
        "files": {p.relative_to(assets).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in assets.rglob("*") if p.is_file()},
    }))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert admin_bundle.verify_assets()["wire_protocol"] == 2
    (assets / "payload.py").write_text("changed")
    with pytest.raises(RuntimeError, match="Rebuild"):
        admin_bundle.verify_assets()


@pytest.mark.parametrize("missing", ["app.py", "store.py", "hub.py", "protocol.py", "cli.py", "auth.py"])
def test_complete_deployment_context_required_for_staging_and_frozen_resolver(tmp_path, monkeypatch, missing):
    assets = tmp_path / "admin_assets"
    remote = source(assets)
    (remote / "voxarium_studio_remote" / missing).unlink()
    with pytest.raises(ValueError, match="missing"):
        builder.source_files(remote)
    (assets / "openssh").mkdir()
    (assets / "openssh/ssh.exe").write_bytes(b"ssh")
    # Every listed hash is correct, but the context is incomplete.
    (assets / "manifest.json").write_text(json.dumps({
        "format": 1, "wire_protocol": 2, "schema_version": 2, "admin_rpc_version": 1,
        "files": {p.relative_to(assets).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in assets.rglob("*") if p.is_file()},
    }))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(RuntimeError, match="Rebuild"):
        admin_bundle.default_source()


@pytest.mark.parametrize("directory", [False, True])
def test_source_rejects_links(tmp_path, directory):
    remote = source(tmp_path)
    target = tmp_path / "outside"
    if directory:
        target.mkdir()
    else:
        target.write_text("SECRET")
    link = remote / "voxarium_studio_remote" / ("linked" if directory else "linked.py")
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError, match="Links"):
        builder.source_files(remote)


def test_missing_required_source(tmp_path):
    remote = source(tmp_path)
    (remote / "Dockerfile").unlink()
    with pytest.raises(ValueError, match="missing"):
        builder.source_files(remote)


def test_missing_openssh_actionable(tmp_path):
    with pytest.raises(ValueError, match="Install the Windows optional feature"):
        builder.ssh_files(tmp_path / "OpenSSH")


def test_offline_runtime_check_has_no_network_files_or_real_clipboard(monkeypatch, capsys):
    import socket
    import subprocess
    from voxarium_studio_remote import admin_secrets

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline check attempted an external side effect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(admin_secrets, "WindowsClipboard", forbidden)
    assert admin_bundle.offline_runtime_check() == {"protocol": 2, "schema_version": 2, "admin_rpc_version": 1}
    assert capsys.readouterr().out == ""
