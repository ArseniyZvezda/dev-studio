"""``voxarium_studio/automations.yaml`` — the project's automations, committed with it (§5, §6.5).

A file rather than rows for the reason pipelines and rosters are files: what a project does by
itself when a task is done is part of how the project works, so it belongs in the diff.

The file is somebody else's as much as the page's — hand-edited, merged, written by a newer
Studio — so reading it is forgiving and writing it is careful, the way ``core/mcp.py`` treats
``.mcp.json``: an entry that does not validate is listed as invalid and **kept verbatim** by
every write, and a document that does not parse is never rewritten at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..core.paths import ProjectPaths
from .schema import Automation

log = logging.getLogger(__name__)

HEADER = (
    "# Automations for this project — edited on the Studio's Automations page.\n"
    "# One trigger and one action each; see the page for what the kinds mean.\n"
)


class AutomationsUnreadable(ValueError):
    """The file is there and does not parse; nothing may be written over it."""


class AutomationNotFound(KeyError):
    pass


@dataclass(slots=True)
class InvalidEntry:
    index: int
    id: str | None
    error: str


@dataclass(slots=True)
class Loaded:
    automations: list[Automation] = field(default_factory=list)
    invalid: list[InvalidEntry] = field(default_factory=list)
    error: str | None = None
    """Why the whole document could not be read — then both lists are empty."""


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()) if not isinstance(part, int))
    message = first.get("msg", "invalid")
    return f"{where}: {message}" if where else message


def _tidy(value: Any) -> Any:
    """Drop the text fields nobody filled in. The file is read by people and lives in a diff,
    and ``name: ''`` / ``title: ''`` / ``pattern: ''`` on every entry say nothing; each of them
    defaults to empty on the way back in. Nothing else is dropped — ``enabled: true`` and the
    list of changes are worth reading — and never ``kind``, which is how an entry is parsed.
    """
    if isinstance(value, dict):
        return {key: _tidy(item) for key, item in value.items() if item != ""}
    return value


class AutomationStore:
    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths
        self._cached: tuple[tuple[int, int] | None, Loaded] | None = None

    @property
    def file(self) -> Path:
        return self.paths.automations_file

    def relative(self) -> str:
        return self.file.relative_to(self.paths.root).as_posix()

    # ---- reading ----

    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.file.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def load(self) -> Loaded:
        """The file as it is now. Re-parsed only when it changed on disk: the engine asks
        on every event it cares about, and a hand edit must still take effect unprompted."""
        signature = self._signature()
        if self._cached is not None and self._cached[0] == signature:
            return self._cached[1]
        loaded = self._parse(self._raw_entries) if signature is not None else Loaded()
        self._cached = (signature, loaded)
        return loaded

    def _raw_entries(self) -> list[Any]:
        try:
            document = yaml.safe_load(self.file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AutomationsUnreadable(str(exc)) from exc
        if document is None:
            return []
        entries = document.get("automations") if isinstance(document, dict) else None
        if entries is None and isinstance(document, dict):
            return []
        if not isinstance(entries, list):
            raise AutomationsUnreadable("Expected a top-level `automations:` list")
        return entries

    @staticmethod
    def _parse(read: Any) -> Loaded:
        try:
            entries = read()
        except AutomationsUnreadable as exc:
            return Loaded(error=str(exc))
        loaded = Loaded()
        seen: set[str] = set()
        for index, raw in enumerate(entries):
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            try:
                automation = Automation.model_validate(raw)
            except ValidationError as exc:
                loaded.invalid.append(
                    InvalidEntry(index, raw_id if isinstance(raw_id, str) else None, _first_error(exc))
                )
                continue
            if automation.id in seen:
                loaded.invalid.append(
                    InvalidEntry(index, automation.id, f"id {automation.id!r} is used twice")
                )
                continue
            seen.add(automation.id)
            loaded.automations.append(automation)
        return loaded

    def get(self, automation_id: str) -> Automation:
        for automation in self.load().automations:
            if automation.id == automation_id:
                return automation
        raise AutomationNotFound(automation_id)

    # ---- writing ----

    def upsert(self, automation: Automation) -> Automation:
        """Replace the entry with this id where it stands, or add it at the end."""
        entries = self._raw_entries() if self.file.exists() else []
        dumped = _tidy(automation.model_dump(mode="json"))
        for index, raw in enumerate(entries):
            if isinstance(raw, dict) and raw.get("id") == automation.id:
                entries[index] = dumped
                break
        else:
            entries.append(dumped)
        self._write(entries)
        return automation

    def delete(self, automation_id: str) -> None:
        entries = self._raw_entries() if self.file.exists() else []
        kept = [raw for raw in entries if not (isinstance(raw, dict) and raw.get("id") == automation_id)]
        if len(kept) == len(entries):
            raise AutomationNotFound(automation_id)
        self._write(kept)

    def _write(self, entries: list[Any]) -> None:
        body = yaml.safe_dump(
            {"automations": entries}, sort_keys=False, allow_unicode=True, indent=2
        )
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".yaml.tmp")
        try:
            tmp.write_text(HEADER + body, encoding="utf-8", newline="\n")
            tmp.replace(self.file)
        finally:
            tmp.unlink(missing_ok=True)
        self._cached = None
