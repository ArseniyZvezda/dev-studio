"""Shell-level harness for the generated Update / Migrate & Update scripts.

Unlike test_admin_transport.py (which inspects the script as text and mocks
SSH), every test here runs `_update_script()` for real under Git Bash against
a stateful fake `docker` executable on PATH. The fake keeps containers, the
image build result and the data-volume schema (v1|v2) in a FAKE_STATE
directory, logs every invocation to calls.log, and honours failure switches
(APPLY_FAIL, PROBE_FAIL, ROLLBACK_FAIL, BUILD_FAIL) and flags
(schema_missing, crash_schema) so the cutover, migration and cleanup-trap
paths can be exercised end to end.

The generated script is written to a file instead of passed via `bash -c`:
the migrate script is ~14 KB and Git Bash truncates -c arguments above
~8-10 KB on Windows. Semantics are otherwise identical (`sh -c script` with
the build-context tar on stdin becomes `bash script.sh` with the same stdin).
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import tarfile
from typing import NamedTuple

import pytest

from voxarium_studio_remote.admin_transport import _update_script

GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")

OLD_ID = "a" * 64
NEW_ID = "c" * 64
NAME = "voxarium-studio-remote"
BACKUP = NAME + ".admin-previous"
STAGING = NAME + ".admin-candidate"
VOLUME = "voxarium-studio-remote-data"
IMAGE = "voxarium-studio-remote:admin"
STAMP = "harness0stamp"

pytestmark = pytest.mark.skipif(
    not GIT_BASH.is_file(),
    reason="requires Git Bash at C:\\Program Files\\Git\\bin\\bash.exe",
)

# Stateful fake docker. Layout under $FAKE_STATE:
#   schema                 "v1" or "v2" (the data volume's schema)
#   calls.log              one "docker <args>" line per invocation
#   c/<id>/{name,status,startedat,gen}   containers
#   i/built_id             id produced by the last successful `build`
#   flags: schema_missing, crash_schema, backup_created, recovery_created
# Env switches: APPLY_FAIL, PROBE_FAIL, ROLLBACK_FAIL, BUILD_FAIL.
FAKE_DOCKER = r'''#!/bin/bash
set -u
S="${FAKE_STATE:?FAKE_STATE must point at the harness state directory}"
CDIR="$S/c"
IDIR="$S/i"
printf 'docker %s\n' "$*" >>"$S/calls.log"

unmatched() { printf 'UNMATCHED docker %s\n' "$*" >>"$S/calls.log"; exit 9; }
err() { printf '%s\n' "$*" >&2; }

OLD_IMAGE_ID='sha256:1111111111111111111111111111111111111111111111111111111111111111'
NEW_IMAGE_ID='sha256:2222222222222222222222222222222222222222222222222222222222222222'
NEW_CONTAINER_ID='cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
# Byte-identical for every container: the cutover compares the normalized
# HostConfig/Labels of the original and the candidate before proceeding.
SETTINGS='{"Binds":["voxarium-studio-remote-data:/data"],"NetworkMode":"host","RestartPolicy":{"MaximumRetryCount":0,"Name":"unless-stopped"}} {"io.voxarium.studio.remote":"true"}'

find_container() {
  key="${1#/}"
  if [ -d "$CDIR/$key" ]; then printf '%s' "$key"; return 0; fi
  for d in "$CDIR"/*; do
    [ -d "$d" ] || continue
    if [ -f "$d/name" ] && [ "$(cat "$d/name")" = "$key" ]; then
      printf '%s' "${d##*/}"
      return 0
    fi
  done
  return 1
}

config_out() { printf 'null  \nPATH=/usr/bin\n'; }

cmd="${1:-}"
shift 2>/dev/null || true
case "$cmd" in
info)
  exit 0
  ;;
ps)
  fmt=''
  while [ $# -gt 0 ]; do
    case "$1" in --format) fmt="${2:-}"; shift 2;; *) shift;; esac
  done
  for d in "$CDIR"/*; do
    [ -d "$d" ] || continue
    case "$fmt" in
      '{{.ID}}') printf '%s\n' "${d##*/}";;
      '{{.Names}}') printf '%s\n' "$(cat "$d/name")";;
      *) unmatched "ps --format $fmt";;
    esac
  done
  exit 0
  ;;
inspect)
  fmt=''
  if [ "${1:-}" = '--format' ]; then fmt="${2:-}"; shift 2; fi
  target="${1:-}"
  if ! id=$(find_container "$target"); then
    err "Error: No such object: $target"
    exit 1
  fi
  c="$CDIR/$id"
  status=$(cat "$c/status")
  name=$(cat "$c/name")
  if [ -z "$fmt" ]; then printf '{"Id":"%s"}\n' "$id"; exit 0; fi
  case "$fmt" in
    '{{.Name}}') printf '/%s\n' "$name";;
    '{{index .Config.Labels "com.docker.compose.project"}}') printf '\n';;
    '{{.HostConfig.NetworkMode}}') printf 'host\n';;
    '{{index .Config.Labels "io.voxarium.studio.remote"}}') printf 'true\n';;
    '{{if eq (len .Mounts) 1}}{{with index .Mounts 0}}{{if and (eq .Type "volume") (eq .Destination "/data") .RW}}{{.Name}}{{end}}{{end}}{{end}}') printf '%s\n' 'voxarium-studio-remote-data';;
    '{{with .HostConfig.RestartPolicy}}{{.Name}}{{if and (eq .Name "on-failure") (gt .MaximumRetryCount 0)}}:{{.MaximumRetryCount}}{{end}}{{end}}') printf 'unless-stopped\n';;
    '{{json .Config.Cmd}}') printf '%s\n' '["voxarium-studio-remote","serve","--host","127.0.0.1","--port","8787"]';;
    '{{json .Config.Entrypoint}} {{.Config.User}} {{.Config.WorkingDir}}{{range .Config.Env}}{{if ne (index (split . "=") 0) "VOXARIUM_REMOTE_AI_CONTROL"}}{{printf "\\n%s" .}}{{end}}{{end}}') config_out;;
    '{{range .Config.Env}}{{if eq (index (split . "=") 0) "VOXARIUM_REMOTE_AI_CONTROL"}}{{.}}{{end}}{{end}}') printf '\n';;
    '{{.State.Status}}') printf '%s\n' "$status";;
    '{{.State.Running}}') if [ "$status" = running ]; then printf 'true\n'; else printf 'false\n'; fi;;
    '{{.Id}}') printf '%s\n' "$id";;
    '{{.Image}}') printf '%s\n' "$OLD_IMAGE_ID";;
    '{{json .HostConfig}} {{json .Config.Labels}}') printf '%s\n' "$SETTINGS";;
    '{{if eq .State.Status "running"}}{{.State.StartedAt}}{{end}}') if [ "$status" = running ]; then printf '%s\n' "$(cat "$c/startedat")"; else printf '\n'; fi;;
    'Replacement state: {{.State.Status}}; exit code: {{.State.ExitCode}}; OOM killed: {{.State.OOMKilled}}.') printf 'Replacement state: %s; exit code: 1; OOM killed: false.\n' "$status";;
    *) unmatched "inspect --format $fmt";;
  esac
  exit 0
  ;;
create)
  cidfile=''
  cname=''
  while [ $# -gt 0 ]; do
    case "$1" in
      --cidfile) cidfile="${2:-}"; shift 2;;
      --name) cname="${2:-}"; shift 2;;
      --label|--restart|--network|--volume|--env) shift 2;;
      *) break;;
    esac
  done
  [ -n "$cname" ] || unmatched "create"
  mkdir -p "$CDIR/$NEW_CONTAINER_ID"
  printf '%s' "$cname" >"$CDIR/$NEW_CONTAINER_ID/name"
  printf '%s' 'created' >"$CDIR/$NEW_CONTAINER_ID/status"
  printf '' >"$CDIR/$NEW_CONTAINER_ID/startedat"
  printf '0' >"$CDIR/$NEW_CONTAINER_ID/gen"
  [ -z "$cidfile" ] || printf '%s\n' "$NEW_CONTAINER_ID" >"$cidfile"
  printf '%s\n' "$NEW_CONTAINER_ID"
  exit 0
  ;;
rename)
  if ! id=$(find_container "${1:-}"); then err "Error: No such container: ${1:-}"; exit 1; fi
  new="${2#/}"
  [ -n "$new" ] || { err "Error: rename requires a new name"; exit 1; }
  if find_container "$new" >/dev/null 2>&1; then
    err "Error: Conflict. The container name \"$new\" is already in use"
    exit 1
  fi
  printf '%s' "$new" >"$CDIR/$id/name"
  exit 0
  ;;
stop)
  while [ $# -gt 0 ]; do
    case "$1" in -t) shift 2;; --) shift;; *) break;; esac
  done
  if ! id=$(find_container "${1:-}"); then err "Error: No such container: ${1:-}"; exit 1; fi
  printf '%s' 'exited' >"$CDIR/$id/status"
  printf '' >"$CDIR/$id/startedat"
  exit 0
  ;;
start)
  if ! id=$(find_container "${1:-}"); then err "Error: No such container: ${1:-}"; exit 1; fi
  c="$CDIR/$id"
  if [ "$(cat "$c/status")" != running ]; then
    gen=$(( $(cat "$c/gen") + 1 ))
    printf '%s' "$gen" >"$c/gen"
    printf 'gen-%s' "$gen" >"$c/startedat"
    printf '%s' 'running' >"$c/status"
  fi
  printf '%s\n' "$(cat "$c/name")"
  exit 0
  ;;
rm)
  while [ $# -gt 0 ]; do
    case "$1" in -f|--) shift;; *) break;; esac
  done
  if ! id=$(find_container "${1:-}"); then err "Error: No such container: ${1:-}"; exit 1; fi
  rm -rf -- "$CDIR/$id"
  exit 0
  ;;
logs)
  while [ $# -gt 0 ]; do
    case "$1" in --tail) shift 2;; *) break;; esac
  done
  if [ -f "$S/crash_schema" ]; then
    printf '%s\n' 'SchemaError: Legacy database requires explicit offline migration'
  else
    printf '%s\n' 'remote: listening on 127.0.0.1:8787'
  fi
  exit 0
  ;;
build)
  tag=''
  while [ $# -gt 0 ]; do
    case "$1" in --tag) tag="${2:-}"; shift 2;; *) shift;; esac
  done
  printf '%s\n' 'DEPRECATED: The legacy builder is deprecated and will be removed in a future release.' >&2
  if [ -n "${BUILD_FAIL:-}" ]; then
    printf '%s\n' 'failed to solve: simulated builder failure' >&2
    exit 1
  fi
  mkdir -p "$IDIR"
  printf '%s\n' "$NEW_IMAGE_ID" >"$IDIR/built_id"
  printf '%s\n' "$tag" >"$IDIR/built_tag"
  printf 'Successfully built %s\n' "$NEW_IMAGE_ID"
  exit 0
  ;;
tag)
  dst="${2:-}"
  [ -n "$dst" ] || { err "Error: tag requires a source and a destination"; exit 1; }
  safe=$(printf '%s' "$dst" | tr ':' '=')
  mkdir -p "$IDIR"
  printf '%s\n' "$NEW_IMAGE_ID" >"$IDIR/$safe"
  exit 0
  ;;
image)
  sub="${1:-}"
  shift 2>/dev/null || true
  case "$sub" in
  inspect)
    fmt=''
    if [ "${1:-}" = '--format' ]; then fmt="${2:-}"; shift 2; fi
    case "$fmt" in
      '{{.Id}}')
        if [ ! -f "$IDIR/built_id" ]; then err "Error: No such image: ${1:-}"; exit 1; fi
        cat "$IDIR/built_id"
        exit 0;;
      '{{json .Config.Entrypoint}} {{.Config.User}} {{.Config.WorkingDir}}{{range .Config.Env}}{{if ne (index (split . "=") 0) "VOXARIUM_REMOTE_AI_CONTROL"}}{{printf "\\n%s" .}}{{end}}{{end}}')
        config_out
        exit 0;;
      *) unmatched "image inspect --format $fmt";;
    esac
    ;;
  rm)
    exit 0
    ;;
  ls)
    printf '%s\n' 'voxarium-studio-remote:admin'
    exit 0
    ;;
  *) unmatched "image $sub";;
  esac
  ;;
volume)
  sub="${1:-}"
  shift 2>/dev/null || true
  case "$sub" in
  inspect)
    fmt=''
    if [ "${1:-}" = '--format' ]; then fmt="${2:-}"; shift 2; fi
    case "$fmt" in
      '{{.Name}}') printf '%s\n' "${1:-}"; exit 0;;
      *) unmatched "volume inspect --format $fmt";;
    esac
    ;;
  ls)
    printf '%s\n' 'voxarium-studio-remote-data'
    exit 0
    ;;
  *) unmatched "volume $sub";;
  esac
  ;;
exec)
  if ! id=$(find_container "${1:-}"); then err "Error: No such container: ${1:-}"; exit 1; fi
  if [ "$(cat "$CDIR/$id/status")" = running ] && [ -z "${PROBE_FAIL:-}" ]; then
    exit 0
  fi
  exit 1
  ;;
run)
  entrypoint=''
  while [ $# -gt 0 ]; do
    case "$1" in
      --rm) shift;;
      --network|--mount|--volume) shift 2;;
      --entrypoint) entrypoint="${2:-}"; shift 2;;
      *) break;;
    esac
  done
  image="${1:-}"
  shift 2>/dev/null || true
  if [ "$entrypoint" = python ] && [ "${1:-}" = '-c' ]; then
    # Read-only schema probe inside the new image.
    if [ -f "$S/schema_missing" ]; then exit 1; fi
    if [ "$(cat "$S/schema")" = v1 ]; then printf 'legacy\n'; fi
    exit 0
  fi
  if [ "${1:-}" = voxarium-studio-remote ] && [ "${2:-}" = migration ]; then
    mode="${3:-}"
    if [ "$mode" = apply ]; then
      if [ -n "${APPLY_FAIL:-}" ]; then
        printf '%s\n' 'simulated failure: offline migration apply'
        exit 1
      fi
      printf 'v2' >"$S/schema"
      : >"$S/backup_created"
      printf '%s\n' 'Schema migrated from v1 to v2; the backup was validated.'
      exit 0
    fi
    if [ "$mode" = rollback ]; then
      if [ -n "${ROLLBACK_FAIL:-}" ]; then
        printf '%s\n' 'simulated failure: schema rollback'
        exit 1
      fi
      printf 'v1' >"$S/schema"
      : >"$S/recovery_created"
      printf '%s\n' 'Schema rolled back to v1 from the validated backup.'
      exit 0
    fi
  fi
  unmatched "run $image $*"
  ;;
esac
unmatched "$cmd $*"
'''

FAKE_SLEEP = "#!/bin/bash\nexit 0\n"


class Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def _context_archive() -> bytes:
    """The real build-context tar the script extracts from stdin."""
    buffer = io.BytesIO()
    files = [
        ("Dockerfile", b"FROM python:3.12-slim\n"),
        ("pyproject.toml", b"[project]\nname = \"voxarium-studio-remote\"\n"),
        ("README.md", b"# Voxarium Studio Remote\n"),
        ("voxarium_studio_remote/__init__.py", b"__version__ = \"0.0.0\"\n"),
    ]
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Harness:
    """One fake server: initial container state, fake docker, script runner."""

    def __init__(self, root: Path):
        self.root = root
        self.state = root / "state"
        self.bindir = root / "bin"
        (self.state / "c").mkdir(parents=True)
        (self.state / "i").mkdir(parents=True)
        self.bindir.mkdir()
        (self.state / "schema").write_bytes(b"v1")
        (self.state / "calls.log").write_bytes(b"")
        container = self.state / "c" / OLD_ID
        container.mkdir()
        # write_bytes everywhere: no CRLF translation, the fake compares raw.
        (container / "name").write_bytes(NAME.encode())
        (container / "status").write_bytes(b"running")
        (container / "startedat").write_bytes(b"gen-1")
        (container / "gen").write_bytes(b"1")
        (self.bindir / "docker").write_bytes(FAKE_DOCKER.encode("utf-8"))
        (self.bindir / "sleep").write_bytes(FAKE_SLEEP.encode("utf-8"))
        git_root = GIT_BASH.parent.parent
        self.env = dict(os.environ)
        # Minimal PATH: the fakes first, then Git's own coreutils. A real
        # docker.exe elsewhere on the machine must never be reachable.
        self.env["PATH"] = os.pathsep.join(
            [str(self.bindir), str(git_root / "usr" / "bin"), str(git_root / "bin")]
        )
        self.env["FAKE_STATE"] = str(self.state)
        self._archive = _context_archive()

    # ---- fake-state readers ------------------------------------------------

    def schema(self) -> str:
        return (self.state / "schema").read_text(encoding="ascii")

    def flag(self, name: str) -> bool:
        return (self.state / name).exists()

    def calls(self) -> str:
        return (self.state / "calls.log").read_text(encoding="utf-8", errors="replace")

    def container_exists(self, container_id: str) -> bool:
        return (self.state / "c" / container_id).is_dir()

    def container_name(self, container_id: str) -> str:
        return (self.state / "c" / container_id / "name").read_text(encoding="utf-8")

    def container_status(self, container_id: str) -> str:
        return (self.state / "c" / container_id / "status").read_text(encoding="ascii")

    def id_of(self, name: str) -> str | None:
        for directory in (self.state / "c").iterdir():
            if directory.is_dir() and (directory / "name").read_text(encoding="utf-8") == name:
                return directory.name
        return None

    # ---- script runner -----------------------------------------------------

    def deploy(self, *, migrate: bool, switches=(), flags=()) -> Result:
        for flag in flags:
            (self.state / flag).write_bytes(b"")
        env = dict(self.env)
        for switch in switches:
            env[switch] = "1"
        script = _update_script(OLD_ID, STAMP, migrate=migrate)
        path = self.root / "script.sh"
        path.write_bytes(script.encode("utf-8"))
        try:
            proc = subprocess.run(
                [str(GIT_BASH), str(path)],
                input=self._archive,
                capture_output=True,
                env=env,
                timeout=180,
                cwd=str(self.root),
            )
        except subprocess.TimeoutExpired:  # pragma: no cover - harness failure
            pytest.fail(f"the generated script did not finish in 180 s (migrate={migrate})")
        return Result(
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# ---------------------------------------------------------------------------
# Migrate & Update
# ---------------------------------------------------------------------------

def test_migrate_success_from_v1(harness):
    result = harness.deploy(migrate=True)
    assert result.returncode == 0, result.stderr
    assert "Remote updated and migrated to schema v2" in result.stdout
    assert "admin-v1-backup-" in result.stdout
    assert harness.schema() == "v2"
    assert harness.flag("backup_created")
    # The original container is gone; the candidate owns the original name.
    assert not harness.container_exists(OLD_ID)
    assert harness.container_name(NEW_ID) == NAME
    assert harness.container_status(NEW_ID) == "running"
    assert harness.id_of(BACKUP) is None
    calls = harness.calls()
    assert "migration apply" in calls
    assert "migration rollback" not in calls
    # The data volume is never removed or recreated.
    assert "volume rm" not in calls
    assert "volume create" not in calls


def test_migrate_apply_failure_restores_previous(harness):
    result = harness.deploy(migrate=True, switches=("APPLY_FAIL",))
    assert result.returncode != 0
    assert "Offline migration failed" in result.stderr
    # apply is atomic: the volume is still v1 and no rollback is attempted.
    assert harness.schema() == "v1"
    assert "migration rollback" not in harness.calls()
    # The previous container is running under the original name again.
    assert harness.container_name(OLD_ID) == NAME
    assert harness.container_status(OLD_ID) == "running"
    assert not harness.container_exists(NEW_ID)


def test_migrate_probe_failure_rolls_back_schema(harness):
    result = harness.deploy(migrate=True, switches=("PROBE_FAIL",))
    assert result.returncode != 0
    assert "did not become healthy after the migration" in result.stderr
    assert "v1 backup was restored" in result.stderr
    assert harness.schema() == "v1"
    assert harness.flag("recovery_created")
    assert "migration rollback" in harness.calls()
    assert not harness.container_exists(NEW_ID)
    # Required end state: the v1 container runs again under the original name.
    assert harness.container_name(OLD_ID) == NAME
    assert harness.container_status(OLD_ID) == "running"


def test_migrate_probe_and_rollback_failure_is_critical(harness):
    result = harness.deploy(migrate=True, switches=("PROBE_FAIL", "ROLLBACK_FAIL"))
    assert result.returncode != 0
    assert "CRITICAL" in result.stderr
    assert "must NOT be started" in result.stderr
    # The v1 container must never face v2 data: it stays stopped, renamed.
    assert harness.schema() == "v2"
    assert harness.container_name(OLD_ID) == BACKUP
    assert harness.container_status(OLD_ID) == "exited"
    assert harness.id_of(NAME) is None
    assert not harness.container_exists(NEW_ID)


def test_migrate_refuses_already_migrated_volume(harness):
    (harness.state / "schema").write_bytes(b"v2")
    result = harness.deploy(migrate=True)
    assert result.returncode != 0
    assert "normal Update instead" in result.stderr
    # Nothing was changed.
    assert harness.schema() == "v2"
    assert harness.container_name(OLD_ID) == NAME
    assert harness.container_status(OLD_ID) == "running"
    assert not harness.container_exists(NEW_ID)
    assert "migration apply" not in harness.calls()


def test_migrate_requires_readable_schema(harness):
    result = harness.deploy(migrate=True, flags=("schema_missing",))
    assert result.returncode != 0
    assert "Could not verify the data schema" in result.stderr
    # Nothing was changed.
    assert harness.container_name(OLD_ID) == NAME
    assert harness.container_status(OLD_ID) == "running"
    assert harness.schema() == "v1"
    assert not harness.container_exists(NEW_ID)


# ---------------------------------------------------------------------------
# Plain Update
# ---------------------------------------------------------------------------

def test_update_refuses_v1_volume_with_reserved_exit_code(harness):
    result = harness.deploy(migrate=False)
    assert result.returncode == 3, result.stderr
    assert "legacy v1 schema" in result.stderr
    # Nothing was changed.
    assert harness.container_name(OLD_ID) == NAME
    assert harness.container_status(OLD_ID) == "running"
    assert harness.schema() == "v1"
    assert not harness.container_exists(NEW_ID)
    assert "rename" not in harness.calls()


def test_update_v2_happy_path(harness):
    (harness.state / "schema").write_bytes(b"v2")
    result = harness.deploy(migrate=False)
    assert result.returncode == 0, result.stderr
    assert "Remote updated: container" in result.stdout
    assert not harness.container_exists(OLD_ID)
    assert harness.container_name(NEW_ID) == NAME
    assert harness.container_status(NEW_ID) == "running"
    calls = harness.calls()
    assert "volume rm" not in calls
    assert "volume create" not in calls


def test_update_build_failure_leaves_container_untouched(harness):
    result = harness.deploy(migrate=False, switches=("BUILD_FAIL",))
    assert result.returncode != 0
    assert "Docker image build failed" in result.stderr
    # The build happens long before any cutover step.
    assert harness.container_name(OLD_ID) == NAME
    assert harness.container_status(OLD_ID) == "running"
    assert not harness.container_exists(NEW_ID)
    calls = harness.calls()
    assert "docker create" not in calls
    assert "docker rename" not in calls
