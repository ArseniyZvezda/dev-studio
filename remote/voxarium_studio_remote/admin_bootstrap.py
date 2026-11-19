"""Conservative Ubuntu/Debian provisioning for the separate Remote (§16).

These scripts run only after explicit installation consent, never on discovery.
Package installation uses the distribution's signed repositories, not curl | sh.
"""

from __future__ import annotations

import ipaddress
import re
import shlex


CADDY_IMAGE = "caddy:2-alpine"
CADDY_CONTAINER = "voxarium-studio-remote-caddy"
CADDY_DATA = "voxarium-studio-remote-caddy-data"
CADDY_CONFIG = "voxarium-studio-remote-caddy-config"
CADDY_DIRECTORY = "/etc/voxarium-studio-remote-caddy"


def validate_domain(domain: str | None) -> str | None:
    """Accept an ASCII public DNS hostname, never a URL, IP, wildcard or config."""
    if domain is None:
        return None
    if not isinstance(domain, str):
        raise ValueError("HTTPS domain must be a DNS hostname, without a URL or port.")
    domain = domain.lower().removesuffix(".")
    labels = domain.split(".")
    if (len(domain) > 253 or len(labels) < 2 or labels[-1].isdigit()
            or labels[-1] in {"localhost", "local", "internal", "test", "invalid"}
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)):
        raise ValueError("HTTPS domain must be a public DNS hostname, without a URL or port.")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("HTTPS requires a DNS hostname, not an IP address.")


ROOT_HELPER = r'''
run_root() {
    if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n -- "$@"; fi
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
'''

# No daemon, package, service or firewall changes precede these checks.
PREFLIGHT = r'''
set -eu
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
''' + ROOT_HELPER + r'''
[ "$(uname -s)" = Linux ] || fail 'Unsupported OS: installation requires Ubuntu/Debian Linux with systemd and apt.'
[ -r /etc/os-release ] || fail 'Unsupported OS: /etc/os-release is missing.'
. /etc/os-release
case "${ID:-}" in ubuntu|debian) ;; *) fail 'Unsupported OS: automatic installation supports Ubuntu and Debian only; nothing changed.' ;; esac
for tool in apt-get dpkg-query systemctl timeout; do
    command -v "$tool" >/dev/null 2>&1 || fail "Unsupported host: required base utility $tool is missing; nothing changed."
done
[ -d /run/systemd/system ] && [ "$(cat /proc/1/comm)" = systemd ] || fail 'Unsupported host: systemd must be running as PID 1; nothing changed.'
if [ "$(id -u)" != 0 ]; then
    command -v sudo >/dev/null 2>&1 && sudo -n true </dev/null || fail 'Installation requires root SSH or passwordless sudo -n; nothing changed. No password will be requested.'
fi
'''


def port_check(domain: str | None, *, public_http: bool = False) -> str:
    ports = "8787|80|443" if domain else "8787|80" if public_http else "8787"
    return "\n".join([
        'listeners=$(run_root ss -H -ltn)',
        f"if printf '%s\\n' \"$listeners\" | awk '$4 ~ /:({ports})$/ {{found=1}} END {{exit !found}}'; then",
        "  fail 'Required TCP port is already occupied; existing services were not stopped or replaced.'", "fi",
        *([
            'listeners=$(run_root ss -H -lun)',
            "if printf '%s\\n' \"$listeners\" | awk '$4 ~ /:443$/ {found=1} END {exit !found}'; then",
            "  fail 'UDP port 443 is already occupied; existing service was not changed.'", "fi",
        ] if domain else []),
    ])


def bootstrap_script(domain: str | None = None) -> str:
    domain = validate_domain(domain)
    return PREFLIGHT + (f'''
if run_root test -e {CADDY_DIRECTORY} || run_root test -L {CADDY_DIRECTORY}; then
    fail 'Existing Remote Caddy configuration found; refusing to replace it.'
fi
''' if domain else "") + r'''
installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = 'install ok installed' ]; }
packages=''
for package in ca-certificates curl tar coreutils iproute2; do
    if ! installed "$package"; then packages="$packages $package"; fi
done
if ! command -v docker >/dev/null 2>&1; then
    for package in docker-ce docker-ce-cli containerd.io podman-docker; do
        if installed "$package"; then fail "Conflicting/existing package $package: repair Docker manually; no packages were removed."; fi
    done
    packages="$packages docker.io"
fi
if [ -n "$packages" ]; then
    printf 'Installing missing distribution packages:%s\n' "$packages"
    run_root timeout --signal=TERM --kill-after=10 300 env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -o DPkg::Lock::Timeout=60 -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update </dev/null || fail 'APT update failed/timed out; check repositories, network and apt locks. No automatic rollback.'
    run_root timeout --signal=TERM --kill-after=10 600 env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -o DPkg::Lock::Timeout=60 -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 --no-remove --no-upgrade --yes --no-install-recommends install $packages </dev/null || fail 'APT installation failed/timed out; packages may be partially installed. Inspect apt/dpkg before retrying; nothing was removed.'
fi
run_root timeout --signal=TERM --kill-after=10 60 systemctl enable --now docker </dev/null || fail 'Cannot enable/start docker.service; inspect systemctl status docker and journalctl -u docker. Packages are preserved.'
run_root timeout --signal=TERM --kill-after=10 30 docker info >/dev/null </dev/null || fail 'Docker daemon is not usable after provisioning; inspect docker.service. No data was removed.'
printf '%s\n' 'Docker prerequisites ready.'
'''


