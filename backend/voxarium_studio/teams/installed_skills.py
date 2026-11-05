"""Read-only discovery of skills installed alongside agent CLIs (§10.10).

Only known skill roots and installed plugin manifests are inspected. No CLI/model is run,
and neither backend configuration nor installed files are changed. Public ids describe the
logical installation, not its absolute home directory, so roster ids survive moving a home
or updating a plugin. The shared .agents directory is offered to every Studio backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class InstalledSkill:
    name: str
    directory: Path
    source: str
    scope: str
    backends: tuple[str, ...]
    display_name: str


def user_home() -> Path:
    return Path.home()


def admin_skill_roots() -> tuple[Path, ...]:
    return (Path("/etc/codex/skills"),) if os.name != "nt" else ()


def _read_config(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return {}
        text = path.read_text(encoding="utf-8-sig")
        value = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _children(path: Path) -> list[Path]:
    try:
        return sorted((entry for entry in path.iterdir() if entry.is_dir()), key=lambda p: p.name.casefold())
    except OSError:
        return []


def _skill_directories(root: Path):
    """Bounded traversal supports nested synced/system skills and linked installations."""
    pending = deque([(root, 0)])
    visited: set[Path] = set()
    while pending and len(visited) < 4096:
        folder, depth = pending.popleft()
        try:
            resolved = folder.resolve()
            if resolved in visited or not folder.is_dir():
                continue
            visited.add(resolved)
            if (folder / "SKILL.md").is_file():
                yield folder
                continue  # Reference folders are part of this skill, not more skills.
            if depth >= 6:
                continue
            for child in _children(folder):
                if child.name in {"node_modules", "__pycache__", "venv", ".venv"}:
                    continue
                if child.name.startswith(".") and child.name != ".system":
                    continue
                pending.append((child, depth + 1))
        except (OSError, RuntimeError):
            continue


def _expanded(value: str, *, home: Path, base: Path) -> Path:
    if value == "~" or value.startswith(("~/", "~\\")):
        return home / value[2:] if len(value) > 1 else home
    path = Path(value)
    return path if path.is_absolute() else base / path


def _plugin_roots(directory: Path, manifest: Path) -> list[Path]:
    config = _read_config(manifest)
    if not config:
        return []
    declared = config.get("skills", "skills")
    entries = [declared] if isinstance(declared, str) else declared if isinstance(declared, list) else []
    roots = []
    for value in entries:
        if not isinstance(value, str):
            continue
        try:
            target = (directory / value).resolve()
            if target.is_relative_to(directory.resolve()):
                roots.append(target)
        except (OSError, RuntimeError):
            continue
    return roots


def discover_installed_skills(project: Path) -> dict[str, InstalledSkill]:
    """Shared, Claude Code, Codex and Qwen user/project installs plus package skills."""
    home = user_home()
    codex = _expanded(os.environ.get("CODEX_HOME") or str(home / ".codex"), home=home, base=home)
    claude = _expanded(os.environ.get("CLAUDE_CONFIG_DIR") or str(home / ".claude"), home=home, base=home)
    qwen = home / ".qwen"
    roots: list[tuple[Path, str, str, str, tuple[str, ...]]] = []

    def add(root: Path, source: str, scope: str, identity: str, backends: tuple[str, ...]) -> None:
        roots.append((root, source, scope, identity, backends))

    # Shared roots go first: aliases under backend folders are deduplicated against them.
    for scope, base in (("global", home), ("project", project)):
        add(base / ".agents" / "skills", "shared", scope, "shared", ())
    for backend, global_root, local in (
        ("claude_code", claude, ".claude"),
        ("codex", codex, ".codex"),
        ("qwen", qwen, ".qwen"),
    ):
        add(global_root / "skills", backend, "global", backend, (backend,))
        add(project / local / "skills", backend, "project", backend, (backend,))
    for root in admin_skill_roots():
        add(root, "codex", "global", "codex:admin", ("codex",))

    # Qwen explicitly supports additional personal skill directories.
    for base in (qwen, project / ".qwen"):
        config = _read_config(base / "settings.json")
        skills = config.get("skills", {})
        directories = skills.get("directories", []) if isinstance(skills, dict) else []
        if isinstance(directories, list):
            for value in directories:
                if isinstance(value, str):
                    root = _expanded(value, home=home, base=project)
                    add(root, "qwen", "global", f"qwen:custom:{value}", ("qwen",))

    # Claude's install registry records the actual version directory and installation scope.
    installed = _read_config(claude / "plugins" / "installed_plugins.json").get("plugins", {})
    enabled: dict[str, Any] = {}
    for base in (claude, project / ".claude"):
        settings = _read_config(base / "settings.json").get("enabledPlugins", {})
        if isinstance(settings, dict):
            enabled.update(settings)
    if isinstance(installed, dict):
        for plugin_id, entries in sorted(installed.items()):
            if enabled.get(plugin_id) is False:
                continue
            for entry in entries if isinstance(entries, list) else [entries]:
                if not isinstance(entry, dict) or not isinstance(entry.get("installPath"), str):
                    continue
                scope = "global" if entry.get("scope") in (None, "user", "managed") else "project"
                if scope == "project" and Path(entry.get("projectPath") or "").resolve() != project.resolve():
                    continue
                directory = Path(entry["installPath"])
                for root in _plugin_roots(directory, directory / ".claude-plugin" / "plugin.json"):
                    relative = root.relative_to(directory.resolve()).as_posix()
                    add(root, f"claude_code:{plugin_id}", scope, f"claude_code:plugin:{plugin_id}:{relative}", ("claude_code",))

    # Codex's configured plugins identify installed cache packages, unlike marketplace
    # catalogs which contain plugins the user has never installed. Pick one cached version.
    codex_config = _read_config(codex / "config.toml")
    plugins = codex_config.get("plugins", {})
    if isinstance(plugins, dict):
        for plugin_id, config in sorted(plugins.items()):
            if not isinstance(config, dict) or config.get("enabled") is not True or "@" not in plugin_id:
                continue
            plugin, marketplace = plugin_id.rsplit("@", 1)
            if not all(re.fullmatch(r"[\w.-]+", part) and part not in (".", "..") for part in (plugin, marketplace)):
                continue
            cached = codex / "plugins" / "cache" / marketplace / plugin
            versions = _children(cached)
            # Numeric version components sort naturally; the name breaks equivalent versions.
            versions.sort(key=lambda p: (tuple(int(n) for n in re.findall(r"\d+", p.name)), p.name), reverse=True)
            for directory in [*versions, cached]:
                package_roots = _plugin_roots(directory, directory / ".codex-plugin" / "plugin.json")
                if package_roots:
                    for root in package_roots:
                        relative = root.relative_to(directory.resolve()).as_posix()
                        add(root, f"codex:{plugin_id}", "global", f"codex:plugin:{plugin_id}:{relative}", ("codex",))
                    break

    for directory in _children(qwen / "extensions"):
        manifest = directory / "qwen-extension.json"
        config = _read_config(manifest)
        name = config.get("name", directory.name)
        for root in _plugin_roots(directory, manifest):
            relative = root.relative_to(directory.resolve()).as_posix()
            add(root, f"qwen:{name}", "global", f"qwen:extension:{name}:{relative}", ("qwen",))

    result: dict[str, InstalledSkill] = {}
    by_path: dict[Path, str] = {}
    for root, source, scope, identity, backends in roots:
        for directory in _skill_directories(root):
            resolved = directory.resolve()
            if resolved in by_path:
                public_id = by_path[resolved]
                previous = result[public_id]
                combined = tuple(sorted(set(previous.backends) | set(backends))) if previous.backends and backends else ()
                result[public_id] = replace(previous, backends=combined)
                continue
            relative = directory.relative_to(root).as_posix()
            digest = hashlib.sha256(f"{scope}\0{identity}\0{relative}".encode()).hexdigest()[:32]
            public_id = f"installed:{digest}"
            result[public_id] = InstalledSkill(public_id, directory, source, scope, backends, directory.name)
            by_path[resolved] = public_id
    return result
