"""Initialisation of the ``voxarium_studio/`` data directory inside a managed project."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import DEFAULT_GITIGNORE, DEFAULT_PLASTIC_IGNORE, AppConfig, load_config, save_config
from .paths import ProjectPaths

log = logging.getLogger(__name__)

SAMPLE_PIPELINE = """\
# Sample pipeline created by `voxarium_studio init`. Safe to delete.
#
# This is spec §15.1's reference pipeline as the shipped plugin implements it, and it is a
# template to adapt rather than something to press Run on: Hunyuan3D is image-to-3D, so it
# needs a real picture at the path below and a Hunyuan3D API server to reach (Mode, Server
# URL and Check server are on the plugin's Hunyuan3D Settings page). The voxelizer and the
# preview are stdlib-only and run wherever the mesh comes from.
#
# It stops at the voxels. §8.3's fourth step — carrying the asset into an engine — is a
# node per engine now (`unreal.import_asset`), and naming one of them here would make the
# template about that engine.
#
# The id still says "prompt" because it is the file name in every project already
# initialised and the key the pipeline is fetched by; the graph behind it is what changed.
id: prompt-to-voxel-asset
name: Image -> Voxel Asset
description: Reference pipeline from the spec — a reference image to an engine-ready voxel asset.
nodes:
  - id: n1
    type: hunyuan3d.image_to_mesh
    position: { x: 0, y: 0 }
    params:
      image: Assets/reference.png
      seed: 7
  - id: n2
    type: hunyuan3d.voxelize
    position: { x: 320, y: 0 }
    params:
      resolution: 32
  - id: n3
    type: preview3d.view
    position: { x: 640, y: 0 }
edges:
  - from: { node: n1, port: mesh_file }
    to: { node: n2, port: mesh_file }
  - from: { node: n2, port: vox_file }
    to: { node: n3, port: file }
"""

SAMPLE_TEAM = """\
# Sample team created by `voxarium_studio init`. Safe to delete.
#
# `kind: human` members have no agent session: their tasks wait in an inbox until the
# person claims them. `can_assign_to` opens a sideways route — the tester below can send a
# defect straight to whoever needs to fix it instead of routing it through the director.
id: team_default
name: Main team
members:
  - id: director
    name: You
    role_title: Director
    kind: human
    manager_id: null
    responsibility: Setting priorities, accepting finished features.
    can_assign_to: ["*"]
    # What this member does, drawn wherever their name appears. Any Lucide icon name the
    # Studio's catalogue knows; leave it out for initials and a plain dot of their colour.
    avatar_icon: crown

  - id: designer
    name: Marina
    role_title: Game Designer
    manager_id: director
    responsibility: >-
      Game and level design. Write the feature specification as a project document, then
      decompose it into implementation tasks for the developer.
    can_assign_to: [dev]
    avatar_icon: gamepad-2
    model: sonnet
    allowed_tools: [Read, Write, Grep, Glob]

  - id: dev
    name: Igor
    role_title: Developer
    manager_id: director
    responsibility: >-
      Implement features against the design documents; integrate generated assets into
      the engine.
    avatar_icon: code
    model: sonnet
    allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]

  - id: qa
    name: Sofia
    role_title: QA Tester
    kind: human
    manager_id: director
    responsibility: >-
      Verify finished features by hand, then either send fixes back to the designer or the
      developer, or report the feature complete to the director.
    can_assign_to: [designer, dev]
    avatar_icon: bug
"""


def is_git_repo(root: Path) -> bool:
    if (root / ".git").exists():
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def init_project(root: str | Path, *, with_samples: bool = True) -> ProjectPaths:
    """Create ``<root>/voxarium_studio`` and everything the Studio expects inside it.

    Idempotent: preserve existing content, upgrading the Studio's ignore rules (§5).
    """
    paths = ProjectPaths.for_project(root)
    if not paths.root.exists():
        raise FileNotFoundError(f"Managed project directory does not exist: {paths.root}")
    if not paths.root.is_dir():
        raise NotADirectoryError(f"Managed project path is not a directory: {paths.root}")

    for directory in paths.all_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.gitignore.exists():
        paths.gitignore.write_text(DEFAULT_GITIGNORE, encoding="utf-8")
    else:
        # Upgrade the old generated rule without replacing the user's additions (§5).
        ignored = paths.gitignore.read_text(encoding="utf-8")
        if ignored.startswith("# Generated by Voxarium Studio."):
            updated = "".join(
                line for line in ignored.splitlines(keepends=True) if line.strip() != "app.db"
            )
            # Task scratch files are disposable and never part of the workflow (§10.4).
            if "temp/" not in {line.strip() for line in updated.splitlines()}:
                separator = "\n" if updated and not updated.endswith("\n") else ""
                updated += separator + "temp/\n"
            if updated != ignored:
                paths.gitignore.write_text(updated, encoding="utf-8")

    # Plastic has one workspace-level file; append only missing Studio rules (§5).
    if not paths.plastic_ignore.exists():
        paths.plastic_ignore.write_text(DEFAULT_PLASTIC_IGNORE, encoding="utf-8")
    else:
        ignored = paths.plastic_ignore.read_text(encoding="utf-8")
        existing = {line.strip() for line in ignored.splitlines()}
        missing = [
            line for line in DEFAULT_PLASTIC_IGNORE.splitlines()
            if not line.startswith("#") and line not in existing
        ]
        if missing:
            separator = "\n" if ignored and not ignored.endswith("\n") else ""
            with paths.plastic_ignore.open("a", encoding="utf-8") as stream:
                stream.write(separator + "\n".join(missing) + "\n")

    if not paths.config_file.exists():
        save_config(paths, AppConfig(project_name=paths.root.name))

    if with_samples:
        sample_pipeline = paths.pipelines / "prompt-to-voxel-asset.yaml"
        if not any(paths.pipelines.iterdir()):
            sample_pipeline.write_text(SAMPLE_PIPELINE, encoding="utf-8")
        sample_team = paths.teams / "default-team.yaml"
        if not any(paths.teams.iterdir()):
            sample_team.write_text(SAMPLE_TEAM, encoding="utf-8")

    return paths


def ensure_project(root: str | Path) -> tuple[ProjectPaths, AppConfig]:
    """Initialise if needed, then load config. Used on server start-up."""
    paths = init_project(root)
    return paths, load_config(paths)
