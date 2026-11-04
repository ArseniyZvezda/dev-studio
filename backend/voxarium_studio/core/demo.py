"""The built-in demo project — what the Studio opens when it has nothing else (§5.1).

Starting with no path used to mean "open the current directory", which dropped a
``voxarium_studio/`` into whatever folder the shell happened to be in. It now means "carry on
where I left off"; when there is nothing to carry on from — a first run, or a recent list
whose projects have all been moved away — the Studio opens a project of its own rather
than refusing to start or claiming the cwd.

That project is an ordinary managed project in every respect. The only thing built-in
about it is where it lives: under ``$VOXARIUM_STUDIO_HOME``, beside the global plugin registry and
the recently-opened list, which keeps those the only places the Studio writes outside a
managed project (§5). Nothing here is ever read back, so the folder is safe to edit or
delete — the next start with nothing to open simply writes a fresh one.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import load_config, save_config
from .paths import DATA_DIRNAME, ProjectPaths, user_config_dir
from .project import init_project

log = logging.getLogger(__name__)

DEMO_DIRNAME = "demo-project"
DEMO_PROJECT_NAME = "Demo project"

# Long lines on purpose: the block editor writes a paragraph as one line (block-editor spec
# §8.1), so a document hard-wrapped here would render with the wraps in it and be rewritten
# the first time anyone typed in it.
WELCOME_DOC = """\
# Welcome to Voxarium Studio

You are looking at the built-in demo project. The Studio opened it because it had nothing else to open: no project on the command line, and none opened here before.

This is a normal managed project, so everything in it is yours to change — that is what it is for. It lives in the Studio's own folder rather than next to a game, and nothing reads it back, so nothing here can break anything.

## What is already in it

- **AI Pipelines** — a sample graph, *Image -> Voxel Asset*. It is the reference pipeline from the spec and a template to adapt: it wants a real picture at `Assets/reference.png` and a Hunyuan3D server to reach before it will run.
- **Teams** — a sample roster with a director, a designer, a developer and a tester.
- **Documents** — this page. Documents live in `voxarium_studio/docs/` as plain Markdown files, so they can be committed alongside the project they describe.

## Opening your own project

The project name at the top of the sidebar opens the switcher: **Browse this computer...** raises your desktop's own folder dialog, and the folder you pick is opened straight away if it already holds a `voxarium_studio/`, or initialised first if it does not.

From the command line it is `voxarium_studio run <path>` — or just `voxarium_studio run`, which reopens whatever you had open last and lands back here only when there is nothing to reopen.
"""

DEMO_README = """\
# Voxarium Studio demo project

Voxarium Studio created this folder when it was started with no project to open. It is an
ordinary managed project: the Studio's own data for it is in `voxarium_studio/`, and the rest of
the folder is yours.

Deleting it loses the demo and nothing else.
"""


def demo_project_root() -> Path:
    """Where the built-in demo project lives. Follows ``$VOXARIUM_STUDIO_HOME`` like the rest of
    the machine-level state, which is also what keeps a test run out of the real one."""
    return user_config_dir() / DEMO_DIRNAME


def ensure_demo_project() -> ProjectPaths:
    """Create the demo project if it is not there yet, and return its paths.

    Idempotent, like the :func:`~voxarium_studio.core.project.init_project` it wraps: a demo the
    user has since edited, renamed or emptied is left exactly as they left it. Only the
    seed below is skipped on later starts, because re-writing it would undo their edits.
    """
    root = demo_project_root()
    fresh = not (root / DATA_DIRNAME).is_dir()
    root.mkdir(parents=True, exist_ok=True)
    paths = init_project(root)
    if fresh:
        _seed(paths)
        log.info("Created the built-in demo project at %s", root)
    return paths


def _seed(paths: ProjectPaths) -> None:
    """The parts of a demo that `voxarium_studio init` has no reason to write for a real project."""
    config = load_config(paths)
    config.project_name = DEMO_PROJECT_NAME
    save_config(paths, config)

    # The sample pipeline reads `Assets/reference.png`, so the folder it names should be
    # there for someone about to drop a picture into it.
    (paths.root / "Assets").mkdir(exist_ok=True)
    _write(paths.root / "README.md", DEMO_README)
    _write(paths.docs / "welcome.md", WELCOME_DOC)


def _write(path: Path, text: str) -> None:
    if path.exists():
        return
    path.write_text(text, encoding="utf-8")