# The info probes cannot consume the build-context tar stream on stdin.
DOCKER_HELPER = ROOT_HELPER + r'''
if timeout 20 docker info >/dev/null 2>&1 </dev/null; then
    docker_cmd() { command docker "$@"; }
elif run_root timeout 20 docker info >/dev/null 2>&1 </dev/null; then
    docker_cmd() { run_root docker "$@"; }
else
    fail 'Docker daemon is unavailable or access requires root/passwordless sudo -n.'
fi
'''


def _proxy_commands(caddyfile: str) -> list[str]:
    create = 'umask 022; mkdir -- "$1"; printf "%s\\n" "$2" > "$1/Caddyfile"'
    return [
        shlex.join(["run_root", "sh", "-eu", "-c", create, "sh", CADDY_DIRECTORY, caddyfile]),
        shlex.join(["docker_cmd", "run", "--detach", "--name", CADDY_CONTAINER,
                    "--network", "host", "--restart", "unless-stopped",
                    "--mount", f"type=bind,source={CADDY_DIRECTORY},target=/etc/caddy,readonly",
                    "--volume", f"{CADDY_DATA}:/data", "--volume", f"{CADDY_CONFIG}:/config",
                    CADDY_IMAGE]),
    ]


def http_script() -> str:
    """Explicit public HTTP by IP (dev spec §16.2); the API stays on loopback."""
    caddyfile = "{\n    admin off\n    auto_https off\n}\n:80 {\n    reverse_proxy 127.0.0.1:8787\n}\n"
    return "\n".join([
        *_proxy_commands(caddyfile),
        "if command -v ufw >/dev/null 2>&1; then",
        "  firewall=$(run_root env LC_ALL=C ufw status)",
        "  if printf '%s\\n' \"$firewall\" | grep -q '^Status: active$'; then",
        "    run_root ufw allow 80/tcp",
        "  fi", "fi",
        "http_ready=0",
        "for attempt in 1 2 3 4 5 6; do",
        "  if curl --noproxy '*' --fail --silent --show-error --connect-timeout 3 --max-time 5 http://127.0.0.1/healthz >/dev/null; then http_ready=1; break; fi",
        "  sleep 2", "done",
        "[ \"$http_ready\" = 1 ] || fail 'Remote is installed, but its HTTP proxy is not ready. Inspect docker logs voxarium-studio-remote-caddy; containers and data were preserved.'",
        "printf '%s\\n' 'HTTP proxy health check passed (local server check; external firewall must allow TCP 80).'",
    ])


def tls_script(domain: str) -> str:
    domain = validate_domain(domain)
    assert domain is not None
    caddyfile = "{\n    admin off\n}\n" + domain + " {\n    reverse_proxy 127.0.0.1:8787\n}\n"
    return "\n".join([
        *_proxy_commands(caddyfile),
        # Only an already-active UFW is changed, and SSH/default policy is untouched.
        "if command -v ufw >/dev/null 2>&1; then",
        "  firewall=$(run_root env LC_ALL=C ufw status)",
        "  if printf '%s\\n' \"$firewall\" | grep -q '^Status: active$'; then",
        "    run_root ufw allow 80/tcp",
        "    run_root ufw allow 443/tcp",
        "    run_root ufw allow 443/udp",
        "  fi", "fi",
        # A valid certificate is never claimed on the basis of a running container.
        "tls_ready=0",
        "for attempt in 1 2 3 4 5 6; do",
        f"  if curl --noproxy '*' --fail --silent --show-error --connect-timeout 5 --max-time 10 --resolve {domain}:443:127.0.0.1 https://{domain}/healthz >/dev/null; then tls_ready=1; break; fi",
        "  sleep 5", "done",
        "if [ \"$tls_ready\" = 1 ]; then",
        f"  printf '%s\\n' 'HTTPS certificate and proxy check passed: https://{domain}'",
        "else",
        "  fail 'Remote is installed, but HTTPS is not ready. Check DNS A/AAAA, inbound 80/443, cloud/external firewall and docker logs voxarium-studio-remote-caddy. Containers, certificates and configuration are preserved; do not reinstall.'",
        "fi",
    ])
