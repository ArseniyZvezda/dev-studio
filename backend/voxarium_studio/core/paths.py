"""Filesystem layout of a managed project.

Everything the Studio persists for a project lives under ``<managed-project>/.voxarium_studio``
(§5). The user directory holds application preferences
(§5.2), the global plugin registry (§9.4), and recently opened project paths (§5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DATA_DIRNAME = ".voxarium_studio"
LEGACY_DATA_DIRNAME = "voxarium_studio"


def _legacy_data_dir(root: Path) -> Path | None:
    legacy = root / LEGACY_DATA_DIRNAME
    if not legacy.is_dir():
        return None
    # Both installations and the global settings directory use the undotted name.
    # Selecting their parent in the project picker must never move them (§5.1).
    if legacy.resolve() in (Path(__file__).resolve().parents[1], user_config_dir().resolve()):
        return None
    if (legacy / "__init__.py").is_file() and not any(
        (legacy / name).is_file() for name in ("config.yaml", "app.db")
    ):
        return None
    return legacy


def existing_data_dir(root: Path) -> Path | None:
    """Locate data for read-only discovery, including unopened legacy projects (§5.1)."""
    current = root / DATA_DIRNAME
    return current if current.is_dir() else _legacy_data_dir(root)


def has_project_data(root: Path) -> bool:
    """Whether the directory holds a Studio managed project's data (§5.1)."""
    return existing_data_dir(root) is not None


def current_data_path(value: str, root: Path) -> str:
    """Translate a saved project-relative or absolute legacy data path (§5)."""
    if LEGACY_DATA_DIRNAME not in value.lower():
        return value
    path = Path(value.replace("\\", "/"))
    base = root / LEGACY_DATA_DIRNAME if path.is_absolute() else Path(LEGACY_DATA_DIRNAME)
    if not path.is_relative_to(base) or (root / LEGACY_DATA_DIRNAME).exists():
        return value
    updated = base.with_name(DATA_DIRNAME) / path.relative_to(base)
    result = str(updated) if "\\" in value else updated.as_posix()
    return result + value[-1] if value.endswith(("/", "\\")) else result


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Resolved absolute paths for one managed project."""

    root: Path
    """The managed project directory the user pointed the Studio at."""

    _data_dirname: str = DATA_DIRNAME

    @classmethod
    def for_project(cls, root: str | Path) -> "ProjectPaths":
        return cls(root=Path(root).expanduser().resolve())

    @classmethod
    def for_inspection(cls, root: str | Path) -> "ProjectPaths":
        """Locate an existing project for offline diagnostics without migrating it."""
        paths = cls.for_project(root)
        existing = existing_data_dir(paths.root)
        return cls(paths.root, existing.name) if existing else paths

    @property
    def data(self) -> Path:
        return self.root / self._data_dirname

    def migrate_data_dir(self) -> None:
        """Rename the entire legacy directory before any project service opens it (§5)."""
        legacy = _legacy_data_dir(self.root)
        if legacy is None:
            return
        if self.data.exists() or self.data.is_symlink():
            if not legacy.is_dir():  # Another opener completed the rename.
                return
            raise FileExistsError(f"Cannot rename {legacy} to {self.data}: both paths exist.")
        try:
            legacy.rename(self.data)
        except FileNotFoundError:
            if not self.data.is_dir():
                raise

    @property
    def config_file(self) -> Path:
        return self.data / "config.yaml"

    @property
    def db_file(self) -> Path:
        return self.data / "app.db"

    @property
    def pipelines(self) -> Path:
        return self.data / "pipelines"

    @property
    def teams(self) -> Path:
        return self.data / "teams"

    @property
    def docs(self) -> Path:
        """Project documentation the team writes and reads (§10.4) — committable."""
        return self.data / "docs"

    @property
    def temp(self) -> Path:
        """Disposable working files, one TASK-N directory per task (§10.4)."""
        return self.data / "temp"

    @property
    def skills(self) -> Path:
        """Skills the team's agents can be given, a folder each (§10.10) — committable.

        Not ``.claude/skills/``, and that is the point: nothing discovers this folder on its
        own — neither the developer's terminal nor a session — so a skill reaches exactly the
        members it was given to on the Teams page, and no one else.
        """
        return self.data / "skills"

    @property
    def automations_file(self) -> Path:
        """``.voxarium_studio/automations.yaml`` — what the project does by itself (§6.5) — committable."""
        return self.data / "automations.yaml"

    @property
    def attachments(self) -> Path:
        """Files a reviewer attached to a review comment, a folder per task (§10.4).

        Ignored by git like ``runs/``: screenshots and logs handed to one session, not a
        decision the project records.
        """
        return self.data / "attachments"

    @property
    def plugins(self) -> Path:
        return self.data / "plugins"

    @property
    def assets(self) -> Path:
        return self.data / "assets"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    @property
    def worktrees(self) -> Path:
        """Git worktrees for isolated parallel agent tasks (§10.5)."""
        return self.data / "worktrees"

    @property
    def gitignore(self) -> Path:
        return self.data / ".gitignore"

    @property
    def plastic_ignore(self) -> Path:
        """Plastic's workspace-level ignore rules, scoped to ``.voxarium_studio/`` (§5)."""
        return self.root / "ignore.conf"

    @property
    def mcp_file(self) -> Path:
        """``.mcp.json`` — MCP servers for the project's Claude agents (§10.7).

        The one file the Studio manages *outside* ``.voxarium_studio/``: the format is Claude
        Code's own, read by the `claude` CLI from the project root, and moving it
        somewhere tidier would mean the developer's terminal and the Studio disagreed
        about which servers the project has.
        """
        return self.root / ".mcp.json"

    @property
    def claude_agents(self) -> Path:
        """``.claude/agents/`` — the roster as Claude Code agents (§10.3).

        The second thing the Studio manages outside ``.voxarium_studio/``, and for the same reason
        as ``.mcp.json``: the format is Claude Code's own and ``claude --agent <name>``
        resolves a name **only** from disk, so a definition kept anywhere tidier could not
        be selected at all. Putting it here also means the developer's terminal offers the
        same team members the Studio runs.

        The project root is enough for every run: agent discovery walks up from the working
        directory, so a session started in an isolated worktree under ``.voxarium_studio/worktrees/``
        still finds these. A launch root *outside* the project is the one case it does not
        reach — see ``teams/agents.py``.
        """
        return self.root / ".claude" / "agents"

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    @property
    def all_dirs(self) -> tuple[Path, ...]:
        return (
            self.data,
            self.pipelines,
            self.teams,
            self.docs,
            self.temp,
            self.skills,
            self.attachments,
            self.plugins,
            self.assets,
            self.runs,
        )

    def is_inside_project(self, path: Path) -> bool:
        """True if ``path`` resolves to something within the managed project."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved == self.root or self.root in resolved.parents

    def is_inside_data(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved == self.data or self.data in resolved.parents


HOME_ENV_VAR = "VOXARIUM_STUDIO_HOME"


def user_config_dir() -> Path:
    """User-wide Studio state: application settings, plugin registry and recent paths.

    ``$VOXARIUM_STUDIO_HOME`` overrides the location outright. That is what keeps a test run from
    writing its throwaway projects into the developer's real recent list, and it is the
    supported way to keep two Studio installations from sharing one registry.
    """
    import os

    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "voxarium_studio"
