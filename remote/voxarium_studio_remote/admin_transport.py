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
    DOCKER_HELPER, bootstrap_script, http_script, port_check, tls_script, validate_domain,
)


LABEL = "io.voxarium.studio.remote"
IMAGE = "voxarium-studio-remote:admin"
UPDATE_IMAGE = "voxarium-studio-remote:admin-update"
CONTAINER = "voxarium-studio-remote"
VOLUME = "voxarium-studio-remote-data"
MAX_CONTEXT_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+A-Za-z0-9.]+)?\Z")

# Shared by install and update: the container must answer its own health probe
# before the deployment is reported as successful.
_HEALTH_PY = """import json, time, urllib.request
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
# One attempt only: update retries docker exec itself as well, because the
# daemon rejects exec while a newly started container is briefly restarting.
_UPDATE_PROBE = """import json, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open('http://127.0.0.1:8787/healthz', timeout=2) as response:
        result = json.loads(response.read(8192))
    ok = (isinstance(result, dict) and result.get('status') == 'ok'
          and result.get('service') == 'voxarium_studio_remote')
except (OSError, ValueError):
    ok = False
raise SystemExit(0 if ok else 1)
"""

# Inspect only the schema on a read-only mount before any downtime. Do not
# print project metadata or invoke a migration. SQLite may need its WAL/SHM;
# an inconclusive probe leaves the normal startup/rollback checks in charge.
_UPDATE_SCHEMA_PROBE = """import sqlite3
from contextlib import closing
from voxarium_studio_remote.migrations import schema_version
with closing(sqlite3.connect('file:/data/remote.db?mode=ro', uri=True, timeout=3)) as db:
    db.execute('BEGIN')
    if schema_version(db) == 1:
        print('legacy')
