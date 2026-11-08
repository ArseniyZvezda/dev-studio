"""The Unreal plugin, without Unreal.

The editor and build tools are exercised through local substitutes:

* **The transport.** A fake editor speaks Unreal's remote-execution protocol — multicast
  ping/pong, connect-back, command, command_result — and a stub executable stands in for
  the headless route. Both are exercised end to end.
* **The generated script.** The fake editor *runs* it against a stub ``unreal`` module,
  so the calls it makes to ``AssetImportTask`` and ``AssetToolsHelpers`` are real calls
  with real arguments, and the result marker round-trips.
* **The build.** Stand-ins for UnrealBuildTool and AutomationTool receive the tool
  arguments, archive a game and its content, and exercise error reporting and cancellation.

What is *not* covered is Unreal's own behaviour: whether a given engine version accepts
these properties, and whether the import factory does what the name suggests. That needs
an engine.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import socket
import struct
import sys
import textwrap
import threading
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from voxarium_studio.engine.executors import ExecutionRequest, execute
from voxarium_studio.plugins.api import NodeContext
from voxarium_studio.plugins.registry import Registry

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins_builtin" / "unreal"
NODES = PLUGIN_ROOT / "nodes"

# The multicast port the fake editor and the client meet on. Deliberately not Unreal's
# 6766, so a real editor on the machine cannot answer the test.
#
# Each xdist worker gets its own pair, ten apart. Sharing the multicast port would be the
# real damage — the group is machine-wide, so two workers on one port would answer each
# other's discovery pings and both would see two editors where the test asserts one.
_WORKER = int("".join(c for c in os.environ.get("PYTEST_XDIST_WORKER", "0") if c.isdigit()) or 0)
TEST_PORT = 16700 + _WORKER * 10
COMMAND_PORT = TEST_PORT + 1

# `discover_editors` has no early exit — it collects for the whole window, because the
# question it answers is "which editors are there", not "is there one". So the window is
# what each of these tests costs, and over loopback a stub answers in microseconds.
DISCOVERY_TIMEOUT = 0.3


def load_module(name: str):
    """Import a node script the way the engine does — with its own directory on the path."""
    if str(NODES) not in sys.path:
        sys.path.insert(0, str(NODES))
    spec = importlib.util.spec_from_file_location(f"voxarium_studio_test_{name}", NODES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: `@dataclass` resolves annotations through
    # `sys.modules[cls.__module__]`, which is not there yet during exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ue = load_module("_ue")
import_asset = load_module("import_asset")


@pytest.fixture
def settings(tmp_path: Path) -> "ue.UnrealSettings":
    return ue.UnrealSettings(
        project=str(tmp_path / "Game.uproject"),
        transport="remote",
        multicast_port=TEST_PORT,
        command_port=COMMAND_PORT,
        discovery_timeout=DISCOVERY_TIMEOUT,
    )


# --------------------------------------------------------------------------- fake editor


STUB_UNREAL = '''
"""Enough of Unreal's API surface for the generated script to run against."""
CALLS = []


class _Property:
    def __init__(self):
        self._values = {}

    def set_editor_property(self, name, value):
        self._values[name] = value

    def get_editor_property(self, name):
        return self._values.get(name)


class AssetImportTask(_Property):
    def __init__(self):
        super().__init__()
        CALLS.append(("AssetImportTask", self))


class FbxImportUI(_Property):
    pass


class _FBXImportType:
    FBXIT_STATIC_MESH = "static"
    FBXIT_SKELETAL_MESH = "skeletal"


FBXImportType = _FBXImportType()


class _AssetTools:
    def import_asset_tasks(self, tasks):
        for task in tasks:
            CALLS.append(("import_asset_tasks", task))
            name = task.get_editor_property("destination_name") or "Imported"
            task.set_editor_property(
                "imported_object_paths",
                ["%s/%s" % (task.get_editor_property("destination_path"), name)],
            )


class AssetToolsHelpers:
    @staticmethod
    def get_asset_tools():
        return _AssetTools()


class EditorAssetLibrary:
    @staticmethod
    def does_asset_exist(path):
        return False


def log_warning(message):
    print("Warning: %s" % message)


