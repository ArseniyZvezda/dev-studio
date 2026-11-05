"""Perforce CLI adapter (§9.6), independent of the plugin lifecycle.

Authentication remains with p4 (P4CONFIG, environment, and login tickets). Submits use
a new, empty numbered changelist so a task can never submit somebody else's pending
files merely because they share the default changelist. A failed submit leaves that
changelist available for recovery in p4/P4V; no automatic revert touches working files.
"""

from __future__ import annotations

import difflib
import json
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from .base import (
    DEFAULT_TIMEOUT,
    CommitResult,
    FileChange,
    LogFn,
    ToolResult,
    VcsBackend,
    VcsError,
    VcsStatus,
)

_ACTIONS = {
    "add": "added",
    "edit": "modified",
    "delete": "deleted",
    "move/add": "renamed",
    "move/delete": "deleted",
    "branch": "added",
    "integrate": "modified",
    "import": "added",
}
_FIELDS = "depotFile,clientFile,action,movedFile,unresolved,type,change"


class PerforceBackend(VcsBackend):
    id: ClassVar[str] = "perforce"
    name: ClassVar[str] = "Perforce"
    program: ClassVar[str] = "p4"
    supports_isolation: ClassVar[bool] = False
    settings_fields: ClassVar[tuple[dict[str, str], ...]] = tuple(
        {"key": key, "type": "string"} for key in ("port", "user", "client", "charset")
    )

    async def run(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        log_fn: LogFn | None = None,
        check: bool = False,
        input: str | None = None,
    ) -> ToolResult:
        options: list[str] = []
        for key, flag in (
            ("port", "-p"),
            ("user", "-u"),
            ("client", "-c"),
            ("charset", "-C"),
        ):
            value = str(self.settings.get(key) or "").strip()
            if value:
                options.extend((flag, value))
        return await super().run(
            *options,
            *args,
            cwd=cwd,
            timeout=timeout,
            log_fn=log_fn,
            check=check,
            input=input,
        )

    async def _query(
        self,
        *args: str,
        empty_ok: bool = False,
        timeout: float | None = DEFAULT_TIMEOUT,
        log_fn: LogFn | None = None,
        input: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.run(
            "-ztag", "-Mj", *args, timeout=timeout, log_fn=log_fn, input=input
        )
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        empty = False
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise VcsError(f"p4 {args[0]} returned invalid JSON: {line}") from exc
            if not isinstance(record, dict):
                raise VcsError(f"p4 {args[0]} returned an invalid record")
            try:
                severity = int(record.get("severity", 0))
            except (ValueError, TypeError) as exc:
                raise VcsError(f"p4 {args[0]} returned an invalid severity") from exc
            if record.get("code") == "error" or severity >= 2:
                # EV_EMPTY is p4's structured 'nothing matched', not an authentication,
                # network, or permissions error. Never turn those into a clean workspace.
                if empty_ok and str(record.get("generic")) == "17":
                    empty = True
                else:
                    errors.append(str(record.get("data") or record))
            else:
                records.append(record)
        if errors or result.stderr.strip() or (not result.ok and not empty):
            detail = "\n".join(errors) or result.output or "(no output)"
            raise VcsError(f"p4 {args[0]} failed:\n{detail}")
        return records

    def _local(self, path: Path | str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise VcsError(f"Perforce path is outside the managed project: {path}")
        return candidate

    def _spec(self, path: Path | str, *, directory: bool = False) -> str:
        value = self._local(path).as_posix()
        # Shell quoting alone is not enough: these characters have filespec semantics
        # inside p4. A literal ellipsis cannot be represented as a safe file selection.
        if "..." in value:
            raise VcsError("Perforce paths containing a literal '...' are unsupported")
        for old, new in (("%", "%25"), ("@", "%40"), ("#", "%23"), ("*", "%2A")):
            value = value.replace(old, new)
        return value.rstrip("/") + "/..." if directory else value

    def _scope(self) -> str:
        return self._spec(self.root, directory=True)

    async def detect(self) -> bool:
        if not self.is_available()[0]:
            return False
        try:
            info = await self._query("info", timeout=10)
            if not info or info[0].get("clientName") in (None, "", "*unknown*"):
                return False
            mappings = await self._query(
                "where", self._scope(), empty_ok=True, timeout=10
            )
            return bool(
                mappings and mappings[-1].get("path") and "unmap" not in mappings[-1]
            )
        except (VcsError, OSError, ValueError):
            return False

    async def current_branch(self) -> str | None:
        rows = await self._query("info", timeout=15)
        if not rows:
            return None
        value = rows[0].get("clientStream") or rows[0].get("clientName")
        return str(value) if value and value != "*unknown*" else None

    async def revision(self) -> str | None:
        rows = await self._query(
            "changes",
            "-m",
            "1",
            "-s",
            "submitted",
            self._scope() + "#have",
            empty_ok=True,
        )
        return str(rows[0]["change"]) if rows and "change" in rows[0] else None

    async def _record_path(self, row: dict[str, Any]) -> Path:
        raw = row.get("path") or row.get("clientFile")
        if raw and not str(raw).startswith("//"):
            return self._local(str(raw))
        depot = row.get("depotFile") or raw
        if depot:
            mappings = await self._query("where", str(depot))
            if mappings and mappings[-1].get("path") and "unmap" not in mappings[-1]:
                return self._local(str(mappings[-1]["path"]))
        raise VcsError(f"Perforce did not provide a local path for {depot or row}")

    async def _changes(self) -> list[FileChange]:
        opened = await self._query(
            "fstat", "-Ro", "-Or", "-T", _FIELDS, self._scope(), empty_ok=True
        )
        preview = await self._query(
            "reconcile", "-n", "-l", self._scope(), empty_ok=True
        )
        changes: dict[str, FileChange] = {}
        depot_paths: dict[str, str] = {}
        for row in opened:
            if "action" not in row:
                continue
            path = (
                (await self._record_path(row))
                .relative_to(self.root.resolve())
                .as_posix()
            )
            if row.get("depotFile"):
                depot_paths[str(row["depotFile"])] = path
            changes[path] = FileChange(
                path=path,
                kind=(
                    "conflicted"
                    if int(row.get("unresolved", 0))
                    else _ACTIONS.get(str(row["action"]), "unknown")
                ),
                staged=True,
                binary=str(row.get("type", "")).startswith("binary"),
            )
        for row in opened:
            if str(row.get("action", "")).startswith("move/") and row.get("movedFile"):
                path = depot_paths.get(str(row.get("depotFile")))
                if path:
                    # Preserve an out-of-project move as a missing partner; commit
                    # refuses it rather than accidentally pulling in external work.
                    changes[path].origin = depot_paths.get(
                        str(row["movedFile"]), str(row["movedFile"])
                    )
        for row in preview:
            if "action" not in row:
                continue
            path = (
                (await self._record_path(row))
                .relative_to(self.root.resolve())
                .as_posix()
            )
            if path not in changes:
                action = str(row["action"])
                changes[path] = FileChange(
                    path=path,
                    kind=(
                        "untracked"
                        if action == "add"
                        else _ACTIONS.get(action, "unknown")
                    ),
                )
            elif changes[path].kind != "conflicted":
                kind = _ACTIONS.get(str(row["action"]), "unknown")
                if kind != changes[path].kind:
                    changes[path].kind = kind
                    changes[path].staged = False
        return sorted(changes.values(), key=lambda change: change.path)

    async def status(self) -> VcsStatus:
        return VcsStatus(
            backend=self.id,
            root=self.root.resolve(),
            changes=await self._changes(),
            branch=await self.current_branch(),
            revision=await self.revision(),
        )

    def _select(
        self, changes: list[FileChange], paths: Sequence[str] | None
    ) -> tuple[list[FileChange], list[str]]:
        if paths is None:
            return changes, []
        targets = [(raw, self._local(raw)) for raw in paths if raw.strip()]
        selected = [
            change
            for change in changes
            if any(
                self._local(change.path) == target
                or self._local(change.path).is_relative_to(target)
                for _, target in targets
            )
        ]
        skipped = [
            raw
            for raw, target in targets
            if not any(
                self._local(change.path) == target
                or self._local(change.path).is_relative_to(target)
                for change in selected
            )
        ]
        return selected, skipped

    async def add(
        self,
        paths: Sequence[str],
        *,
        recursive: bool = True,
        log_fn: LogFn | None = None,
    ) -> list[str]:
        selected, _ = self._select(await self._changes(), paths or None)
        selected = [c for c in selected if c.kind == "untracked"]
        if not recursive and paths:
            targets = [self._local(raw) for raw in paths]
            selected = [
                c
                for c in selected
                if any(
                    self._local(c.path) == p or self._local(c.path).parent == p
                    for p in targets
                )
            ]
        for change in selected:
            await self._query(
                "reconcile", "-a", "-f", self._spec(change.path), log_fn=log_fn
            )
        return [c.path for c in selected]

    async def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] | None = None,
        add_untracked: bool = False,
        timeout: float | None = None,
        log_fn: LogFn | None = None,
    ) -> CommitResult:
        if not message.strip():
            raise VcsError("A submit description is required")
        all_changes = await self._changes()
        selected, skipped = self._select(all_changes, paths)
        selected = [c for c in selected if c.kind != "untracked" or add_untracked]
        branch = await self.current_branch()
        if not selected:
            return CommitResult(
                committed=False, branch=branch, message=message, skipped=skipped
            )
        names = {c.path for c in selected}
        if any(c.kind == "conflicted" for c in selected):
            raise VcsError("Resolve the selected Perforce files before submitting")
        for change in all_changes:
            if change.origin and ((change.path in names) != (change.origin in names)):
                raise VcsError(
                    "Both paths of a Perforce move must be selected for submit"
                )

        # Explicit empty Files/Stream fields prevent p4 from copying the default
        # changelist or an open stream spec into the new one (§9.6).
        description = "\n".join("\t" + line for line in message.splitlines())
        form = f"Change: new\n\nDescription:\n{description}\n\nFiles:\n\nStream:\n\n"
        created = await self._query("change", "-i", input=form, log_fn=log_fn)
        number = _change_number(created, "created")
        if number is None:
            raise VcsError(
                "Perforce created a changelist but did not return its number; inspect pending changelists"
            )
        try:
            # One exact path per command avoids Windows' command-line length limit and
            # never feeds a directory wildcard to a write operation.
            for change in selected:
                if not change.staged:
                    flags = ("-a", "-f") if change.kind == "untracked" else ("-e", "-d")
                    await self._query(
                        "reconcile",
                        "-c",
                        number,
                        *flags,
                        self._spec(change.path),
                        log_fn=log_fn,
                    )
                await self._query(
                    "reopen", "-c", number, self._spec(change.path), log_fn=log_fn
                )
            # Validate the entire numbered changelist, including files outside this
            # project. Concurrent external changes cannot silently expand this submit.
            opened = await self._query("opened", "-c", number)
            actual = {
                (await self._record_path(row))
                .relative_to(self.root.resolve())
                .as_posix()
                for row in opened
                if "depotFile" in row
            }
            if actual != names:
                raise VcsError(
                    "The pending changelist no longer matches the selected files"
                )
            submitted = await self._query(
                "submit",
                "-c",
                number,
                timeout=timeout if timeout is not None else 1800,
                log_fn=log_fn,
            )
        except VcsError as exc:
            raise VcsError(
                f"Perforce changelist {number} remains pending for recovery. {exc}"
            ) from exc
        return CommitResult(
            committed=True,
            revision=_change_number(submitted, "submitted") or number,
            branch=branch,
            files=[c.path for c in selected],
            message=message,
            skipped=skipped,
        )

    async def diff(self, path: Path | str | None = None) -> str:
        if path is not None:
            target = self._local(path)
            if not target.is_dir():
                return await self._patch(target)
            selected, _ = self._select(await self._changes(), [str(target)])
            return "\n".join(f"{c.kind}: {c.path}" for c in selected)
        return "\n".join(f"{c.kind}: {c.path}" for c in await self._changes())

    async def workspace_patch(self, path: Path | str, file: str | None = None) -> str:
        inner = (
            self
            if Path(path).resolve() == self.root.resolve()
            else type(self)(Path(path), dict(self.settings))
        )
        if file is not None:
            return await inner._patch(inner._local(file))
        return "".join(
            [await inner._patch(inner._local(c.path)) for c in await inner._changes()]
        )

    async def _patch(self, target: Path) -> str:
        rows = await self._query(
            "fstat", "-T", "depotFile,haveRev", self._spec(target), empty_ok=True
        )
        base: bytes | None = None
        if rows and int(rows[0].get("haveRev", 0)) > 0:
            # print -o preserves bytes and avoids P4DIFF launching a desktop GUI.
            with tempfile.TemporaryDirectory(prefix="voxarium-studio-p4-") as scratch:
                output = Path(scratch) / "base"
                await self.run(
                    "print",
                    "-q",
                    "-o",
                    str(output),
                    f"{rows[0]['depotFile']}#{rows[0]['haveRev']}",
                    check=True,
                )
                if not output.is_file():
                    raise VcsError("p4 print did not write the synced file revision")
                base = output.read_bytes()
        work = target.read_bytes() if target.is_file() else None
        return _patch_text(
            base, work, target.relative_to(self.root.resolve()).as_posix()
        )


def _change_number(records: list[dict[str, Any]], action: str) -> str | None:
    for row in reversed(records):
        value = (
            row.get("submittedChange") if action == "submitted" else row.get("change")
        )
        if value is not None and str(value).isdigit():
            return str(value)
        match = re.search(
            rf"\bChange (\d+) {action}\b", str(row.get("data", "")), re.IGNORECASE
        )
        if match:
            return match[1]
    return None


def _patch_text(base: bytes | None, work: bytes | None, name: str) -> str:
    if base == work:
        return ""
    old, new = (f"a/{name}" if base is not None else "/dev/null"), (
        f"b/{name}" if work is not None else "/dev/null"
    )
    if b"\0" in (base or b"") or b"\0" in (work or b""):
        return f"Binary files {old} and {new} differ\n"
    lines = difflib.unified_diff(
        (base or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
        (work or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
        fromfile=old,
        tofile=new,
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