"""

_UPDATE_PREFLIGHT = r'''
name=$(docker_cmd inspect --format '{{.Name}}' "$old")
name=${name#/}
[ -n "$name" ] || fail 'Cannot read the container name; nothing was changed.'
backup="$name.admin-previous"
staging="$name.admin-candidate"
compose=$(docker_cmd inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$old")
network=$(docker_cmd inspect --format '{{.HostConfig.NetworkMode}}' "$old")
managed=$(docker_cmd inspect --format '{{index .Config.Labels "io.voxarium.studio.remote"}}' "$old")
[ -z "$compose" ] && [ "$network" = host ] && [ "$managed" = true ] ||
    fail 'Update supports installations created by Remote Admin with host networking. For Compose or custom deployments, update using the original deployment configuration. Nothing was changed.'
# Accept one existing writable named volume, never a bind mount, another
# destination or additional mounts that the replacement would drop.
volume=$(docker_cmd inspect --format '{{if eq (len .Mounts) 1}}{{with index .Mounts 0}}{{if and (eq .Type "volume") (eq .Destination "/data") .RW}}{{.Name}}{{end}}{{end}}{{end}}' "$old")
case "$volume" in
    ''|*[!A-Za-z0-9_.-]*|[-.]*) fail 'Expected one writable named /data volume; nothing was changed.' ;;
esac
[ "$(docker_cmd volume inspect --format '{{.Name}}' "$volume")" = "$volume" ] ||
    fail 'The original /data volume could not be verified; nothing was changed.'
restart=$(docker_cmd inspect --format '{{with .HostConfig.RestartPolicy}}{{.Name}}{{if and (eq .Name "on-failure") (gt .MaximumRetryCount 0)}}:{{.MaximumRetryCount}}{{end}}{{end}}' "$old")
[ -n "$restart" ] || restart=no
case "$(docker_cmd inspect --format '{{json .Config.Cmd}}' "$old")" in
    '["voxarium-studio-remote","serve","--host","127.0.0.1","--port","8787"]') ai_flag='' ;;
    '["voxarium-studio-remote","serve","--host","127.0.0.1","--port","8787","--ai-control"]') ai_flag=--ai-control ;;
    '["voxarium-studio-remote","serve","--host","127.0.0.1","--port","8787","--no-ai-control"]') ai_flag=--no-ai-control ;;
    *) fail 'This installation has a custom launch command. Update it using the original deployment configuration. Nothing was changed.' ;;
esac
config='{{json .Config.Entrypoint}} {{.Config.User}} {{.Config.WorkingDir}}{{range .Config.Env}}{{if ne (index (split . "=") 0) "VOXARIUM_REMOTE_AI_CONTROL"}}{{printf "\\n%s" .}}{{end}}{{end}}'
old_image=$(docker_cmd inspect --format '{{.Image}}' "$old")
original_config=$(docker_cmd image inspect --format "$config" "$old_image")
container_config=$(docker_cmd inspect --format "$config" "$old")
[ "$original_config" = "$container_config" ] ||
    fail 'This installation has custom environment or launch settings. Update it using the original deployment configuration. Nothing was changed.'
ai_control=$(docker_cmd inspect --format '{{range .Config.Env}}{{if eq (index (split . "=") 0) "VOXARIUM_REMOTE_AI_CONTROL"}}{{.}}{{end}}{{end}}' "$old")
case "$(docker_cmd inspect --format '{{.State.Status}}' "$old")" in
    running|restarting|created|exited) ;;
    *) fail 'The original container is not in an updatable state; nothing was changed.' ;;
esac
work=$(mktemp -d /tmp/voxarium-remote-admin.XXXXXXXXXX)
case "$work" in /tmp/voxarium-remote-admin.*) ;; *) exit 1 ;; esac
'''

# Exit status the update script reserves for "the /data volume is v1"; the
# client maps it to LegacySchemaError so the UI can offer Migrate & Update.
_LEGACY_SCHEMA_EXIT = 3
_UPDATE_HINT = (
    "The data volume uses the legacy v1 schema. Choose Migrate & Update for the offline "
    "migration with a validated backup, or follow remote/README.md; Update never migrates data."
)
_MIGRATE_HINT = "The new server rejected the data schema after a reported-successful migration."

# Shared rollback primitive: restore the renamed, stopped original container.
_RESTORE_PREVIOUS = r'''
restore_previous() {
    if ! docker_cmd rename "$old" "$name"; then
        printf 'Rollback failed to restore the original name; previous container is retained as %s.\n' "$backup" >&2
    elif [ "$was_running" = true ]; then
        if docker_cmd start "$old" >/dev/null &&
           [ "$(docker_cmd inspect --format '{{.State.Running}}' "$old" 2>/dev/null)" = true ]; then
            printf '%s\n' 'The original container was started again; verify service health before retrying.' >&2
        else
            printf '%s\n' 'Rollback could not restart the original container; it is retained for inspection.' >&2
        fi
    else
        printf '%s\n' 'The original container was restored to its stopped state.' >&2
    fi
}
'''

_CLEANUP_UPDATE = r'''
cleanup_update() {
    rc=$?
    trap - EXIT
    trap '' HUP INT TERM
    if [ "$committed" = 0 ]; then
        candidate_id=''
        if [ -s "$work/candidate.id" ]; then read -r candidate_id < "$work/candidate.id" || true; fi
        if [ -n "$candidate_id" ]; then
            docker_cmd rm -f -- "$candidate_id" >/dev/null ||
                printf '%s\n' 'Could not remove the failed replacement container.' >&2
        fi
        # Check by ID: a timeout may have killed the client after the daemon
        # applied rename. Never reconstruct the previous container from flags.
        if [ "$renaming" = 1 ] &&
           [ "$(docker_cmd inspect --format '{{.Name}}' "$old" 2>/dev/null)" = "/$backup" ]; then
            restore_previous
        elif [ "$renaming" = 1 ]; then
            printf '%s\n' 'Verify the original container state before retrying.' >&2
        fi
    fi
    docker_cmd image rm "$image" >/dev/null 2>&1 || true
    rm -rf -- "$work"
    exit "$rc"
}
'''

# Migrate & Update cleanup: the v1 container may only be restarted on v1 data.
# A failed/interrupted apply is atomic, but a kill just after its commit is
# indistinguishable from the exit code alone, so an unknown outcome re-probes
# the schema read-only and restores the validated v1 backup when needed.
_CLEANUP_MIGRATE = r'''
data_schema() {
    # legacy | current | unknown — read-only probe inside the new image.
    if ! schema_out=$(docker_cmd run --rm --network none \
            --mount "type=volume,source=$volume,target=/data,readonly,volume-nocopy" \
            --entrypoint python "$image" -c "$schema_probe" </dev/null 2>/dev/null); then
        printf unknown
        return 0
    fi
    if [ "$schema_out" = legacy ]; then printf legacy; else printf current; fi
}

schema_rollback() {
    docker_cmd run --rm --network none --volume "$volume:/data" \
        "$image" voxarium-studio-remote migration rollback --data /data \
        --backup "$backup_db" --recovery-backup "$recovery_db" \
        --confirm-offline --accept-data-loss </dev/null > "$work/schema-rollback.log" 2>&1
}

cleanup_update() {
    rc=$?
    trap - EXIT
    trap '' HUP INT TERM
    if [ "$committed" = 0 ]; then
        candidate_id=''
        if [ -s "$work/candidate.id" ]; then read -r candidate_id < "$work/candidate.id" || true; fi
        if [ -n "$candidate_id" ]; then
            docker_cmd rm -f -- "$candidate_id" >/dev/null ||
                printf '%s\n' 'Could not remove the failed replacement container.' >&2
        fi
        if [ "$renaming" = 1 ] &&
           [ "$(docker_cmd inspect --format '{{.Name}}' "$old" 2>/dev/null)" = "/$backup" ]; then
            v1_ok=1
            if [ "$migrated" = 1 ]; then
                v1_ok=0
            elif [ "$migrate_started" = 1 ] && [ "$apply_failed" = 0 ] &&
                 [ "$(data_schema)" != legacy ]; then
                # apply was interrupted with an unknown outcome: verify before
                # trusting the v1 binary with the volume again.
                v1_ok=0
            fi
            if [ "$v1_ok" = 0 ]; then
                if schema_rollback; then
                    printf '%s\n' 'The validated v1 backup was restored; the migrated state was kept in the recovery backup inside the volume.' >&2
                    v1_ok=1
                else
                    # Show only the CLI's own diagnostics; they carry no request data.
                    tail -c 1600 "$work/schema-rollback.log" >&2 2>/dev/null || true
                    printf 'CRITICAL: the data volume is on schema v2 and the previous v1 container must NOT be started. The v1 backup is preserved at %s inside the volume; follow the rollback procedure in remote/README.md.\n' "$backup_db" >&2
                    v1_ok=2
                fi
            fi
            if [ "$v1_ok" = 1 ]; then
                restore_previous
            fi
        elif [ "$renaming" = 1 ]; then
            printf '%s\n' 'Verify the original container state before retrying.' >&2
        fi
    fi
    docker_cmd image rm "$image" >/dev/null 2>&1 || true
    rm -rf -- "$work"
    exit "$rc"
}
'''

_CUTOVER_FUNCS = r'''
probe_update() {
    attempt=0
    seen=''
    while [ "$attempt" -lt 30 ]; do
        attempt=$((attempt + 1))
        since=$(docker_cmd inspect --format '{{if eq .State.Status "running"}}{{.State.StartedAt}}{{end}}' "$candidate" 2>/dev/null) || since=''
        if [ -n "$since" ] && docker_cmd exec "$candidate" python -c "$probe" >/dev/null 2>&1 &&
           [ "$since" = "$(docker_cmd inspect --format '{{if eq .State.Status "running"}}{{.State.StartedAt}}{{end}}' "$candidate" 2>/dev/null)" ]; then
            # Demand two successes from the same running generation. On a host
            # network, an unrelated listener must not mask a crash/restart.
            [ "$seen" != "$since" ] || return 0
            seen=$since
        else
            seen=''
        fi
        [ "$attempt" -eq 30 ] || sleep 1
    done
    return 1
}

report_candidate_failure() {
    # Logs can contain sensitive paths/requests. Match only known schema
    # diagnostics and never print raw container logs in the Admin console.
    if docker_cmd logs --tail 80 "$candidate" 2>&1 |
       grep -Fq 'SchemaError: Legacy database requires explicit offline migration'; then
        printf '%s\n' "$migration_hint" >&2
    else
        docker_cmd inspect --format 'Replacement state: {{.State.Status}}; exit code: {{.State.ExitCode}}; OOM killed: {{.State.OOMKilled}}.' "$candidate" >&2 || true
    fi
}
'''

_CUTOVER_HEAD_UPDATE = r'''
committed=0
renaming=0
trap cleanup_update EXIT
'''

_CUTOVER_HEAD_MIGRATE = r'''
committed=0
renaming=0
migrate_started=0
apply_failed=0
migrated=0
trap cleanup_update EXIT
'''

_CUTOVER_BUILD = r'''
trap 'exit 1' HUP INT TERM
tar -xf - -C "$work"
if ! docker_cmd build --tag "$image" "$work" > "$work/build.log" 2>&1; then
    tail -c 1600 "$work/build.log" >&2
    fail 'Docker image build failed; the current container was not changed.'
fi
new_image_id=$(docker_cmd image inspect --format '{{.Id}}' "$image")
'''

# Update refuses v1 data with a distinct exit status; nothing was changed.
_SCHEMA_GUARD_UPDATE = r'''
if schema=$(docker_cmd run --rm --network none --mount "type=volume,source=$volume,target=/data,readonly,volume-nocopy" \
              --entrypoint python "$new_image_id" -c "$schema_probe" </dev/null 2>/dev/null) && [ "$schema" = legacy ]; then
    printf '%s\n' "$migration_hint The current container was not changed." >&2
    exit 3
fi
'''

# Migrate & Update is the inverse: a verified v1 volume is its precondition.
_SCHEMA_GUARD_MIGRATE = r'''
if ! schema=$(docker_cmd run --rm --network none --mount "type=volume,source=$volume,target=/data,readonly,volume-nocopy" \
                --entrypoint python "$new_image_id" -c "$schema_probe" </dev/null 2>/dev/null); then
    fail 'Could not verify the data schema; migration requires a readable v1 database. Nothing was changed.'
fi
if [ "$schema" != legacy ]; then
    fail 'The data volume is not on the legacy v1 schema; choose the normal Update instead. Nothing was changed.'
fi
'''

_CUTOVER_MIDDLE = r'''
[ "$(docker_cmd inspect --format '{{.Id}}' "$name")" = "$old" ] ||
    fail 'The target changed during the build; discover installations again.'
if docker_cmd inspect "$backup" >/dev/null 2>&1; then
    fail 'An earlier update left a rollback container; inspect it before retrying.'
fi

# Create under a staging name before touching the live container. The fixed
# name also prevents two Admin updates from entering cutover simultaneously.
set --
if [ -n "$ai_control" ]; then set -- --env "$ai_control"; fi
candidate=$(docker_cmd create --cidfile "$work/candidate.id" --name "$staging" --label "$label" \
    --restart "$restart" --network host --volume "$volume:/data" \
    "$@" "$image" voxarium-studio-remote serve --host 127.0.0.1 --port 8787 $ai_flag) ||
    fail 'Could not create a replacement container; the current container was not changed.'
# Compare Docker's normalized settings before cutting over. In particular,
# do not silently drop any custom resource, network, or security options.
settings='{{json .HostConfig}} {{json .Config.Labels}}'
original_settings=$(docker_cmd inspect --format "$settings" "$old")
candidate_settings=$(docker_cmd inspect --format "$settings" "$candidate")
[ "$original_settings" = "$candidate_settings" ] ||
    fail 'This installation has different Docker settings. Update it using the original deployment configuration. Nothing was changed on the current container.'
[ "$(docker_cmd inspect --format '{{.Id}}' "$name")" = "$old" ] ||
    fail 'The target changed before cutover; discover installations again.'
was_running=$(docker_cmd inspect --format '{{.State.Running}}' "$old")
renaming=1
docker_cmd rename "$old" "$backup"
docker_cmd stop -t 30 -- "$old" >/dev/null
'''

# Offline migration between stopping the original and starting the candidate:
# the container is stopped, so --confirm-offline is the tool's verified claim.
# apply is atomic and creates the validated v1 backup inside the volume first.
_MIGRATION_STEP = r'''
migrate_started=1
printf '%s\n' 'Running the offline v1 to v2 data migration (service stopped)...'
if ! docker_cmd run --rm --network none --volume "$volume:/data" \
        "$image" voxarium-studio-remote migration apply --data /data \
        --backup "$backup_db" --confirm-offline </dev/null > "$work/migrate.log" 2>&1; then
    apply_failed=1
    tail -c 1600 "$work/migrate.log" >&2
    fail 'Offline migration failed; the data volume was not upgraded. The previous container is being restored.'
fi
migrated=1
tail -c 2000 "$work/migrate.log"
'''

_CUTOVER_TAIL_UPDATE = r'''
if ! docker_cmd rename "$candidate" "$name" || ! docker_cmd start "$candidate" >/dev/null; then
    report_candidate_failure
    fail 'The updated container failed to start.'
fi
if ! probe_update; then
    report_candidate_failure
    fail 'Updated Remote did not become healthy.'
fi
docker_cmd tag "$image" "$canonical_image"
# From here on a healthy replacement owns the original name. A failure to
# remove the stopped rollback container must not take the new service down.
committed=1
if ! docker_cmd rm -- "$old" >/dev/null; then
    printf 'Warning: the stopped previous container is still retained as %s.\n' "$backup"
fi
printf '%s\n' "Remote updated: container $name is running the new build."
'''

# After a migration the rollback path is the cleanup's schema restore, so the
# failure messages must say what the operator's data is doing, not just the
# container's.
_CUTOVER_TAIL_MIGRATE = r'''
if ! docker_cmd rename "$candidate" "$name" || ! docker_cmd start "$candidate" >/dev/null; then
    report_candidate_failure
    fail 'The updated container failed to start after the migration; the v1 backup is being restored and the previous container restarted.'
fi
if ! probe_update; then
    report_candidate_failure
    fail 'Updated Remote did not become healthy after the migration; the v1 backup is being restored and the previous container restarted.'
fi
docker_cmd tag "$image" "$canonical_image"
committed=1
if ! docker_cmd rm -- "$old" >/dev/null; then
    printf 'Warning: the stopped previous container is still retained as %s.\n' "$backup"
fi
printf '%s\n' "Remote updated and migrated to schema v2: container $name is running the new build."
printf 'The validated v1 backup is preserved inside the data volume at %s (owner-only). Copy it out and delete it only after verifying the new installation; see remote/README.md.\n' "$backup_db"
'''

_UPDATE_CUTOVER = (
    _RESTORE_PREVIOUS + _CLEANUP_UPDATE + _CUTOVER_FUNCS + _CUTOVER_HEAD_UPDATE +
    _CUTOVER_BUILD + _SCHEMA_GUARD_UPDATE + _CUTOVER_MIDDLE + _CUTOVER_TAIL_UPDATE
)
_UPDATE_MIGRATE_CUTOVER = (
    _RESTORE_PREVIOUS + _CLEANUP_MIGRATE + _CUTOVER_FUNCS + _CUTOVER_HEAD_MIGRATE +
    _CUTOVER_BUILD + _SCHEMA_GUARD_MIGRATE + _CUTOVER_MIDDLE + _MIGRATION_STEP +
    _CUTOVER_TAIL_MIGRATE
)


def _update_script(container: str, stamp: str, *, migrate: bool) -> str:
    lines = [
        "set -eu", "umask 077", DOCKER_HELPER,
        f"old={shlex.quote(container)}",
        f"label={shlex.quote(LABEL + '=true')}",
        f"image={shlex.quote(UPDATE_IMAGE + '-' + stamp)}",
        f"canonical_image={shlex.quote(IMAGE)}",
        f"probe={shlex.quote(_UPDATE_PROBE)}",
        f"schema_probe={shlex.quote(_UPDATE_SCHEMA_PROBE)}",
        "migration_hint=" + shlex.quote(_MIGRATE_HINT if migrate else _UPDATE_HINT),
    ]
    if migrate:
        # Unique per invocation: migration apply/rollback never overwrite an
        # existing backup file, so a retry after failure cannot collide.
        lines += [
            f"backup_db={shlex.quote('/data/admin-v1-backup-' + stamp + '.db')}",
            f"recovery_db={shlex.quote('/data/admin-v2-recovery-' + stamp + '.db')}",
        ]
    lines += [_UPDATE_PREFLIGHT, _UPDATE_MIGRATE_CUTOVER if migrate else _UPDATE_CUTOVER]
    return "\n".join(lines)

_INSPECT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"status":{{json .State.Status}},"image":{{json .Config.Image}},'
    '"labels":{{json .Config.Labels}}}'
)


class AdminError(RuntimeError):
    """An actionable connection, deployment or inspection failure."""


class LegacySchemaError(AdminError):
    """Update's read-only probe found a v1 data volume; nothing was changed.

    Distinct type so the menu can offer the explicit Migrate & Update flow
    instead of only displaying the refusal.
    """


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
            error = AdminError(
                f"Remote command failed (exit {result.returncode}): {detail or 'no diagnostic'}"
            )
            # Callers branch on reserved remote exit statuses (e.g. the update
            # script's legacy-schema refusal) without parsing the message.
            error.exit_code = result.returncode
            raise error
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

    def install(self, source: Path, domain: str | None = None, *, public_http: bool = False) -> str:
        domain = validate_domain(domain)
        if type(public_http) is not bool or (public_http and domain):
            raise ValueError("Choose either public HTTP or an HTTPS domain.")
        proxy = bool(domain or public_http)
        archive = _source_archive(source)
        # Separate SSH call: apt/sudo must never consume the tar upload on stdin.
        self._run(["sh", "-c", bootstrap_script(domain)], timeout=1050)
        if self.discover():
            raise AdminError("Remote is already installed (possibly stopped); no containers or data were replaced.")
        # Only this invocation's mktemp directory is removed. Failed containers and
        # volumes are retained for diagnosis; never delete existing user data (§16.4).
        health = _HEALTH_PY
        script = "\n".join([
            "set -eu", "umask 077", DOCKER_HELPER,
            port_check(domain, public_http=public_http),
            "names=$(docker_cmd ps -a --format '{{.Names}}')",
            *[f"if printf '%s\\n' \"$names\" | grep -Fx {shlex.quote(name)}; then fail 'Existing container found; nothing replaced.'; fi"
              for name in ([CONTAINER, CADDY_CONTAINER] if proxy else [CONTAINER])],
            "volumes=$(docker_cmd volume ls --format '{{.Name}}')",
            *[f"if printf '%s\\n' \"$volumes\" | grep -Fx {shlex.quote(volume)}; then fail 'Existing data volume found; refusing to reuse or replace it.'; fi"
              for volume in ([VOLUME, CADDY_DATA, CADDY_CONFIG] if proxy else [VOLUME])],
            *([f"if run_root test -e {CADDY_DIRECTORY} || run_root test -L {CADDY_DIRECTORY}; then fail 'Existing Caddy configuration found; nothing replaced.'; fi"] if proxy else []),
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
            tls_script(domain) if domain else http_script() if public_http else "",
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
        if public_http:
            return output + self._http_instructions()
        return output + (
            "\nInstalled on 127.0.0.1:8787 only. Use an SSH tunnel (-L 8787:127.0.0.1:8787) "
            "or configure a TLS reverse proxy before external access; no firewall ports were opened."
        )

    def _http_instructions(self) -> str:
        host = self.connection.host
        if ":" in host:
            host = f"[{host}]"
        return (f"\nStudio / Android URL: http://{host}\n"
                "Users need only this URL and their personal key, not SSH. "
                "HTTP sends keys and data without encryption. "
                "The local proxy was checked; allow TCP 80 in any external/cloud firewall.")

    def publish_http(self, installation: Installation) -> str:
        """Expose an existing loopback deployment without replacing its API/data (§16.2)."""
        container = self._container(installation)
        if installation.status != "running":
            raise AdminError("Remote must be running before enabling public HTTP.")
        script = "\n".join([
            "set -eu", "umask 077", DOCKER_HELPER,
            f"[ \"$(docker_cmd inspect --format '{{{{.HostConfig.NetworkMode}}}}' {container})\" = host ] || fail 'Public HTTP requires a Remote Admin host-network deployment; nothing changed.'",
            f"docker_cmd exec {container} python -c {shlex.quote(_UPDATE_PROBE)}",
            "names=$(docker_cmd ps -a --format '{{.Names}}')",
            f"if printf '%s\\n' \"$names\" | grep -Fx {CADDY_CONTAINER}; then fail 'A Remote proxy already exists; its configuration was preserved.'; fi",
            f"if run_root test -e {CADDY_DIRECTORY} || run_root test -L {CADDY_DIRECTORY}; then fail 'Existing Caddy configuration found; nothing replaced.'; fi",
            "volumes=$(docker_cmd volume ls --format '{{.Name}}')",
            *[f"if printf '%s\\n' \"$volumes\" | grep -Fx {volume}; then fail 'Existing Caddy data volume found; nothing replaced.'; fi"
              for volume in (CADDY_DATA, CADDY_CONFIG)],
            'listeners=$(run_root ss -H -ltn)',
            "if printf '%s\\n' \"$listeners\" | awk '$4 ~ /:80$/ {found=1} END {exit !found}'; then fail 'TCP port 80 is occupied; existing service was not changed.'; fi",
            http_script(),
        ])
        output = self._run(["timeout", "--signal=TERM", "--kill-after=10", "600", "sh", "-c", script], timeout=630)
        return output + self._http_instructions()

    def version(self, installation: Installation) -> str | None:
        """Best-effort package version of a running installation; never fatal.

        The status box must render even when the container is stopped, the RPC
        generation predates the probe, or Docker access is degraded — so every
        failure here is 'unknown', not an error the caller has to handle.
        """
        if installation.status != "running":
            return None
        try:
            output = self._docker([
                "exec", self._container(installation), "python", "-c",
                "import voxarium_studio_remote as module; print(module.__version__)",
            ])
        except AdminError:
            return None
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return None
        candidate = lines[-1]
        return candidate if _VERSION.fullmatch(candidate) else None

    def update(self, source: Path) -> str:
        """Build and health-check a replacement for an Admin-managed installation.

        Never migrates data: a v1 volume is refused with LegacySchemaError
        before anything is changed, so the UI can offer migrate_update().
        """
        return self._deploy_update(source, migrate=False)

    def migrate_update(self, source: Path) -> str:
        """Explicit offline v1→v2 migration combined with the update.

        The service is stopped only after the replacement image is built and
        verified to face a v1 volume. The migration keeps a validated v1
        backup inside the volume; a failed migration or health check restores
        that backup and restarts the previous container. The volume itself is
        never removed or recreated.
        """
        return self._deploy_update(source, migrate=True)

    def _deploy_update(self, source: Path, *, migrate: bool) -> str:
        archive = _source_archive(source)
        installations = self.discover()
        if not installations:
            raise AdminError("Remote is not installed; choose Install to provision the server.")
        # One installation is assumed; prefer the canonical container name.
        target = next((item for item in installations if item.name == CONTAINER), installations[0])
        container = self._container(target)
        script = _update_script(container, uuid4().hex, migrate=migrate)
        try:
            output = self._run(
                ["timeout", "--signal=TERM", "--kill-after=10", "900", "sh", "-c", script],
                data=archive, timeout=930,
            )
        except AdminError as exc:
            if not migrate and getattr(exc, "exit_code", None) == _LEGACY_SCHEMA_EXIT:
                raise LegacySchemaError(str(exc)) from exc
            subject = "Migrate & update did not complete" if migrate else "Update did not complete"
            error = AdminError(
                subject + ". Use Find Remote and verify service health before retrying.\n"
                + str(exc)
            )
            error.exit_code = getattr(exc, "exit_code", None)
            raise error from exc
        if migrate:
            return output + "\nThe container name, data volume and HTTPS configuration were preserved."
        return output + "\nThe data volume, container name and HTTPS configuration were preserved."

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
