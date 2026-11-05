"""The independent Plastic entity and built-in nodes, without Plastic.

There is no free Unity Version Control server to test against, so what is covered here is
everything on this side of the process boundary: which ``cm`` command line each operation
builds, how its output is parsed, and how the nodes behave around it — including the two
cases a pipeline actually hits, an unchanged workspace and a missing client.

What is *not* covered is whether a given ``cm`` build accepts these flags. That needs a
real client and a real repository, and the parsing here is deliberately forgiving because
of it: an unrecognised status code costs a change's kind, never its path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voxarium_studio.plugins.api import NodeContext
from voxarium_studio.vcs.base import CHANGE_KINDS, ToolResult, VcsError

from voxarium_studio.vcs import plastic as plastic_vcs
from voxarium_studio.vcs import nodes as plastic_plugin

PlasticBackend = plastic_vcs.PlasticBackend


# --------------------------------------------------------------------------- a fake cm


class FakeCm(PlasticBackend):
    """A backend whose ``cm`` calls are recorded and answered from a handler."""

    def __init__(self, root: Path, settings=None, handler=None) -> None:
        super().__init__(root, settings)
        self.calls: list[list[str]] = []
        self.handler = handler or (lambda args: (0, ""))

    def is_available(self) -> tuple[bool, str]:
        return True, ""

    async def detect(self) -> bool:
        return True

    async def run(self, *args: str, check: bool = False, **kwargs) -> ToolResult:
        self.calls.append(list(args))
        code, stdout = self.handler(list(args))
        result = ToolResult(code=code, stdout=stdout, stderr="")
        if check and not result.ok:
            raise VcsError(f"cm {' '.join(args)} failed")
        return result

    def call_starting(self, verb: str) -> list[str] | None:
        return next((call for call in self.calls if call and call[0] == verb), None)


STATUS_HEADER = "STATUS 42 br:/main@MyGame@localhost:8087"

MACHINE_READABLE = "\n".join(
    [
        STATUS_HEADER,
        "CH;C:\\Workspaces\\MyGame\\Assets\\Generated\\golem.vox",
        "AD;C:\\Workspaces\\MyGame\\Assets\\Generated\\golem.vox.meta",
        "PR;C:\\Workspaces\\MyGame\\Assets\\Generated\\preview.png",
        "IG;C:\\Workspaces\\MyGame\\Temp\\junk.tmp",
    ]
)


def default_handler(pending: str = MACHINE_READABLE, checkin_output: str = ""):
    def handle(args: list[str]) -> tuple[int, str]:
        if args[0] == "status" and "--header" in args:
            return 0, "cs:42@br:/main@MyGame@localhost:8087\n"
        if args[0] == "status":
            return 0, pending
        if args[0] == "checkin":
            return 0, checkin_output or (
                "Checkin operation has finished\n"
                "Created changeset cs:43@br:/main@MyGame@localhost:8087 (mount:'/')"
            )
        return 0, ""

    return handle


# --------------------------------------------------------------------------- parsing


def test_machine_readable_status_is_mapped_onto_the_abstraction() -> None:
    changes = plastic_vcs._parse_status(MACHINE_READABLE, machine_readable=True)

    kinds = {Path(change.path).name: change.kind for change in changes}
    assert kinds["golem.vox"] == "modified"
    assert kinds["golem.vox.meta"] == "added"
    assert kinds["preview.png"] == "untracked"
    # The header line is not a change, and a Windows path is not mistaken for a code.
    assert all(change.path.startswith("C:\\") for change in changes)


def test_an_unknown_status_code_keeps_the_path() -> None:
    changes = plastic_vcs._parse_status("ZZ;/wk/thing.png", machine_readable=True)

    assert len(changes) == 1
    assert changes[0].kind == "unknown"
    assert changes[0].path == "/wk/thing.png"


def test_a_path_with_no_code_survives_whole() -> None:
    """The --short fallback prints paths only — none of them may be truncated."""
    changes = plastic_vcs._parse_status(
        "C:\\Workspaces\\My Game\\Assets\\a b.png", machine_readable=False
    )

    assert [change.path for change in changes] == ["C:\\Workspaces\\My Game\\Assets\\a b.png"]


def test_whitespace_separated_output_is_accepted_too() -> None:
    """Not every cm build honours --fieldseparator; a space-separated line still parses."""
    changes = plastic_vcs._parse_status("CH /wk/Assets/a b.png", machine_readable=True)

    assert changes[0].kind == "modified"
    assert changes[0].path == "/wk/Assets/a b.png"


def test_the_changeset_comes_from_the_last_marker() -> None:
    output = "Created changeset cs:12@br:/main\nCreated changeset cs:13@br:/main"
    assert plastic_vcs._find_changeset(output) == "cs:13"
    assert plastic_vcs._find_changeset("nothing here") is None


def test_a_workspace_is_recognised_by_its_marker(tmp_path: Path) -> None:
    (tmp_path / ".plastic").mkdir()
    nested = tmp_path / "Assets" / "Generated"
    nested.mkdir(parents=True)

    assert PlasticBackend(tmp_path).workspace_root() == tmp_path
    # The managed project is often a subdirectory of the workspace.
    assert PlasticBackend(nested).workspace_root() == tmp_path
    assert PlasticBackend(tmp_path.parent).workspace_root() is None


async def test_detection_says_no_without_a_marker_or_a_client(tmp_path: Path) -> None:
    backend = PlasticBackend(tmp_path, {"executable": str(tmp_path / "nope" / "cm.exe")})
    assert await backend.detect() is False


def test_the_workspace_setting_redirects_the_root(tmp_path: Path) -> None:
    elsewhere = tmp_path / "workspaces" / "MyGame"
    elsewhere.mkdir(parents=True)

    backend = PlasticBackend(tmp_path / "project", {"workspace": str(elsewhere)})

    assert backend.root == elsewhere


def test_a_relative_workspace_is_resolved_against_the_managed_project(tmp_path: Path) -> None:
    backend = PlasticBackend(tmp_path / "game", {"workspace": ".."})
    assert backend.root == tmp_path


async def test_explicit_paths_keep_the_managed_project_frame_with_workspace_override(tmp_path: Path) -> None:
    project = tmp_path / "game"
    backend = FakeCm(project, {"workspace": ".."}, handler=default_handler())
    await backend.commit("asset", paths=["Assets/rock.vox"])
    assert backend.call_starting("checkin")[1] == str(project / "Assets/rock.vox")


# --------------------------------------------------------------------------- the backend


async def test_status_reports_branch_changeset_and_changes(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())

    status = await backend.status()

    assert status.branch == "/main"
    assert status.revision == "cs:42"
    # The ignored item is dropped; the other three are pending.
    assert len(status.changes) == 3
    assert status.clean is False


async def test_revision_answers_the_changeset(tmp_path: Path) -> None:
    """`VcsBackend.revision` is what `/api/vcs` asks every backend for."""
    backend = FakeCm(tmp_path, handler=default_handler())

    assert await backend.revision() == "cs:42"


async def test_status_only_ever_speaks_the_normalised_vocabulary(tmp_path: Path) -> None:
    """`kind` is `ChangeKind`, never one of cm's own letters — the UI colours by it.

    `ignored` is the trap here: it is in `STATUS_CODES` as a local sentinel and is *not*
    part of the vocabulary, so it must never survive as far as a change.
    """
    lines = [f"{code};/wk/{code}.png" for code in plastic_vcs.STATUS_CODES]
    backend = FakeCm(tmp_path, handler=default_handler(pending="\n".join([STATUS_HEADER, *lines])))

    status = await backend.status()

    assert status.changes
    assert {change.kind for change in status.changes} <= CHANGE_KINDS
    assert not any(change.path.endswith("IG.png") for change in status.changes)


async def test_checkin_passes_all_and_private_and_returns_the_changeset(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())

    result = await backend.commit("generated a golem", add_untracked=True)

    checkin = backend.call_starting("checkin")
    assert checkin is not None
    # --all/--applychanged are what make a pipeline-written file count: nothing was ever
    # checked out. `cm checkin` has no --recursive; it is always recursive from the path.
    assert "--all" in checkin
    assert "--applychanged" in checkin
    assert "--private" in checkin
    assert not any(flag in checkin for flag in ("-R", "-r", "--recursive"))
    assert checkin[1] == "."  # no paths given means the whole workspace
    # The comment is one argument, `-c=<comment>` — the form the CLI documents.
    assert "-c=generated a golem" in checkin
    assert result.committed is True
    assert result.revision == "cs:43"
    assert result.branch == "/main"
    assert len(result.files) == 3


async def test_checkin_scoped_to_paths_names_them(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())

    await backend.commit("one file", paths=["Assets/Generated/golem.vox"])

    checkin = backend.call_starting("checkin")
    assert checkin[1] == "Assets/Generated/golem.vox"
    assert "--private" not in checkin  # add_untracked defaults to False on the backend


async def test_private_items_are_excluded_when_not_wanted(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())

    result = await backend.commit("controlled only", add_untracked=False)

    assert all(not path.endswith("preview.png") for path in result.files)


async def test_an_unchanged_workspace_is_not_an_error_and_runs_no_checkin(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=default_handler(pending=STATUS_HEADER))

    result = await backend.commit("nothing to do")

    assert result.committed is False
    assert result.revision is None
    assert backend.call_starting("checkin") is None


async def test_a_failed_checkin_raises(tmp_path: Path) -> None:
    def failing(args: list[str]) -> tuple[int, str]:
        if args[0] == "checkin":
            return 1, "The server is not available"
        return default_handler()(args)

    backend = FakeCm(tmp_path, handler=failing)

    with pytest.raises(VcsError, match="checkin failed"):
        await backend.commit("will not land")


async def test_status_falls_back_to_short_when_machine_readable_is_rejected(tmp_path: Path) -> None:
    def picky(args: list[str]) -> tuple[int, str]:
        if args[0] == "status" and "--machinereadable" in args:
            return 1, "Unknown option --fieldseparator"
        if args[0] == "status" and "--short" in args:
            return 0, "/wk/Assets/a.png\n/wk/Assets/b.png"
        return default_handler()(args)

    changes = await FakeCm(tmp_path, handler=picky)._changes()

    assert [change.path for change in changes] == ["/wk/Assets/a.png", "/wk/Assets/b.png"]


async def test_add_without_paths_takes_everything_private(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())

    added = await backend.add([])

    assert added == ["C:\\Workspaces\\MyGame\\Assets\\Generated\\preview.png"]
    add = backend.call_starting("add")
    assert "-R" in add and "--ignorefailed" in add
    assert "--parents" not in add  # cm has no such flag; parents are named explicitly


async def test_add_names_private_parent_directories_first(tmp_path: Path) -> None:
    """`cm add` refuses an item whose parent is not under control, and has no --parents."""
    generated = tmp_path / "Assets" / "Generated"
    private = "\n".join(
        [
            f"PR;{tmp_path / 'Assets'}",
            f"PR;{generated}",
            f"PR;{generated / 'golem.vox'}",
        ]
    )
    backend = FakeCm(tmp_path, handler=default_handler(pending=private))

    added = await backend.add([str(generated / "golem.vox")])

    # Outermost first, and the file last.
    assert added == [str(tmp_path / "Assets"), str(generated), str(generated / "golem.vox")]

    # The parents go in their own non-recursive call: adding them with -R would sweep in
    # every other private file under Assets, which the caller did not name.
    adds = [call for call in backend.calls if call[0] == "add"]
    assert len(adds) == 2
    assert "-R" not in adds[0] and adds[0][-2:] == [str(tmp_path / "Assets"), str(generated)]
    assert "-R" in adds[1] and adds[1][-1] == str(generated / "golem.vox")


async def test_add_does_not_invent_parents_that_are_already_controlled(tmp_path: Path) -> None:
    generated = tmp_path / "Assets" / "Generated"
    backend = FakeCm(
        tmp_path, handler=default_handler(pending=f"PR;{generated / 'golem.vox'}")
    )

    added = await backend.add([str(generated / "golem.vox")])

    assert added == [str(generated / "golem.vox")]


# --------------------------------------------------------------------------- a file's patch


def cat_handler(bases: dict[str, bytes | None]):
    """A ``cm`` whose ``cat`` writes the loaded revision through ``--file=``.

    ``None`` is a file the repository has never heard of — ``cm`` prints "The specified
    revision was not found" and exits 1 — and ``b""`` is a checked-out add, which ``cm``
    answers with an empty file and a clean exit.
    """
    status = default_handler()

    def handle(args: list[str]) -> tuple[int, str]:
        if args[0] != "cat":
            return status(args)
        base = bases.get(Path(args[1]).name)
        if base is None:
            return 1, ""
        out = next(arg for arg in args if arg.startswith("--file=")).removeprefix("--file=")
        Path(out).write_bytes(base)
        return 0, ""

    return handle


async def test_a_file_patch_is_the_loaded_revision_against_the_disk(tmp_path: Path) -> None:
    """`cm diff` opens a window; the patch the pane renders is made from `cm cat`."""
    (tmp_path / "Script").mkdir()
    (tmp_path / "Script" / "Dash.as").write_bytes(b"one\nchanged\nthree\n")
    backend = FakeCm(tmp_path, handler=cat_handler({"Dash.as": b"one\ntwo\nthree\n"}))

    patch = await backend.workspace_patch(tmp_path, "Script/Dash.as")

    assert patch.startswith("--- a/Script/Dash.as\n+++ b/Script/Dash.as\n@@")
    assert "-two\n" in patch and "+changed\n" in patch
    cat = backend.call_starting("cat")
    assert cat is not None and cat[1] == str(tmp_path / "Script" / "Dash.as")
    assert any(arg.startswith("--file=") for arg in cat), "stdout re-encodes the bytes"


async def test_a_private_file_is_an_addition_of_every_line(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_bytes(b"first\nsecond\n")
    backend = FakeCm(tmp_path, handler=cat_handler({"notes.md": None}))

    patch = await backend.workspace_patch(tmp_path, "notes.md")

    assert patch.startswith("--- /dev/null\n+++ b/notes.md\n")
    assert "+first\n+second\n" in patch


async def test_a_checked_out_add_reads_the_same_as_a_private_file(tmp_path: Path) -> None:
    """`cm cat` answers an added item with an empty revision, not a refusal."""
    (tmp_path / "new.yaml").write_bytes(b"a: 1\n")
    backend = FakeCm(tmp_path, handler=cat_handler({"new.yaml": b""}))

    patch = await backend.workspace_patch(tmp_path, "new.yaml")

    assert "+a: 1\n" in patch
    body = patch.split("@@", 2)[2]
    assert not any(line.startswith("-") for line in body.splitlines())


async def test_a_deleted_file_is_a_removal_of_every_line(tmp_path: Path) -> None:
    backend = FakeCm(tmp_path, handler=cat_handler({"gone.txt": b"was here\n"}))

    patch = await backend.workspace_patch(tmp_path, "gone.txt")

    assert patch.startswith("--- a/gone.txt\n+++ /dev/null\n")
    assert "-was here\n" in patch


async def test_an_unchanged_file_has_no_patch(tmp_path: Path) -> None:
    (tmp_path / "same.txt").write_bytes(b"same\n")
    backend = FakeCm(tmp_path, handler=cat_handler({"same.txt": b"same\n"}))

    assert await backend.workspace_patch(tmp_path, "same.txt") == ""


async def test_a_binary_file_is_named_and_not_diffed(tmp_path: Path) -> None:
    (tmp_path / "icon.png").write_bytes(b"\x89PNG\0\0new")
    backend = FakeCm(tmp_path, handler=cat_handler({"icon.png": b"\x89PNG\0\0old"}))

    patch = await backend.workspace_patch(tmp_path, "icon.png")

    assert patch == "Binary files a/icon.png and b/icon.png differ\n"


async def test_an_absolute_path_is_taken_as_it_is(tmp_path: Path) -> None:
    """`task_patch` hands the path over as the changes list reported it — absolute, as
    `cm status` prints them."""
    target = tmp_path / "abs.txt"
    target.write_bytes(b"new\n")
    backend = FakeCm(tmp_path, handler=cat_handler({"abs.txt": b"old\n"}))

    patch = await backend.workspace_patch(tmp_path, str(target))

    assert "--- a/abs.txt\n+++ b/abs.txt\n" in patch and "-old\n+new\n" in patch


async def test_the_whole_workspace_is_still_the_summary(tmp_path: Path) -> None:
    """No file named, no `cm cat` per file: that is a process each, and nothing asks."""
    backend = FakeCm(tmp_path, handler=cat_handler({}))

    patch = await backend.workspace_patch(tmp_path)

    assert "modified" in patch and "golem.vox" in patch
    assert backend.call_starting("cat") is None


def test_the_patch_line_endings_are_the_formats_own() -> None:
    """A last line with no newline must not run into the next patch line."""
    patch = plastic_vcs._unified_patch(b"a\nb", b"a\nc", "f.txt")

    assert patch.endswith("-b\n+c\n")


# --------------------------------------------------------------------------- the nodes


def make_ctx(tmp_path: Path, params: dict, sink: list[str] | None = None) -> NodeContext:
    async def log(message: str, stream: str = "stdout") -> None:
        (sink if sink is not None else []).append(message)

    return NodeContext(
        inputs={},
        params=params,
        project_root=tmp_path,
        assets_dir=tmp_path / "assets",
        work_dir=tmp_path / "work",
        settings={},
        log_fn=log,
    )


@pytest.fixture
def use_fake_cm(monkeypatch):
    """Point every node at one recording backend."""

    def install(backend) -> None:
        monkeypatch.setattr(plastic_plugin, "_backend", lambda ctx: backend)

    return install


async def test_checkin_node_outputs_the_changeset(tmp_path: Path, use_fake_cm) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())
    use_fake_cm(backend)
    logs: list[str] = []

    outputs = await plastic_plugin.PlasticCheckinNode().run(
        make_ctx(tmp_path, {"message": "run 7: golem", "file": "Assets/golem.vox"}, logs)
    )

    assert outputs["committed"] is True
    assert outputs["changeset"] == "cs:43"
    assert outputs["branch"] == "/main"
    assert backend.call_starting("checkin")[1] == "Assets/golem.vox"
    assert any("checked in" in line for line in logs)


async def test_checkin_node_refuses_an_empty_comment(tmp_path: Path, use_fake_cm) -> None:
    use_fake_cm(FakeCm(tmp_path, handler=default_handler()))

    with pytest.raises(ValueError, match="comment is required"):
        await plastic_plugin.PlasticCheckinNode().run(make_ctx(tmp_path, {"message": "  "}))


async def test_checkin_node_passes_through_an_unchanged_workspace(
    tmp_path: Path, use_fake_cm
) -> None:
    """A pipeline that regenerates an identical asset must not fail the run."""
    use_fake_cm(FakeCm(tmp_path, handler=default_handler(pending=STATUS_HEADER)))

    outputs = await plastic_plugin.PlasticCheckinNode().run(
        make_ctx(tmp_path, {"message": "nothing changed"})
    )

    assert outputs["committed"] is False
    assert outputs["changeset"] == ""


async def test_checkin_node_can_be_told_to_fail_instead(tmp_path: Path, use_fake_cm) -> None:
    use_fake_cm(FakeCm(tmp_path, handler=default_handler(pending=STATUS_HEADER)))

    with pytest.raises(ValueError, match="Nothing to check in"):
        await plastic_plugin.PlasticCheckinNode().run(
            make_ctx(tmp_path, {"message": "must land", "skip_if_unchanged": False})
        )


async def test_status_node_summarises_the_workspace(tmp_path: Path, use_fake_cm) -> None:
    use_fake_cm(FakeCm(tmp_path, handler=default_handler()))

    outputs = await plastic_plugin.PlasticStatusNode().run(make_ctx(tmp_path, {}))

    assert outputs["clean"] is False
    assert outputs["count"] == 3
    assert outputs["changeset"] == "cs:42"
    assert {change["kind"] for change in outputs["changes"]} == {"modified", "added", "untracked"}


async def test_add_node_merges_the_file_port_and_the_paths_field(
    tmp_path: Path, use_fake_cm
) -> None:
    backend = FakeCm(tmp_path, handler=default_handler())
    use_fake_cm(backend)

    outputs = await plastic_plugin.PlasticAddNode().run(
        make_ctx(tmp_path, {"file": "a.png", "paths": "b.png\n c.png ,d.png"})
    )

    assert outputs["paths"] == ["a.png", "b.png", "c.png", "d.png"]
    assert outputs["count"] == 4


async def test_a_missing_client_is_reported_as_something_to_fix(tmp_path: Path, monkeypatch) -> None:
    backend = PlasticBackend(tmp_path, {"executable": str(tmp_path / "gone" / "cm.exe")})
    monkeypatch.setattr(plastic_plugin, "_backend", lambda ctx: backend)

    with pytest.raises(VcsError, match="Unity Version Control client"):
        await plastic_plugin.PlasticStatusNode().run(make_ctx(tmp_path, {}))


async def test_a_project_outside_a_workspace_is_reported_too(tmp_path: Path, monkeypatch) -> None:
    class Available(PlasticBackend):
        def is_available(self):
            return True, ""

        async def detect(self) -> bool:
            return False

    monkeypatch.setattr(plastic_plugin, "_backend", lambda ctx: Available(tmp_path))

    with pytest.raises(VcsError, match="not inside a Plastic workspace"):
        await plastic_plugin.PlasticStatusNode().run(make_ctx(tmp_path, {}))


# --------------------------------------------------------------------------- registration


def test_plastic_nodes_are_available_without_a_plugin(registry) -> None:
    assert registry.plugin("plastic") is None
    assert not any(page.id == "plastic-settings" for page in registry.pages())
    for node_id in ("plastic.checkin", "plastic.add", "plastic.status"):
        assert registry.node_type(node_id).plugin_id == "vcs:plastic"
        assert registry.node_class(node_id) is not None


async def test_every_node_returns_exactly_its_declared_output_ports(
    tmp_path: Path, use_fake_cm
) -> None:
    """An edge attaches by port id, so an output under any other key goes nowhere."""
    use_fake_cm(FakeCm(tmp_path, handler=default_handler()))

    for node_class in (
        plastic_plugin.PlasticCheckinNode,
        plastic_plugin.PlasticAddNode,
        plastic_plugin.PlasticStatusNode,
    ):
        outputs = await node_class().run(make_ctx(tmp_path, {"message": "a comment"}))
        declared = {port.id for port in node_class.schema.outputs}
        assert set(outputs) == declared, node_class.schema.id


def test_the_backend_satisfies_the_abstraction_as_it_stands(tmp_path: Path) -> None:
    """Audited against `VcsBackend` itself, so the check follows the base class."""
    from voxarium_studio.vcs.base import VcsBackend

    assert issubclass(PlasticBackend, VcsBackend)
    assert PlasticBackend.program == "cm"
    # The settings key the plugin's own SettingsPage.js writes is the inherited default,
    # which is what lets the VCS page configure this backend without knowing it is Plastic.
    assert PlasticBackend.executable_setting == "executable"

    # Isolation stays unsupported and inherited: a per-task Plastic workspace means a
    # server round trip, so §10.5 falls back to path locks (see plugins.md).
    assert PlasticBackend.supports_isolation is False
    assert PlasticBackend.create_workspace is VcsBackend.create_workspace
    assert PlasticBackend.remove_workspace is VcsBackend.remove_workspace

    # Everything else is implemented here, `revision` included — the base's default
    # returns None, which for Plastic would silently drop the changeset.
    for name in ("detect", "current_branch", "revision", "status", "diff", "add", "commit"):
        assert getattr(PlasticBackend, name) is not getattr(VcsBackend, name), name


def test_legacy_settings_move_to_the_vcs_entity_with_explicit_values_winning() -> None:
    from voxarium_studio.core.config import AppConfig

    config = AppConfig.model_validate({
        "plugins": {"plastic": {"enabled": False, "settings": {
            "executable": "C:/plugin/cm.exe", "workspace": "D:/Workspaces/MyGame"
        }}},
        "vcs": {"settings": {"plastic": {"executable": "C:/vcs-page/cm.exe"}}},
    })
    assert "plastic" not in config.plugins
    assert config.vcs.settings["plastic"] == {
        "executable": "C:/vcs-page/cm.exe", "workspace": "D:/Workspaces/MyGame"
    }
    assert AppConfig.model_validate(config.model_dump()).model_dump() == config.model_dump()


def test_the_vcs_page_can_configure_plastic(client) -> None:
    overview = client.put(
        "/api/vcs/settings", json={"settings": {"plastic": {"executable": "C:/nope/cm.exe"}}}
    ).json()

    plastic = next(option for option in overview["backends"] if option["id"] == "plastic")
    assert plastic["executable"] == "C:/nope/cm.exe"
    assert plastic["program"] == "cm"
    assert plastic["available"] is False and "C:/nope/cm.exe" in plastic["reason"]
    assert plastic["detected"] is False and plastic["active"] is False


def test_the_checkin_node_never_reads_from_cache(registry) -> None:
    """A cache hit would mean a pipeline reporting a commit it did not make."""
    for node_type_id in ("plastic.checkin", "plastic.add", "plastic.status"):
        schema = registry.node_type(node_type_id)
        assert schema.execution.cacheable is False
        assert schema.execution.resource_class == "io"
