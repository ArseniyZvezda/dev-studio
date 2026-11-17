"""Service-owner OS exit evidence (studio-design §6/S3, VS-120).

Three synthetic boundaries only, not power-loss or server replay testing. Each
child owns the real unmounted service and journal; no runtime/API is started.
Run with an isolated home/temp and --noconftest; only fake transport is used.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import importlib
from importlib.machinery import ModuleSpec
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import types
from urllib.parse import unquote, urlsplit

import pytest


PROFILE = "00000000-0000-4000-8000-000000000120"
URL = "https://service-owner.invalid/deployment/prefix"
OP = "Synthetic.Create-120"
RAW_LABEL = "  Cafe\u0301 / Process  "
WIRE_LABEL = "Caf\u00e9 / Process"
KEY = "SYNTHETIC-PROCESS-KEY-120"
EXIT_CODE = 120
ZERO_IO = dict.fromkeys(("dns", "socket", "listeners", "store",
                         "outside_sqlite", "outside_file", "descendant"), 0)
HOME_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
             "VOXARIUM_STUDIO_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
             "PYTHONPYCACHEPREFIX", "CODEX_HOME", "CLAUDE_CONFIG_DIR")


def isolated_env(root):
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("VOXARIUM_STUDIO_REMOTE_") or name in ("PYTHONPATH", "PYTHONHOME"):
            del env[name]
    for name in HOME_VARS:
        target = root / name
        target.mkdir()
        env[name] = str(target)
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYDANTIC_DISABLE_PLUGINS="__all__")
    return env


@contextmanager
def guarded_io(root, journal_path, *, allow_spawn=False):
    """Same counters in parent/child, including swallowed forbidden attempts."""
    counts = ZERO_IO.copy()
    active = True

    def denied(kind):
        def fail(*_args, **_kwargs):
            counts[kind] += 1
            raise AssertionError("Synthetic service owner blocked " + kind)
        return fail

    def inside(path):
        return path.resolve().is_relative_to(root.resolve())

    def audit(event, args):
        if not active:
            return
        if event.startswith("socket."):
            denied("dns" if "get" in event else "socket")()
        elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty"}:
            if not allow_spawn:
                denied("descendant")()
        elif event == "sqlite3.connect":
            raw = str(args[0])
            if raw.startswith("file:"):
                decoded = unquote(urlsplit(raw).path)
                target = Path(decoded.lstrip("/") if os.name == "nt" else decoded)
            else:
                target = Path(raw)
            if target.resolve() != journal_path.resolve():
                denied("outside_sqlite")()
        elif event == "open" and not isinstance(args[0], int):
            target = Path(os.fsdecode(args[0]))
            flags = args[2]
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            # Imports may read source/bytecode; all data and writes stay synthetic.
            if not inside(target) and (writing or target.suffix.lower() not in {".py", ".pyc", ".pyd", ".dll"}):
                denied("outside_file")()

    sys.addaudithook(audit)
    with pytest.MonkeyPatch.context() as patch:
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
            patch.setattr(socket, name, denied("dns"))
        for name in ("bind", "listen"):
            patch.setattr(socket.socket, name, denied("listeners"))
        for name in ("connect", "connect_ex"):
            patch.setattr(socket.socket, name, denied("socket"))
        for name in ("socket", "socketpair", "create_connection", "create_server"):
            patch.setattr(socket, name, denied("socket"))
        try:
            yield counts, patch, denied
        finally:
            active = False
            assert counts == ZERO_IO


def load_sources(patch, denied):
    if "voxarium_studio.sync" not in sys.modules:
        unmounted_sync(Path(__file__).resolve().parents[1])
    from voxarium_studio.core import application_config, paths
    from voxarium_studio.sync import credentials, profiles

    for owner, names in (
        (paths, ("user_config_dir",)),
        (application_config, ("user_config_dir", "application_config_file",
                              "_load_application_config_strict", "_write_application_profiles",
                              "load_application_config", "save_application_config",
                              "application_config_transaction")),
        (credentials, ("user_config_dir", "keys_file", "load_profile_token", "save_profile_token",
                       "load_token", "save_token", "import_legacy_token")),
        (profiles, ("application_config_file", "_load_application_config_strict",
                    "_write_application_config", "application_config_transaction")),
    ):
        for name in names:
            patch.setattr(owner, name, denied("store"))
    for name in ("get", "list", "load_token"):
        patch.setattr(profiles.RemoteProfiles, name, denied("store"))
    return (application_config.RemoteServerProfile,
            importlib.import_module("voxarium_studio.sync.catalog"),
            importlib.import_module("voxarium_studio.sync.catalog_intents"),
            importlib.import_module("voxarium_studio.sync.catalog_create_service"))


def original(intents):
    return intents.CatalogIntent(OP, PROFILE, URL, 17, "process-120", RAW_LABEL)


def build(sources, journal, calls, *, cold=False, boundary=None):
    Profile, catalog, _, service = sources

    class Profiles:
        def get(self, profile_id):
            assert profile_id == PROFILE
            return Profile(profile_id=PROFILE, name="Synthetic profile", url=URL, revision=17)

    def credential(profile_id, url):
        calls["credentials"] += 1
        assert not cold, "Cold owner attempted credential lookup"
        assert (profile_id, url) == (PROFILE, URL)
        return KEY

    class Transport:
        async def request(self, method, url, *, headers, params, json, follow_redirects):
            calls["transport"] += 1
            assert not cold, "Cold owner attempted transport"
            assert not params and follow_redirects is False
            if method == "POST":
                calls["posts"] += 1
                assert url == URL + "/api/v2/projects"
                assert json == {"id": "process-120", "display_name": WIRE_LABEL}
                assert headers["Authorization"] == "Bearer " + KEY
                if boundary is not None:
                    boundary("post_before_result")
                body = {"protocol": 2, "project": {
                    "id": "process-120", "display_name": WIRE_LABEL,
                    "storage_epoch": "0123456789abcdef0123456789abcdef",
                    "metadata_revision": 1, "created_at": "2026-11-15T12:00:00Z"}}
                status = 201
            elif url == URL + "/healthz":
                assert method == "GET" and json is None and "Authorization" not in headers
                body = {"service": "voxarium_studio_remote", "version": "synthetic-v2",
                        "protocol": 2, "supported_protocols": [2], "catalog_api": 2}
                status = 200
            else:
                assert method == "GET" and url == URL + "/api/v2/me" and json is None
                assert headers["Authorization"] == "Bearer " + KEY
                body = {"protocol": 2, "catalog_api": 2,
                        "capabilities": ["project_catalog", "explicit_project_create", "strict_connect"],
                        "user": {"id": "synthetic-user", "display_name": "Synthetic user", "color": "#3e63dd"}}
                status = 200
            await asyncio.sleep(0)
            return catalog.CatalogResponse(status, body)

    return service.CatalogCreateService(profiles=Profiles(), credentials=credential,
                                        transport=Transport(), mode="durable", journal=journal)


async def create(service, *, label=RAW_LABEL):
    return await service.create_project(PROFILE, expected_revision=17, id="process-120",
                                        display_name=label, operation_id=OP)


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """No wakeup sockets or cross-thread scheduling, as in S3 service tests."""
    def _make_self_pipe(self):
        self._ssock = None
        self._csock = types.SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("Synthetic loop forbids cross-thread I/O")

    def _loop_self_reading(self, *_args):
        pass


def run(command):
    with asyncio.Runner(loop_factory=SocketFreeLoop) as runner:
        return runner.run(command)


def unmounted_sync(backend):
    # Avoid sync.__init__'s eager RemoteSync import, using the real source path.
    import voxarium_studio
    package = types.ModuleType("voxarium_studio.sync")
    package.__path__ = [str(backend / "voxarium_studio" / "sync")]
    package.__package__ = "voxarium_studio.sync"
    package.__spec__ = ModuleSpec(package.__package__, loader=None, is_package=True)
    sys.modules[package.__package__] = package
    voxarium_studio.sync = package


def child_owner(backend, root, phase):
    sys.path.insert(0, str(backend))
    unmounted_sync(backend)
    path = root / "intent.sqlite3"
    with guarded_io(root, path) as (counts, patch, denied):
        sources = load_sources(patch, denied)
        intents = sources[2]
        calls = dict(credentials=0, transport=0, posts=0)

        def boundary(reached):
            if reached != phase:
                return
            record = intents.CatalogIntentJournal(path).lookup(OP)
            report = dict(boundary=reached, pid=os.getpid(), counts=counts,
                          calls=calls, record=asdict(record))
            with (root / "boundary.json").open("w", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=True)
                output.flush()
                os.fsync(output.fileno())
            # Deliberately bypass coroutine/finally cleanup in the real owner.
            os._exit(EXIT_CODE)

        class ExitJournal(intents.CatalogIntentJournal):
            def reserve(self, intent):
                result = super().reserve(intent)
                assert result.post_permitted is True
                boundary("reservation_committed")
                return result

            def record_state(self, intent, *, state):
                assert state == "created"
                result = super().record_state(intent, state=state)
                boundary("result_committed")
                return result

        owner = build(sources, ExitJournal(path), calls, boundary=boundary)
        run(create(owner))
        raise AssertionError("Service owner missed requested exit boundary")


def exit_owner(root, phase):
    env = isolated_env(root)
    backend = Path(__file__).resolve().parents[1]
    # Windows venv python.exe can be a redirector with a second PID. Launch the
    # actual interpreter and inject only this environment's dependency directory,
    # so kill/wait owns the service PID itself, not a disposable launcher.
    executable = getattr(sys, "_base_executable", sys.executable)
    env["PYTHONPATH"] = str(Path(pytest.__file__).resolve().parents[1])
    command = [executable, "-B", "-s", "-S", str(Path(__file__).resolve()),
               "--owner", str(backend), str(root), phase]
    (root / "stdin.empty").write_bytes(b"")
    with (root / "child.log").open("wb") as output, (root / "stdin.empty").open("rb") as empty:
        process = subprocess.Popen(command, cwd=root, env=env, stdin=empty,
                                   stdout=output, stderr=subprocess.STDOUT)
    try:
        code = process.wait(timeout=30)
    finally:
        # Also reap on timeout, failed assertions and interruptions. No descendants
        # are launched by the child: one PID is the whole disposable owner.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    assert code == EXIT_CODE, (root / "child.log").read_text(encoding="utf-8", errors="replace")
    assert process.returncode == EXIT_CODE
    report = json.loads((root / "boundary.json").read_text(encoding="utf-8"))
    assert report["pid"] == process.pid and report["pid"] != os.getpid()
    return report


@pytest.mark.parametrize("phase,state,transport,posts", [
    ("reservation_committed", "reserved", 0, 0),
    ("post_before_result", "reserved", 3, 1),
    ("result_committed", "created", 3, 1),
])
def test_service_owner_exit_cannot_authorize_cold_create(tmp_path, monkeypatch, phase, state, transport, posts):
    root = tmp_path.resolve()
    # Parent data providers never consult the real home, even under ordinary pytest.
    for name, value in isolated_env(root).items():
        if name in HOME_VARS or name == "PYDANTIC_DISABLE_PLUGINS":
            monkeypatch.setenv(name, value)
    for name in list(os.environ):
        if name.startswith("VOXARIUM_STUDIO_REMOTE_"):
            monkeypatch.delenv(name)
    child = root / "owner"
    child.mkdir()
    path = child / "intent.sqlite3"
    with guarded_io(root, path, allow_spawn=True) as (counts, patch, denied):
        sources = load_sources(patch, denied)
        report = exit_owner(child, phase)
        assert report["boundary"] == phase
        assert report["counts"] == ZERO_IO
        assert report["calls"]["transport"] == transport
        assert report["calls"]["posts"] == posts
        assert report["calls"]["credentials"] >= 1
        catalog, intents = sources[1:3]
        expected = original(intents)
        assert report["record"] == {"intent": asdict(expected), "state": state}
        journal = intents.CatalogIntentJournal(path)
        before = journal.lookup(OP)
        assert before.intent == expected and before.state == state
        assert journal.reserve(expected).post_permitted is False
        calls = dict(credentials=0, transport=0, posts=0)
        # Fresh owners exercise BOTH cold entry points first, not merely one
        # already-latched failure in a shared service instance.
        for first in ("create", "recover"):
            cold = build(sources, intents.CatalogIntentJournal(path), calls, cold=True)
            for action in (first, "recover" if first == "create" else "create", first):
                command = create(cold) if action == "create" else cold.recover_create(
                    PROFILE, OP, expected_revision=17)
                with pytest.raises(catalog.CatalogError, match="^credential_binding_unproven$") as caught:
                    run(command)
                assert caught.value.code == "credential_binding_unproven"
            # Equivalent wire label is NOT the exact original raw intent.
            with pytest.raises(catalog.CatalogError, match="^operation_conflict$"):
                run(create(cold, label=WIRE_LABEL))
        assert calls == dict(credentials=0, transport=0, posts=0)
        assert counts == ZERO_IO
        assert intents.CatalogIntentJournal(path).lookup(OP) == before
        assert intents.CatalogIntentJournal(path).reserve(expected).post_permitted is False
        for candidate in child.glob("intent.sqlite3*"):
            assert KEY.encode() not in candidate.read_bytes()
        # Compact evidence for the task's targeted-run log/card, not secrets.
        print(json.dumps(dict(phase=phase, state=state, child_calls=report["calls"],
                              child_io=report["counts"], cold_calls=calls, parent_io=counts)))


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--owner"
    child_owner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