def log_error(message):
    print("Error: %s" % message)
'''


class FakeEditor:
    """Answers discovery, dials back, runs the script it is sent, reports the log."""

    def __init__(self, tmp_path: Path, *, port: int = TEST_PORT, fail: bool = False) -> None:
        self.node_id = str(uuid.uuid4())
        self.port = port
        self.fail = fail
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir(exist_ok=True)
        (self.stub_dir / "unreal.py").write_text(STUB_UNREAL, encoding="utf-8")
        self.ran_script: str | None = None
        self._stop = threading.Event()
        self._bound = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeEditor":
        self._thread.start()
        # The ping goes out the moment this returns, so the socket has to be joined to the
        # group by then. Waiting for the bind says that; a fixed sleep only guessed it.
        assert self._bound.wait(timeout=5), "the fake editor never bound its socket"
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            struct.pack("4s4s", socket.inet_aton("239.0.0.1"), socket.inet_aton("0.0.0.0")),
        )
        sock.settimeout(0.2)
        return sock

    def _serve(self) -> None:
        sock = self._socket()
        self._bound.set()
        try:
            while not self._stop.is_set():
                try:
                    raw, _ = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                message = ue._decode(raw)
                if message is None or message.get("source") == self.node_id:
                    continue
                if message.get("type") == ue.TYPE_PING:
                    sock.sendto(
                        ue._encode(
                            self.node_id,
                            ue.TYPE_PONG,
                            dest=message["source"],
                            data={
                                "engine_version": "5.4.0",
                                "project_name": "FakeGame",
                                "machine": "test",
                            },
                        ),
                        ("239.0.0.1", self.port),
                    )
                elif message.get("type") == ue.TYPE_OPEN_CONNECTION:
                    data = message.get("data") or {}
                    threading.Thread(
                        target=self._command_session,
                        args=(data.get("command_ip"), data.get("command_port")),
                        daemon=True,
                    ).start()
        finally:
            sock.close()

    def _command_session(self, host: str, port: int) -> None:
        try:
            connection = socket.create_connection((host, port), timeout=5)
        except OSError:
            return
        try:
            connection.settimeout(10)
            buffer = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                message = ue._decode(buffer)
                if message is not None and message.get("type") == ue.TYPE_COMMAND:
                    break

            script_path = (message.get("data") or {}).get("command")
            self.ran_script = Path(script_path).read_text(encoding="utf-8")
            output, success = self._execute(script_path)
            connection.sendall(
                ue._encode(
                    self.node_id,
                    ue.TYPE_COMMAND_RESULT,
                    dest=message["source"],
                    data={
                        "success": success and not self.fail,
                        "command": script_path,
                        "output": [{"type": "Info", "output": line} for line in output],
                        "result": "",
                    },
                )
            )
        except OSError:
            pass
        finally:
            connection.close()

    def _execute(self, script_path: str) -> tuple[list[str], bool]:
        """Run the script with the stub `unreal` importable, capturing what it printed."""
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        namespace: dict = {"__name__": "__main__"}
        sys.path.insert(0, str(self.stub_dir))
        try:
            with redirect_stdout(buffer):
                exec(compile(Path(script_path).read_text(encoding="utf-8"), script_path, "exec"), namespace)
            return buffer.getvalue().splitlines(), True
        except Exception as exc:  # noqa: BLE001 - reported the way the editor would
            return buffer.getvalue().splitlines() + [f"Error: {exc}"], False
        finally:
            sys.path.remove(str(self.stub_dir))
            sys.modules.pop("unreal", None)


# --------------------------------------------------------------------------- the script


def test_the_generated_script_is_valid_python() -> None:
    script = import_asset.build_editor_script(
        {"filename": "C:/it's/a \"path\"/rock.fbx", "destination_path": "/Game/M",
         "destination_name": "SM_Rock", "stem": "rock", "replace_existing": True,
         "save": True, "import_as_skeletal": False, "import_materials": True}
    )
    compile(script, "<editor>", "exec")
    assert "unreal.AssetImportTask()" in script
    assert "AssetToolsHelpers.get_asset_tools().import_asset_tasks" in script


def test_parameters_are_embedded_as_data_not_interpolated_text() -> None:
    """A quote or a backslash in a path must not be able to break out of the script."""
    nasty = {"filename": r'C:\a"b\'c\rock.fbx', "destination_path": "/Game/M",
             "destination_name": "", "stem": "rock", "replace_existing": True,
             "save": False, "import_as_skeletal": False, "import_materials": True}
    script = import_asset.build_editor_script(nasty)
    compile(script, "<editor>", "exec")

    namespace: dict = {}
    for line in script.splitlines():
        if line.startswith("PARAMS = "):
            namespace["PARAMS"] = json.loads(eval(line[len("PARAMS = json.loads("):-1]))
            break
    assert namespace["PARAMS"]["filename"] == nasty["filename"]


# --------------------------------------------------------------------------- transport


def test_discovery_finds_a_running_editor(tmp_path: Path, settings) -> None:
    with FakeEditor(tmp_path):
        editors = ue.discover_editors(settings)
    assert [editor["project_name"] for editor in editors] == ["FakeGame"]


def test_discovery_reports_when_nothing_answers(tmp_path: Path, settings) -> None:
    settings.discovery_timeout = 0.5
    assert ue.discover_editors(settings) == []

    with pytest.raises(ue.UnrealError, match="No Unreal editor answered"):
        ue.run_script(settings, "pass", work_dir=tmp_path, timeout=5, log=lambda _line: None)


def test_a_script_runs_in_the_editor_and_its_result_comes_back(tmp_path: Path, settings) -> None:
    script = (
        "import json\n"
        f"print({ue.RESULT_MARKER!r} + json.dumps({{'hello': 'from the editor'}}))\n"
    )
    with FakeEditor(tmp_path):
        result, lines = ue.run_script(
            settings, script, work_dir=tmp_path, timeout=20, log=lambda _line: None
        )
    assert result == {"hello": "from the editor"}
    assert any(ue.RESULT_MARKER in line for line in lines)


def test_a_failed_command_surfaces_the_editor_log(tmp_path: Path, settings) -> None:
    with FakeEditor(tmp_path, fail=True):
        with pytest.raises(ue.UnrealError, match="reported the script as failed"):
            ue.run_script(settings, "pass", work_dir=tmp_path, timeout=20, log=lambda _line: None)


def test_the_import_script_drives_the_real_asset_import_api(tmp_path: Path, settings) -> None:
    """End to end bar the engine: the generated script runs against a stub `unreal`."""
    source = tmp_path / "rock.fbx"
    source.write_bytes(b"Kaydara FBX Binary  \x00")

    script = import_asset.build_editor_script(
        {
            "filename": str(source),
            "destination_path": "/Game/Meshes",
            "destination_name": "SM_Rock",
            "stem": "rock",
            "replace_existing": True,
            "save": True,
            "import_as_skeletal": False,
            "import_materials": True,
        }
    )

    with FakeEditor(tmp_path):
        result, _lines = ue.run_script(
            settings, script, work_dir=tmp_path, timeout=20, log=lambda _line: None
        )

    assert result == {"asset_paths": ["/Game/Meshes/SM_Rock"], "count": 1}


def test_an_exception_inside_the_editor_is_reported_not_swallowed(tmp_path: Path, settings) -> None:
    script = import_asset.build_editor_script(
        {"filename": "/nope.fbx", "destination_path": "/Game", "destination_name": "",
         "stem": "nope", "replace_existing": True, "save": True,
         "import_as_skeletal": False, "import_materials": True}
    )
    # Make the stub's import raise, the way a bad file would in the real editor.
    with FakeEditor(tmp_path) as editor:
        (editor.stub_dir / "unreal.py").write_text(
            STUB_UNREAL.replace(
                "            CALLS.append((\"import_asset_tasks\", task))",
                "            raise RuntimeError('factory refused the file')",
            ),
            encoding="utf-8",
        )
        result, _lines = ue.run_script(
            settings, script, work_dir=tmp_path, timeout=20, log=lambda _line: None
        )
    assert "factory refused the file" in result["error"]


# --------------------------------------------------------------------------- commandlet


def stub_editor(tmp_path: Path, body: str) -> Path:
    """A stand-in for UnrealEditor-Cmd: a Python script wrapped in a launcher."""
    script = tmp_path / "fake_editor.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    launcher = tmp_path / ("fake_editor.cmd" if sys.platform == "win32" else "fake_editor.sh")
    if sys.platform == "win32":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def test_the_headless_route_passes_the_project_and_the_script(tmp_path: Path) -> None:
    project = tmp_path / "Game.uproject"
    project.write_text("{}", encoding="utf-8")
    launcher = stub_editor(
        tmp_path,
        """
        import sys
        print("ARGS " + " ".join(sys.argv[1:]))
        for arg in sys.argv[1:]:
            if arg.startswith("-script="):
                exec(open(arg.split("=", 1)[1]).read(), {"__name__": "__main__"})
        """,
    )
    settings = ue.UnrealSettings(
        project=str(project), editor_cmd=str(launcher), transport="commandlet"
    )
    script = f"import json\nprint({ue.RESULT_MARKER!r} + json.dumps({{'ok': True}}))\n"

    logged: list[str] = []
    result, lines = ue.run_script(
        settings, script, work_dir=tmp_path, timeout=60, log=logged.append
    )

    assert result == {"ok": True}
    args = next(line for line in lines if line.startswith("ARGS "))
    assert "-run=pythonscript" in args and "-unattended" in args
    assert str(project.resolve()) in " ".join(logged)


def test_a_nonzero_exit_is_an_error_with_the_tail_of_the_log(tmp_path: Path) -> None:
    project = tmp_path / "Game.uproject"
    project.write_text("{}", encoding="utf-8")
    launcher = stub_editor(
        tmp_path,
        """
        import sys
        print("LogInit: something went wrong")
        sys.exit(3)
        """,
    )
    settings = ue.UnrealSettings(
        project=str(project), editor_cmd=str(launcher), transport="commandlet"
    )
    with pytest.raises(ue.UnrealError, match="exited with code 3"):
        ue.run_script(settings, "pass", work_dir=tmp_path, timeout=60, log=lambda _line: None)


def test_the_headless_route_says_what_is_missing(tmp_path: Path) -> None:
    settings = ue.UnrealSettings(transport="commandlet")
    with pytest.raises(ue.UnrealError, match="No editor executable configured"):
        ue.run_script(settings, "pass", work_dir=tmp_path, timeout=5, log=lambda _line: None)

    settings.editor_cmd = str(tmp_path / "missing.exe")
    with pytest.raises(ue.UnrealError, match="Editor executable not found"):
        ue.run_script(settings, "pass", work_dir=tmp_path, timeout=5, log=lambda _line: None)


def test_auto_falls_back_to_headless_when_no_editor_answers(tmp_path: Path) -> None:
    project = tmp_path / "Game.uproject"
    project.write_text("{}", encoding="utf-8")
    launcher = stub_editor(
        tmp_path,
        """
        import sys
        for arg in sys.argv[1:]:
            if arg.startswith("-script="):
                exec(open(arg.split("=", 1)[1]).read(), {"__name__": "__main__"})
        """,
    )
    settings = ue.UnrealSettings(
        project=str(project),
        editor_cmd=str(launcher),
        transport="auto",
        multicast_port=TEST_PORT,
        discovery_timeout=DISCOVERY_TIMEOUT,
    )
    script = f"import json\nprint({ue.RESULT_MARKER!r} + json.dumps({{'via': 'headless'}}))\n"

    logged: list[str] = []
    result, _lines = ue.run_script(
        settings, script, work_dir=tmp_path, timeout=60, log=logged.append
    )
    assert result == {"via": "headless"}
    assert any("falling back to a headless run" in line for line in logged)


# --------------------------------------------------------------------------- settings


def test_node_parameters_override_the_plugin_settings() -> None:
    resolved = ue.UnrealSettings.from_payload(
        {"project": "/from/settings.uproject", "transport": "commandlet"},
        {"transport": "remote"},
    )
    assert resolved.project == "/from/settings.uproject"
    assert resolved.transport == "remote", "a value on the node wins"


def test_blank_values_do_not_shadow_a_configured_one() -> None:
    resolved = ue.UnrealSettings.from_payload({"project": "/p.uproject"}, {"project": ""})
    assert resolved.project == "/p.uproject"


def test_the_result_marker_survives_noisy_editor_output() -> None:
    lines = [
        "LogPython: warming up",
        f"LogPython: {ue.RESULT_MARKER}" + json.dumps({"asset_paths": ["/Game/A"]}),
        "LogExit: exiting",
    ]
    assert ue._extract_result(lines) == {"asset_paths": ["/Game/A"]}
    assert ue._extract_result(["nothing here"]) is None


# ------------------------------------------------------------------ unreal.string_ue_case
#
# Driven through the registry rather than by importing the plugin module, the way
# `test_core_nodes.py` drives the built-ins: a node that is written but never registered
# fails here instead of in the palette. Nothing below reaches an editor — this node is the
# one in the plugin that is pure string work.


def make_ctx(project: Path, params: dict) -> NodeContext:
    async def log(message: str, stream: str = "stdout") -> None:
        return None

    return NodeContext(
        inputs={},
        params=params,
        project_root=project,
        assets_dir=project / ".voxarium_studio" / "assets",
        work_dir=project / ".voxarium_studio" / "work",
        settings={},
        log_fn=log,
    )


async def run_ue_case(registry: Registry, project: Path, params: dict) -> str:
    node_class = registry.node_class("unreal.string_ue_case")
    assert node_class is not None, "unreal.string_ue_case must register an implementation"
    outputs = await node_class().run(make_ctx(project, params))
    return outputs["name"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        # The three the feature was asked for.
        ({"text": "model.fbx"}, "Model"),
        ({"text": "test_name"}, "Test_Name"),
        ({"text": "imagetexture_normal.jpg", "prefix": "T"}, "T_Imagetexture_Normal"),
        # A prefix is written with one underscore however it was typed.
        ({"text": "imagetexture_normal.jpg", "prefix": "T_"}, "T_Imagetexture_Normal"),
        # Every separator is a word break, and the extension is one too once it is kept.
        ({"text": "hero-idle 01.png", "prefix": "SM"}, "SM_Hero_Idle_01"),
        ({"text": "model.fbx", "remove_extension": False}, "Model_Fbx"),
        # Only the first letter of a word is touched, so an acronym and a hump survive.
        ({"text": "NORMAL_map", "prefix": "T"}, "T_NORMAL_Map"),
        ({"text": "myMesh"}, "MyMesh"),
        # `pathlib`'s two extension rules, which this node borrows.
        ({"text": ".gitignore"}, "Gitignore"),
        ({"text": "archive.tar.gz"}, "Archive_Tar"),
    ],
)
async def test_ue_case_formats_a_name(
    registry: Registry, project: Path, params: dict, expected: str
) -> None:
    assert await run_ue_case(registry, project, params) == expected


async def test_ue_case_names_the_file_and_not_the_folders_above_it(
    registry: Registry, project: Path
) -> None:
    """What arrives on this port is usually a path, and on this platform a backslashed one.

    Splitting on `/` alone would spell `D:\\Assets\\hero.png` as `D_Assets_Hero`.
    """
    windows = {"text": r"D:\Assets\imagetexture_normal.jpg", "prefix": "T"}
    posix = {"text": "Assets/Textures/imagetexture_normal.jpg", "prefix": "T"}
    assert await run_ue_case(registry, project, windows) == "T_Imagetexture_Normal"
    assert await run_ue_case(registry, project, posix) == "T_Imagetexture_Normal"


async def test_ue_case_refuses_a_string_with_no_name_in_it(
    registry: Registry, project: Path
) -> None:
    """An empty name travelling downstream fails much further from the cause than this."""
    with pytest.raises(ValueError, match="Nothing left to name"):
        await run_ue_case(registry, project, {"text": "___", "prefix": "T"})


def test_ue_case_runs_in_process_and_never_asks_for_an_editor(registry: Registry) -> None:
    """String formatting has no editor dependency."""
    schema = registry.node_type("unreal.string_ue_case")
    assert schema is not None
    assert schema.execution.kind == "python_function"
    assert [port.id for port in schema.inputs] == ["text", "prefix", "remove_extension"]


# --------------------------------------------------------------------------- unreal.build


STUB_UAT = """
import json
import sys
from pathlib import Path

args = {arg.split("=", 1)[0].lower(): arg.partition("=")[2] for arg in sys.argv[1:]}
Path(__file__).with_suffix(".json").write_text(json.dumps(sys.argv[1:]))
project = Path(args["-project"]).stem
platform = args.get("-platform") or args["-targetplatform"]
cook_platform = {"Win64": "Windows"}.get(platform, platform)
archive = Path(args["-archivedirectory"])
if not any(part.lower().startswith((cook_platform.lower(), platform.lower())) for part in archive.parts):
    archive = archive / cook_platform
paks = archive / project / "Content" / "Paks"
paks.mkdir(parents=True, exist_ok=True)
(paks / (project + ".pak")).write_bytes(b"cooked content")
(archive / project).with_suffix(".exe" if platform == "Win64" else ".sh").touch()
"""


@pytest.fixture
def build_request(project: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch) -> ExecutionRequest:
    monkeypatch.delenv("VOXARIUM_STUDIO_UNREAL_PROJECT", raising=False)
    monkeypatch.delenv("UE_USE_SYSTEM_DOTNET", raising=False)
    schema = registry.node_type("unreal.build")
    assert schema is not None, "Build must be available in the Unreal Engine palette"
    assert not schema.execution.cacheable
    assert {"Debug", "DebugGame", "Development", "Shipping", "Test"} <= set(schema.input("configuration").options)
    assert schema.input("mode").default == "Package"
    assert schema.input("output_dir").type == schema.output("build_dir").type == "directory"
    assert schema.input("folder_template").default == "{game}-{platform}-{configuration}"

    install = project.parent / "Unreal Engine"
    engine = install / "Engine"
    ubt = project / "fake_ubt.py"
    uat = project / "fake_uat.py"
    uat.write_text(STUB_UAT, encoding="utf-8")
    if sys.platform == "win32":
        arch = "win-arm64" if platform.machine().upper() == "ARM64" else "win-x64"
        dotnet = engine / "Binaries" / "ThirdParty" / "DotNet" / "8.0.300" / arch / "dotnet.exe"
        dotnet.parent.mkdir(parents=True)
        dotnet.touch()
        tools_dir = engine / "Binaries" / "DotNET"
        stubs = {
            tools_dir / "UnrealBuildTool" / "UnrealBuildTool.dll": ubt,
            tools_dir / "AutomationTool" / "AutomationTool.dll": uat,
        }
        for dll in stubs:
            dll.parent.mkdir(parents=True)
            dll.touch()
        spawn = asyncio.create_subprocess_exec

        async def run_dotnet(program, *args, **kwargs):
            if Path(program) == dotnet:
                stub = stubs[Path(args[0])]
                return await spawn(sys.executable, str(stub), *args[1:], **kwargs)
            return await spawn(program, *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", run_dotnet)
    else:
        host = "Mac" if sys.platform == "darwin" else "Linux"
        batch = engine / "Build" / "BatchFiles"
        for launcher, stub in ((batch / host / "Build.sh", ubt), (batch / "RunUAT.sh", uat)):
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)

    async def log(message: str, stream: str) -> None:
        with (project / "build.log").open("a", encoding="utf-8") as target:
            print(message, file=target)

    return ExecutionRequest(
        node_id="build", run_id="test", schema=schema, params={}, inputs={},
        manifest=registry.manifest_for_node_type(schema.id), node_class=registry.node_class(schema.id),
        project_root=project, assets_dir=project / "assets", work_dir=project / "work", log=log,
        settings={"engine_root": str(install)},
    )


def uat_params(root: Path) -> dict[str, str]:
    args = json.loads((root / "fake_uat.json").read_text())
    return {arg.split("=", 1)[0].lower(): arg.partition("=")[2] for arg in args}


async def test_build_packages_the_game_and_content_by_default(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    project = root / "MyGame.uproject"
    project.write_text("{}", encoding="utf-8")
    host = {"win32": "Win64", "darwin": "Mac", "linux": "Linux"}[sys.platform]

    result = await execute(build_request)
    args = uat_params(root)
    assert {"buildcookrun", "-build", "-cook", "-stage", "-package", "-archive"} <= args.keys()
    assert args["-project"] == str(project)
    assert (args.get("-platform") or args.get("-targetplatform")) == host
    assert args["-clientconfig"] == "Development"
    build_dir = Path(result.outputs["build_dir"])
    assert build_dir == root / "Builds" / f"MyGame-{host}-Development"
    assert (build_dir / ("MyGame.exe" if host == "Win64" else "MyGame.sh")).is_file()
    assert (build_dir / "MyGame" / "Content" / "Paks" / "MyGame.pak").read_bytes() == b"cooked content"
    assert result.outputs["project"] == str(project)


async def test_build_uses_custom_package_location_and_target(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    engine = Path(build_request.settings.pop("engine_root")) / "Engine"
    host = {"win32": "Win64", "darwin": "Mac", "linux": "Linux"}[sys.platform]
    editor = engine / "Binaries" / host / ("UnrealEditor-Cmd.exe" if sys.platform == "win32" else "UnrealEditor-Cmd")
    editor.parent.mkdir(parents=True, exist_ok=True)
    editor.touch()
    build_request.settings.update({"editor_cmd": str(editor), "project": str(root / "Other.uproject")})
    project = root / "Project With Spaces" / "MyGame.uproject"
    project.parent.mkdir()
    project.write_text("{}", encoding="utf-8")
    parent = root.parent / "Build Outputs"
    build_request.params = {
        "project": str(project), "target": "MyGame", "configuration": "Shipping",
        "platform": "Linux", "architecture": "arm64",
        "output_dir": str(parent), "folder_template": "{target}_{architecture}_{configuration}",
    }

    result = await execute(build_request)
    args = uat_params(root)
    assert args["-project"] == str(project)
    assert args["-target"] == "MyGame"
    assert args["-clientconfig"] == args["-serverconfig"] == "Shipping"
    assert (args.get("-platform") or args.get("-targetplatform")) == "Linux"
    assert args["-clientarchitecture"] == args["-serverarchitecture"] == "arm64"
    build_dir = Path(result.outputs["build_dir"])
    assert build_dir == parent / "MyGame_arm64_Shipping"
    assert next(build_dir.rglob("MyGame.pak")).is_file()


async def test_build_publishes_the_contents_of_a_legacy_platform_archive(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    (root / "Game.uproject").write_text("{}", encoding="utf-8")
    build_request.params = {"platform": "Win64"}
    (root / "fake_uat.py").write_text(
        STUB_UAT.replace(
            'archive = Path(args["-archivedirectory"])',
            'archive = Path(args["-archivedirectory"]) / "WindowsNoEditor"',
        ),
        encoding="utf-8",
    )
    result = await execute(build_request)
    assert (Path(result.outputs["build_dir"]) / "Game.exe").is_file()


async def test_build_reports_packaging_failure(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    (root / "Game.uproject").write_text("{}", encoding="utf-8")
    first = await execute(build_request)
    package_file = Path(first.outputs["build_dir"]) / "Game" / "Content" / "Paks" / "Game.pak"
    # An incomplete archive on the second run must not replace the last good package.
    (root / "fake_uat.py").write_text(
        STUB_UAT.replace('b"cooked content"', 'b"partial archive"') + "print('Cooking failed')\nraise SystemExit(6)\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="6"):
        await execute(build_request)
    assert "Cooking failed" in (root / "build.log").read_text(encoding="utf-8")
    assert package_file.read_bytes() == b"cooked content"

    # A successful retry publishes fresh content to the same configured directory.
    (root / "fake_uat.py").write_text(STUB_UAT.replace('b"cooked content"', 'b"new content"'), encoding="utf-8")
    retried = await execute(build_request)
    assert retried.outputs["build_dir"] == first.outputs["build_dir"]
    assert package_file.read_bytes() == b"new content"


async def test_build_preserves_existing_unrelated_directory(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    (root / "Game.uproject").write_text("{}", encoding="utf-8")
    build_request.params = {"output_dir": ".", "folder_template": "Content"}
    content = root / "Content"
    content.mkdir()
    existing_asset = content / "Level.umap"
    existing_asset.write_bytes(b"original level")

    with pytest.raises((ValueError, RuntimeError)):
        await execute(build_request)
    assert list(content.iterdir()) == [existing_asset]
    assert existing_asset.read_bytes() == b"original level"


async def test_compile_exports_the_project_products_from_ubt_manifest(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    project = root / "MyGame.uproject"
    project.write_text("{}", encoding="utf-8")
    engine_product = Path(build_request.settings["engine_root"]).absolute() / "Engine" / "Binaries" / "Core.dll"
    engine_product.parent.mkdir(parents=True, exist_ok=True)
    engine_product.touch()
    (root / "fake_ubt.py").write_text(textwrap.dedent(f"""\
        import json, sys
        from pathlib import Path
        from xml.etree import ElementTree as ET

        args = sys.argv[1:]
        Path(__file__).with_suffix('.json').write_text(json.dumps(args))
        manifest = next(arg.partition('=')[2] for arg in args if arg.lower().startswith('-manifest='))
        project = Path(next(arg.partition('=')[2] for arg in args if arg.lower().startswith('-project=')))
        module = project.parent / 'Binaries' / 'Win64' / 'UnrealEditor-MyGame.dll'
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_bytes(b'compiled project module')
        document = ET.Element('BuildManifest')
        products = ET.SubElement(document, 'BuildProducts')
        for path in (module, Path({str(engine_product)!r})):
            ET.SubElement(products, 'string').text = str(path)
        ET.ElementTree(document).write(manifest, encoding='utf-16', xml_declaration=True)
    """), encoding="utf-8")
    build_request.params = {
        "mode": "Compile", "target": "MyGameEditor", "platform": "Win64",
        "output_dir": "Editor Outputs", "folder_template": "{game}-{target}-{architecture}",
    }

    result = await execute(build_request)
    args = json.loads((root / "fake_ubt.json").read_text())
    assert {"MyGameEditor", "Win64", "Development", f"-Project={project}", "-WaitMutex"} <= set(args)
    build_dir = Path(result.outputs["build_dir"])
    assert build_dir == root / "Editor Outputs" / "MyGame-MyGameEditor-Default"
    assert (build_dir / "Binaries" / "Win64" / "UnrealEditor-MyGame.dll").read_bytes() == b"compiled project module"
    assert not list(build_dir.rglob("Core.dll"))
    assert not (root / "fake_uat.json").exists()


@pytest.mark.parametrize("template", ["../AnotherProject", "nested/folder", "{version}", "{game"])
async def test_build_rejects_invalid_folder_templates(build_request: ExecutionRequest, template: str) -> None:
    root = build_request.project_root
    (root / "Game.uproject").write_text("{}", encoding="utf-8")
    build_request.params["folder_template"] = template
    with pytest.raises(ValueError):
        await execute(build_request)
    assert not (root / "fake_uat.json").exists()


async def test_build_requires_a_choice_between_project_files(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    for name in ("First", "Second"):
        (root / f"{name}.uproject").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match=r"2 \.uproject files"):
        await execute(build_request)
    assert not (root / "fake_uat.json").exists()


async def test_cancelling_build_stops_the_compiler_child(build_request: ExecutionRequest) -> None:
    root = build_request.project_root
    (root / "Game.uproject").write_text("{}", encoding="utf-8")
    loop = asyncio.get_running_loop()
    # The child holds the connection open until it exits. Windows reports a reset
    # and POSIX reports EOF when the process is killed.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        child = (
            "import socket\n"
            f"with socket.create_connection({listener.getsockname()!r}, timeout=15) as sock:\n"
            "    sock.recv(1)\n"
        )
        (root / "fake_uat.py").write_text(
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-c', {child!r}], check=True, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n",
            encoding="utf-8",
        )
        task = asyncio.create_task(execute(build_request))
        try:
            connection, _peer = await asyncio.wait_for(loop.sock_accept(listener), 5)
            with connection:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                with suppress(ConnectionResetError):
                    assert await asyncio.wait_for(loop.sock_recv(connection, 1), 3) == b""
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
