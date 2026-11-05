"""Perforce wire-format, local-path, and scoped-submit regression tests (§9.6).

No depot is needed: responses use the p4 -ztag -Mj protocol and the small fake server
below tracks changelists independently of the backend's implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from voxarium_studio.vcs.base import FileChange, ToolResult, VcsBackend, VcsError
from voxarium_studio.vcs.perforce import PerforceBackend, _patch_text


def reply(*rows: dict[str, Any], code: int = 0, stderr: str = "") -> ToolResult:
    return ToolResult(code, "\n".join(json.dumps(row) for row in rows), stderr)


def empty() -> ToolResult:
    return reply(
        {"code": "error", "severity": 2, "generic": 17, "data": "No matching files"},
        code=1,
    )


async def test_settings_are_global_arguments_and_form_input_is_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = AsyncMock(return_value=reply())
    monkeypatch.setattr(VcsBackend, "run", run)
    backend = PerforceBackend(
        tmp_path,
        {
            "port": "ssl:server:1666",
            "user": "artist",
            "client": "game",
            "charset": "utf8",
        },
    )
    await backend.run("change", "-i", input="Change: new\n")
    assert run.call_args.args == (
        "-p",
        "ssl:server:1666",
        "-u",
        "artist",
        "-c",
        "game",
        "-C",
        "utf8",
        "change",
        "-i",
    )
    assert run.call_args.kwargs["input"] == "Change: new\n"
    assert [f["key"] for f in backend.setting_fields()] == [
        "executable",
        "port",
        "user",
        "client",
        "charset",
    ]


@pytest.mark.parametrize(
    "mapping,expected",
    [
        ([{"path": "mapped/..."}], True),
        ([], False),
        ([{"path": "mapped/..."}, {"path": "mapped/...", "unmap": ""}], False),
    ],
)
async def test_detection_requires_effective_client_mapping(
    tmp_path: Path, mapping: list[dict[str, Any]], expected: bool
) -> None:
    backend = PerforceBackend(tmp_path)
    backend.is_available = lambda: (True, "")
    backend.run = AsyncMock(
        side_effect=[reply({"clientName": "game"}), reply(*mapping)]
    )
    assert await backend.detect() is expected
    assert all(call.kwargs["timeout"] == 10 for call in backend.run.call_args_list)


async def test_missing_cli_or_unknown_client_does_not_claim_project(
    tmp_path: Path,
) -> None:
    backend = PerforceBackend(tmp_path)
    backend.is_available = lambda: (False, "missing")
    backend.run = AsyncMock()
    assert not await backend.detect()
    backend.run.assert_not_called()
    backend.is_available = lambda: (True, "")
    backend.run.return_value = reply({"clientName": "*unknown*"})
    assert not await backend.detect()
    assert backend.run.call_count == 1


async def test_status_combines_opened_and_offline_changes_without_writes(
    tmp_path: Path,
) -> None:
    backend = PerforceBackend(tmp_path)
    backend.run = AsyncMock(
        side_effect=[
            reply(
                {
                    "clientFile": str(tmp_path / "a.txt"),
                    "depotFile": "//depot/a.txt",
                    "action": "edit",
                    "unresolved": "1",
                },
                {
                    "clientFile": str(tmp_path / "gone.txt"),
                    "depotFile": "//depot/gone.txt",
                    "action": "edit",
                },
            ),
            reply(
                {"clientFile": "new asset.png", "action": "add"},
                {"clientFile": "offline.txt", "action": "edit"},
                {"clientFile": "gone.txt", "action": "delete"},
            ),
            reply({"clientName": "game", "clientStream": "//streams/main"}),
            reply({"change": "42"}),
        ]
    )
    status = await backend.status()
    assert status.root == tmp_path.resolve()
    assert status.branch == "//streams/main"
    assert status.revision == "42"
    assert {c.path: (c.kind, c.staged) for c in status.changes} == {
        "a.txt": ("conflicted", True),
        "gone.txt": ("deleted", False),
        "new asset.png": ("untracked", False),
        "offline.txt": ("modified", False),
    }
    args = [call.args for call in backend.run.call_args_list]
    assert "-n" in args[1]
    assert args[-1][-1].endswith("/...#have")


async def test_clean_status_and_authentication_failure_are_distinct(
    tmp_path: Path,
) -> None:
    backend = PerforceBackend(tmp_path)
    backend.run = AsyncMock(side_effect=[empty(), empty()])
    assert await backend._changes() == []
    backend.run = AsyncMock(
        return_value=reply(
            {
                "code": "error",
                "severity": 3,
                "generic": 6,
                "data": "Your session has expired",
            },
            code=1,
        )
    )
    with pytest.raises(VcsError, match="session has expired"):
        await backend._changes()


@pytest.mark.parametrize(
    "result",
    [
        ToolResult(0, "not json", ""),
        reply({"severity": "broken"}),
        ToolResult(1, "", "Connection refused"),
    ],
)
async def test_invalid_output_is_a_vcs_error(
    tmp_path: Path, result: ToolResult
) -> None:
    backend = PerforceBackend(tmp_path)
    backend.run = AsyncMock(return_value=result)
    with pytest.raises(VcsError):
        await backend._query("info")


def test_filespecs_escape_revision_and_wildcard_characters(tmp_path: Path) -> None:
    backend = PerforceBackend(tmp_path)
    assert backend._spec("a#b@c%d*.txt").endswith("/a%23b%40c%25d%2A.txt")
    with pytest.raises(VcsError, match="outside"):
        backend._spec("../outside.txt")
    with pytest.raises(VcsError, match="ellipsis|literal"):
        backend._spec("wild...card.txt")


class Depot:
    """Track what a submit actually includes, including unrelated existing work."""

    def __init__(
        self, root: Path, *, fail_submit: bool = False, inject_file: bool = False
    ) -> None:
        self.root = root
        self.files = {
            "task.txt": "default",
            "unrelated.txt": "default",
            "numbered.txt": "123",
        }
        self.submitted: list[set[str]] = []
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.fail_submit = fail_submit
        self.inject_file = inject_file

    async def run(self, *raw: str, **kwargs: Any) -> ToolResult:
        args = raw[2:]  # -ztag -Mj
        self.calls.append((args, kwargs))
        command = args[0]
        if command == "change":
            # A missing/implicit Files list would steal all default files.
            form = kwargs["input"]
            assert "Files:\n\nStream:\n\n" in form
            assert "\tDescription:\n\tmalicious-looking line" in form
            return reply({"code": "info", "data": "Change 200 created."})
        if command in ("reopen", "reconcile"):
            name = Path(args[-1]).name
            self.files[name] = args[args.index("-c") + 1]
            return reply({"depotFile": f"//depot/{name}", "action": "edit"})
        if command == "opened":
            if self.inject_file:
                self.files["surprise.txt"] = "200"
            return reply(
                *[
                    {"depotFile": f"//depot/{name}", "clientFile": f"//game/{name}"}
                    for name, cl in self.files.items()
                    if cl == args[-1]
                ]
            )
        if command == "where":
            return reply({"path": str(self.root / args[-1].split("/")[-1])})
        if command == "submit":
            if self.fail_submit:
                return reply(
                    {
                        "code": "error",
                        "severity": 3,
                        "generic": 6,
                        "data": "Resolve required",
                    },
                    code=1,
                )
            self.submitted.append(
                {name for name, cl in self.files.items() if cl == args[-1]}
            )
            return reply({"submittedChange": "201"})
        raise AssertionError(args)


def commit_backend(tmp_path: Path, **options: Any) -> tuple[PerforceBackend, Depot]:
    backend = PerforceBackend(tmp_path)
    depot = Depot(tmp_path, **options)
    backend.run = depot.run
    backend.current_branch = AsyncMock(return_value="game")
    backend._changes = AsyncMock(
        return_value=[
            FileChange("task.txt", "modified", staged=True),
            FileChange("new.txt", "untracked"),
            FileChange("unrelated.txt", "modified", staged=True),
            FileChange("numbered.txt", "modified", staged=True),
        ]
    )
    return backend, depot


MESSAGE = "task result\nDescription:\nmalicious-looking line"


async def test_scoped_submit_never_includes_other_pending_work(tmp_path: Path) -> None:
    backend, depot = commit_backend(tmp_path)
    result = await backend.commit(
        MESSAGE, paths=["task.txt", "new.txt", "unchanged.txt"], add_untracked=True
    )
    assert result.committed and result.revision == "201"
    assert result.files == ["task.txt", "new.txt"]
    assert result.skipped == ["unchanged.txt"]
    assert depot.submitted == [{"task.txt", "new.txt"}]
    assert depot.files["unrelated.txt"] == "default"
    assert depot.files["numbered.txt"] == "123"
    assert all(
        "..." not in arg
        for args, _ in depot.calls
        if args[0] in {"reconcile", "reopen", "submit"}
        for arg in args
    )


async def test_submit_failure_keeps_recoverable_changelist(tmp_path: Path) -> None:
    backend, depot = commit_backend(tmp_path, fail_submit=True)
    with pytest.raises(
        VcsError, match="(?s)changelist 200 remains pending.*Resolve required"
    ):
        await backend.commit(MESSAGE, paths=["task.txt"])
    assert depot.files["task.txt"] == "200"
    assert not any(args[0] in {"revert", "delete"} for args, _ in depot.calls)


async def test_changed_pending_file_set_cannot_expand_submit(tmp_path: Path) -> None:
    backend, depot = commit_backend(tmp_path, inject_file=True)
    with pytest.raises(VcsError, match="no longer matches"):
        await backend.commit(MESSAGE, paths=["task.txt"])
    assert depot.submitted == []


@pytest.mark.parametrize("paths", [[], ["unchanged.txt"], ["new.txt"]])
async def test_no_selected_tracked_changes_has_no_write_side_effect(
    tmp_path: Path, paths: list[str]
) -> None:
    backend, depot = commit_backend(tmp_path)
    result = await backend.commit(MESSAGE, paths=paths)
    assert not result.committed
    assert depot.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        [FileChange("task.txt", "conflicted", staged=True)],
        [
            FileChange("task.txt", "renamed", staged=True, origin="old.txt"),
            FileChange("old.txt", "deleted", staged=True),
        ],
    ],
)
async def test_conflicts_and_partial_moves_are_rejected_before_writes(
    tmp_path: Path, changes: list[FileChange]
) -> None:
    backend, depot = commit_backend(tmp_path)
    backend._changes = AsyncMock(return_value=changes)
    with pytest.raises(VcsError):
        await backend.commit(MESSAGE, paths=["task.txt"])
    assert depot.calls == []


async def test_file_patch_uses_synced_revision_and_preserves_binary_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes("новая строка\n".encode())
    backend = PerforceBackend(tmp_path)

    async def run(*args: str, **_kwargs: Any) -> ToolResult:
        if args[0] == "print":
            assert args[-1] == "//depot/file.txt#4"
            Path(args[args.index("-o") + 1]).write_bytes("старая строка\n".encode())
            return reply()
        return reply({"depotFile": "//depot/file.txt", "haveRev": "4"})

    backend.run = run
    patch = await backend.workspace_patch(tmp_path, "file.txt")
    assert "-старая строка" in patch and "+новая строка" in patch
    assert "--- a/file.txt" in patch
    assert _patch_text(b"\0old", b"\0new", "asset.bin").startswith("Binary files")
    assert "--- /dev/null" in _patch_text(None, b"new\n", "new.txt")
    assert "+++ /dev/null" in _patch_text(b"old\n", None, "deleted.txt")


async def test_add_respects_nonrecursive_directory_selection(tmp_path: Path) -> None:
    backend = PerforceBackend(tmp_path)
    backend._changes = AsyncMock(
        return_value=[
            FileChange("dir/a.txt", "untracked"),
            FileChange("dir/sub/b.txt", "untracked"),
            FileChange("other.txt", "untracked"),
        ]
    )
    backend.run = AsyncMock(return_value=reply())
    assert await backend.add(["dir"], recursive=False) == ["dir/a.txt"]
    assert backend.run.call_count == 1
