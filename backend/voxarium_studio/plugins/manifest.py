"""``plugin.yaml`` — one manifest covering both extension points (§9.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

MANIFEST_FILENAME = "plugin.yaml"


class PythonEnvSpec(BaseModel):
    """Declares an isolated venv, created with ``uv`` (§2.1)."""

    requirements: str | None = None
    """Path to a requirements.txt relative to the plugin root."""

    pyproject: str | None = None
    python: str | None = None
    """Interpreter version to provision, e.g. ``"3.12"``."""

    def is_declared(self) -> bool:
        return bool(self.requirements or self.pyproject)


class BackendSpec(BaseModel):
    entry: str | None = None
    """``module.py:function`` that receives the ``PluginRegistry``."""

    python_env: PythonEnvSpec | None = None

    @field_validator("entry")
    @classmethod
    def _validate_entry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if ":" not in value:
            raise ValueError(f"backend.entry must be '<file.py>:<func>', got {value!r}")
        return value

    def split_entry(self) -> tuple[str, str]:
        assert self.entry is not None
        module_part, _, func = self.entry.partition(":")
        return module_part, func


class FrontendNodeSpec(BaseModel):
    node_type: str
    component: str
    """Path to a ``.vue`` component, relative to the plugin root."""


class FrontendPageSpec(BaseModel):
    id: str
    title: str
    icon: str | None = None
    route: str
    component: str | None = None


def normalise_extensions(value: list[str], *, claimed_by: str) -> list[str]:
    """Lower-case, dot-prefix and drop the blanks from a list of file suffixes.

    Shared by the two manifest sections that claim file types, so `.AS`, `as` and ` .as `
    are the same claim wherever it is written.
    """
    normalised = []
    for raw in value:
        suffix = raw.strip().lower()
        if not suffix:
            continue
        normalised.append(suffix if suffix.startswith(".") else f".{suffix}")
    if not normalised:
        raise ValueError(f"A {claimed_by} must claim at least one file extension")
    return normalised


class FrontendPreviewerSpec(BaseModel):
    """A viewer the Explorer mounts for a file type (§6.2)."""

    id: str
    extensions: list[str]
    """File suffixes this previewer claims, e.g. ``[".fbx"]``."""

    component: str
    label: str | None = None
    priority: int = 0
    """Highest wins when two plugins claim the same suffix."""

    @field_validator("extensions")
    @classmethod
    def _normalise_extensions(cls, value: list[str]) -> list[str]:
        return normalise_extensions(value, claimed_by="previewer")


class FrontendGrammarSpec(BaseModel):
    """A language the Explorer highlights, taught by a plugin (§6.2).

    The grammar is a plain **TextMate grammar** — the format VS Code uses — so an existing
    ``.tmLanguage.json`` is dropped in unmodified and the plugin author writes no
    highlighting code at all. ``angelscript`` is the worked example.
    """

    id: str
    name: str
    """Human name of the language, e.g. ``AngelScript``."""

    extensions: list[str]
    """File suffixes this grammar claims, e.g. ``[".as"]``."""

    scope_name: str
    """The grammar's own root scope. Must match ``scopeName`` inside the grammar file."""

    grammar: str
    """Path to the ``.tmLanguage.json``, relative to the plugin root."""

    aliases: list[str] = Field(default_factory=list)
    """Other names for the language — what a fenced code block may be tagged with."""

    @field_validator("extensions")
    @classmethod
    def _normalise_extensions(cls, value: list[str]) -> list[str]:
        return normalise_extensions(value, claimed_by="grammar")

    @field_validator("scope_name")
    @classmethod
    def _validate_scope_name(cls, value: str) -> str:
        # A highlighter resolves a grammar by scope, so a blank one is a grammar that can
        # never be selected — worth refusing at load rather than debugging in a browser.
        if not value.strip():
            raise ValueError("A grammar must declare a scope_name, e.g. 'source.angelscript'")
        return value.strip()


class FrontendSpec(BaseModel):
    nodes: list[FrontendNodeSpec] = Field(default_factory=list)
    pages: list[FrontendPageSpec] = Field(default_factory=list)
    previewers: list[FrontendPreviewerSpec] = Field(default_factory=list)
    grammars: list[FrontendGrammarSpec] = Field(default_factory=list)
    dist: str | None = None
    """Pre-built ES module bundle (Vite library mode output) to load at runtime."""


class PluginManifest(BaseModel):
    id: str
    name: str
    version: str = "0.1.0"
    author: str = ""
    description: str = ""
    homepage: str | None = None
    enabled_by_default: bool = True
    backend: BackendSpec | None = None
    frontend: FrontendSpec | None = None
    skills: list[str] = Field(default_factory=list)
    """Skill folders relative to the plugin root, offered while the plugin is loaded (§10.10)."""

    root: Path | None = Field(default=None, exclude=True)
    """Absolute plugin directory; populated by the loader, not by the YAML."""

    scope: str = "builtin"
    """``builtin`` | ``global`` | ``project`` — where the plugin was discovered."""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Plugin id must be alphanumeric/underscore/dash, got {value!r}")
        return value

    @classmethod
    def from_file(cls, path: Path, *, scope: str = "global") -> "PluginManifest":
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path} does not contain a mapping")
        manifest = cls.model_validate(raw)
        manifest.root = path.parent.resolve()
        manifest.scope = scope
        return manifest

    @property
    def venv_path(self) -> Path | None:
        """Isolated environment lives beside the plugin source, so a heavy dependency
        set (torch etc.) is installed once per plugin rather than once per project."""
        if self.root is None or self.backend is None or self.backend.python_env is None:
            return None
        if not self.backend.python_env.is_declared():
            return None
        return self.root / ".venv"

    def venv_python(self) -> Path | None:
        venv = self.venv_path
        if venv is None:
            return None
        candidates = (venv / "Scripts" / "python.exe", venv / "bin" / "python")
        return next((c for c in candidates if c.exists()), None)
