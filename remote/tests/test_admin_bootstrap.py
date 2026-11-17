"""Provisioning scripts: all host/package commands are fake; no network/SSH."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from voxarium_studio_remote.admin_bootstrap import (
    DOCKER_HELPER, PREFLIGHT, bootstrap_script, port_check, tls_script, validate_domain,
)


@pytest.mark.parametrize("value,expected", [(None, None), ("remote.example.com", "remote.example.com"),
                                          ("Remote.Example.com.", "remote.example.com"),
                                          ("xn--bcher-kva.example.org", "xn--bcher-kva.example.org")])
def test_domain(value, expected):
    assert validate_domain(value) == expected


@pytest.mark.parametrize("value", ["", " ", "localhost", "foo.local", "foo.internal", "foo.test", "foo.invalid",
                                   "127.0.0.1", "::1", "https://remote.example.com", "foo.com:443", "*.example.com",
                                   "foo.com/path", "foo.com\n}", "foo;id.com", "$(id).com", "x..com", "-x.com",
                                   "x-.com", "x_.com", "a" * 64 + ".com", "foo.com..", 1])
def test_invalid_domain(value):
    with pytest.raises(ValueError):
        validate_domain(value)


@pytest.fixture
def bash():
    candidates = [Path(r"C:\Program Files\Git\bin\bash.exe"), Path(shutil.which("bash") or "/nonexistent")]
    for candidate in candidates:
        if candidate.is_file() and "WindowsApps" not in str(candidate):
            return str(candidate)
    pytest.skip("No Bash available for POSIX shell syntax/mocked execution")


@pytest.mark.parametrize("script", [PREFLIGHT, DOCKER_HELPER, bootstrap_script(), bootstrap_script("remote.example.com"),
                                    port_check(None), port_check("remote.example.com"), tls_script("remote.example.com")])
def test_generated_shell_syntax(bash, script):
    completed = subprocess.run([bash, "-n"], input=script.encode(), capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode()


@pytest.fixture
def fake_host(tmp_path, bash):
    """Run the actual bootstrap with only absolute host probes redirected.

    Every command capable of host mutation is an inert executable in PATH. The
    actual shell still evaluates root/sudo selection, package checks and errors.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands.log"
    os_release = tmp_path / "os-release"
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    comm = tmp_path / "comm"
    comm.write_text("systemd\n")
    stub = r'''#!/bin/sh
name=${0##*/}
case "$name" in
    uname) printf '%s\n' "${FAKE_KERNEL:-Linux}" ;;
    id) printf '%s\n' "${FAKE_UID:-0}" ;;
    dpkg-query)
        for package do :; done
        case ",${FAKE_INSTALLED:-}," in *",$package,"*) printf 'install ok installed';; *) exit 1;; esac ;;
    sudo)
        printf 'sudo %s\n' "$*" >> "$FAKE_LOG"
        [ "${FAKE_SUDO:-yes}" = yes ] || exit 1
        [ "$1" = -n ] || exit 99
        shift
        if [ "${1:-}" = -- ]; then shift; fi
        "$@" ;;
    timeout)
        printf 'timeout %s\n' "$*" >> "$FAKE_LOG"
        if [ "${1:-}" = --signal=TERM ]; then shift; shift; fi
        shift
        "$@" ;;
    apt-get|systemctl|docker)
        printf '%s %s\n' "$name" "$*" >> "$FAKE_LOG"
        [ "${FAKE_FAIL:-}" != "$name" ] || { echo "mock $name failed" >&2; exit 1; } ;;
    *) echo 'Unexpected mock command' >&2; exit 99 ;;
esac
'''
    for name in ("uname", "id", "dpkg-query", "sudo", "timeout", "apt-get", "systemctl", "docker"):
        path = binaries / name
        path.write_text(stub, newline="\n")
        path.chmod(0o755)

    def execute(*, distribution="ubuntu", running_systemd=True, **environment):
        os_release.write_text(f"ID={distribution}\n", newline="\n")
        comm.write_text("systemd\n" if running_systemd else "other\n", newline="\n")
        script = bootstrap_script()
        bin_path = binaries.as_posix()
        if os.name == "nt":
            bin_path = "/" + bin_path[0].lower() + bin_path[2:]
        script = script.replace("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                "PATH=" + shlex.quote(bin_path) + ":/usr/bin:/bin")
        script = script.replace("/etc/os-release", shlex.quote(os_release.as_posix()))
        script = script.replace("/run/systemd/system", shlex.quote(systemd.as_posix()))
        script = script.replace("/proc/1/comm", shlex.quote(comm.as_posix()))
        script = r'''
command() {
    if [ "${1:-}" = -v ] && [ "${2:-}" = docker ] && [ "${FAKE_DOCKER_PRESENT:-0}" = 0 ]; then return 1; fi
    builtin command "$@"
}
''' + script
        env = {**os.environ, "FAKE_LOG": log.as_posix(), **environment}
        completed = subprocess.run([bash, "-c", script], input=b"", capture_output=True, env=env, timeout=15)
        return completed, log.read_text() if log.exists() else ""

    return execute


@pytest.mark.parametrize("distribution", ["ubuntu", "debian"])
def test_clean_root_bootstrap_installs_and_starts_docker(fake_host, distribution):
    completed, log = fake_host(distribution=distribution)
    assert completed.returncode == 0, completed.stderr.decode()
    assert "--no-remove --no-upgrade --yes --no-install-recommends install ca-certificates curl tar coreutils iproute2 docker.io" in log
    assert "systemctl enable --now docker" in log
    assert "docker info" in log
    assert "sudo" not in log
    assert log.index("apt-get") < log.index("systemctl enable") < log.index("docker info")


def test_nonroot_bootstrap_uses_only_noninteractive_sudo(fake_host):
    completed, log = fake_host(FAKE_UID="1000")
    assert completed.returncode == 0, completed.stderr.decode()
    assert "sudo -n true" in log and "sudo -n -- timeout" in log


@pytest.mark.parametrize("kwargs,message", [
    ({"distribution": "fedora"}, "Unsupported OS"),
    ({"FAKE_KERNEL": "Darwin"}, "Unsupported OS"),
    ({"running_systemd": False}, "systemd must be running"),
    ({"FAKE_UID": "1000", "FAKE_SUDO": "no"}, "root SSH or passwordless sudo"),
    ({"FAKE_INSTALLED": "containerd.io"}, "Conflicting/existing package"),
])
def test_preflight_failure_precedes_any_mutation(fake_host, kwargs, message):
    completed, log = fake_host(**kwargs)
    assert completed.returncode != 0
    assert message in completed.stderr.decode()
    assert "apt-get" not in log and "systemctl" not in log and "docker info" not in log


def test_existing_packages_not_reinstalled(fake_host):
    completed, log = fake_host(FAKE_DOCKER_PRESENT="1", FAKE_INSTALLED="ca-certificates,curl,tar,coreutils,iproute2")
    assert completed.returncode == 0, completed.stderr.decode()
    assert "apt-get" not in log
    assert "docker info" in log


@pytest.mark.parametrize("command,diagnostic", [("apt-get", "APT update failed/timed out"),
                                               ("systemctl", "Cannot enable/start docker.service"),
                                               ("docker", "Docker daemon is not usable")])
def test_partial_failure_actionable_and_no_rollback(fake_host, command, diagnostic):
    completed, log = fake_host(FAKE_FAIL=command)
    assert completed.returncode != 0
    assert diagnostic in completed.stderr.decode()
    assert "remove" not in log.replace("--no-remove", "")
    assert "purge" not in log and "chmod" not in log and "usermod" not in log


def test_time_limits_and_safe_package_policy():
    script = bootstrap_script()
    assert "DPkg::Lock::Timeout=60" in script
    assert "--kill-after=10 300" in script and "--kill-after=10 600" in script
    assert "Acquire::http::Timeout=30" in script and "Acquire::https::Timeout=30" in script
    assert "DEBIAN_FRONTEND=noninteractive" in script
    assert "--no-remove" in script and "--no-upgrade" in script
    assert "curl |" not in script and "usermod" not in script and "chmod" not in script


@pytest.mark.parametrize("domain", [None, "remote.example.com"])
def test_full_deployment_script_syntax(bash, tmp_path, monkeypatch, domain):
    from voxarium_studio_remote import admin_transport
    key = tmp_path / "key"
    key.write_text("mock")
    scripts = []

    def run(_self, args, **kwargs):
        if args[:2] == ["sh", "-c"] or args[0] == "timeout":
            scripts.append(args[-1])
        return ""

    monkeypatch.setattr(admin_transport, "_source_archive", lambda _: b"tar")
    monkeypatch.setattr(admin_transport.RemoteAdmin, "_run", run)
    admin_transport.RemoteAdmin(admin_transport.Connection("host", "root", str(key))).install(tmp_path, domain)
    assert len(scripts) == 2
    for script in scripts:
        completed = subprocess.run([bash, "-n"], input=script.encode(), capture_output=True, timeout=10)
        assert completed.returncode == 0, completed.stderr.decode()


@pytest.mark.parametrize("active", [True, False])
def test_tls_only_opens_ports_for_active_ufw(bash, active):
    harness = r'''
fail() { echo "$*" >&2; exit 1; }
command() { if [ "$*" = '-v ufw' ]; then return 0; fi; builtin command "$@"; }
run_root() {
    case "$*" in
        'env LC_ALL=C ufw status') echo "Status: $FAKE_UFW" ;;
        'ufw allow '*) printf 'RULE %s\n' "$*" ;;
        'sh '*) echo 'CONFIG CREATED' ;;
        *) echo 'UNEXPECTED ROOT OPERATION' >&2; exit 99 ;;
    esac
}
docker_cmd() { echo 'CADDY CREATED'; }
curl() { return 0; }
sleep() { :; }
'''
    completed = subprocess.run([bash, "-c", harness + tls_script("remote.example.com")], capture_output=True,
                               env={**os.environ, "FAKE_UFW": "active" if active else "inactive"}, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode()
    output = completed.stdout.decode()
    assert "HTTPS certificate and proxy check passed" in output
    assert ("RULE ufw allow 80/tcp" in output) is active
    assert ("RULE ufw allow 443/tcp" in output) is active
    assert ("RULE ufw allow 443/udp" in output) is active


def test_tls_failure_never_claims_certificate_or_removes_resources(bash):
    harness = r'''
fail() { echo "$*" >&2; exit 1; }
command() { if [ "$*" = '-v ufw' ]; then return 1; fi; builtin command "$@"; }
run_root() { :; }
docker_cmd() { echo "DOCKER $*"; }
curl() { return 1; }
sleep() { :; }
'''
    completed = subprocess.run([bash, "-c", harness + tls_script("remote.example.com")], capture_output=True, timeout=10)
    assert completed.returncode != 0
    assert "HTTPS is not ready" in completed.stderr.decode()
    assert "are preserved; do not reinstall" in completed.stderr.decode()
    assert "check passed" not in completed.stdout.decode()
    assert "DOCKER rm" not in completed.stdout.decode()


@pytest.mark.parametrize("listeners,domain,blocked", [
    ("LISTEN 0 128 127.0.0.1:8787 0.0.0.0:*", None, True),
    ("LISTEN 0 128 [::]:443 [::]:*", "remote.example.com", True),
    ("LISTEN 0 128 *:80 *:*", "remote.example.com", True),
    ("LISTEN 0 128 0.0.0.0:22 0.0.0.0:*", "remote.example.com", False),
    ("LISTEN 0 128 0.0.0.0:80 0.0.0.0:*", None, False),
])
def test_port_conflicts_are_read_only(bash, listeners, domain, blocked):
    harness = r'''
fail() { echo "$*" >&2; exit 1; }
run_root() { printf '%s\n' "$FAKE_LISTENERS"; }
'''
    completed = subprocess.run([bash, "-c", harness + port_check(domain)], capture_output=True,
                               env={**os.environ, "FAKE_LISTENERS": listeners}, timeout=10)
    assert (completed.returncode != 0) is blocked, completed.stderr.decode()
