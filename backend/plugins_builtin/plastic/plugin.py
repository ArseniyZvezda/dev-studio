"""plastic — commit generated assets to Plastic SCM / Unity Version Control.

A generation pipeline that ends by writing a mesh into ``Assets/Generated`` leaves the
artist with an uncommitted workspace and no record of which run produced what. These
nodes close that loop: the checkin node takes the file an import node just wrote and
commits it with a comment naming the pipeline.

The nodes are ``python_function`` rather than ``python_script``, unlike the ``unreal``
plugin next door. The work is one ``cm`` subprocess driven by
:class:`~plastic_vcs.PlasticBackend`, which is already async and kills the process if the
run is cancelled — so nothing blocks the event loop, and the alternative would have meant
a second copy of the Plastic logic living in a script that cannot import ``voxarium_studio``.

``cacheable=False`` throughout: these nodes mutate the workspace and read state that
changes underneath identical inputs, so a cache hit would silently skip a commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voxarium_studio.plugin_api import (
    BaseNode,
    ExecutionSpec,
    NodeContext,
    NodeTypeSchema,
    PluginRegistry,
    PortSchema,
    VcsError,
)

from plastic_vcs import PlasticBackend

CATEGORY = "Version Control"
ACCENT = "#F0A02F"

CHECKIN_TIMEOUT = 1800.0
"""Game assets are large and the server may be remote — half an hour, like the UE nodes."""


def _backend(ctx: NodeContext) -> PlasticBackend:
    """One backend per node run, configured from the plugin's project settings."""
    return PlasticBackend(ctx.project_root, ctx.settings)


def _paths_from(ctx: NodeContext) -> list[str]:
    """Merge the connected ``file`` port with the free-text ``paths`` field.

    Both are optional: with neither, an operation applies to the whole workspace, which
    is what "commit everything this pipeline just produced" usually means.
    """
    collected: list[str] = []
    single = str(ctx.get("file", "") or "").strip()
    if single:
        collected.append(single)
    for line in str(ctx.get("paths", "") or "").replace(",", "\n").splitlines():
        candidate = line.strip()
        if candidate and candidate not in collected:
            collected.append(candidate)
    return collected


async def _log_paths(ctx: NodeContext, paths: list[str]) -> None:
    if paths:
        await ctx.log(f"targets: {', '.join(paths)}")
    else:
        await ctx.log("no paths given — operating on the whole workspace")


def _file_port() -> PortSchema:
    return PortSchema(
        id="file",
        label="File",
        type="file",
        required=False,
        default="",
        description=(
            "Usually the file a node upstream just wrote, e.g. `core.write_bin_file`'s "
            "`file`. Empty means the whole workspace."
        ),
    )


def _paths_port() -> PortSchema:
    return PortSchema(
        id="paths",
        label="Extra paths",
        type="text",
        required=False,
        default="",
        connectable=False,
        description="One per line (or comma-separated). Relative paths resolve against the workspace.",
    )


class PlasticStatusNode(BaseNode):
    """What is pending in the workspace — a gate for the rest of a pipeline."""

    schema = NodeTypeSchema(
        id="plastic.status",
        name="Plastic Status",
        category=CATEGORY,
        color=ACCENT,
        icon="git-compare",
        version="1.0.0",
        description=(
            "Lists pending changes in the Plastic workspace. Use it to skip a checkin when "
            "a regenerated asset came out identical."
        ),
        inputs=[_file_port(), _paths_port()],
        outputs=[
            PortSchema(id="clean", label="Clean", type="boolean"),
            PortSchema(id="count", label="Pending changes", type="integer"),
            PortSchema(id="changes", label="Changes", type="json"),
            PortSchema(id="branch", label="Branch", type="string"),
            PortSchema(id="changeset", label="Changeset", type="string"),
        ],
        execution=ExecutionSpec(
            kind="python_function", entry="", resource_class="io", cacheable=False
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        backend = _backend(ctx)
        await _require_workspace(backend, ctx)
        status = await backend.status()
        await ctx.log(
            f"{len(status.changes)} pending change(s) on {status.branch or 'unknown branch'}"
        )
        for change in status.changes[:50]:
            await ctx.log(f"  {change.kind:>10}  {change.path}")
        return {
            "clean": status.clean,
            "count": len(status.changes),
            "changes": [change.to_json() for change in status.changes],
            "branch": status.branch or "",
            "changeset": status.revision or "",
        }


class PlasticAddNode(BaseNode):
    """Put newly generated files under source control without committing them yet."""

    schema = NodeTypeSchema(
        id="plastic.add",
        name="Plastic Add",
        category=CATEGORY,
        color=ACCENT,
        icon="file-plus",
        version="1.0.0",
        description=(
            "Adds private (untracked) items to the Plastic workspace with `cm add`. Only "
            "needed when a later checkin should not pick up private files itself."
        ),
        inputs=[
            _file_port(),
            _paths_port(),
            PortSchema(
                id="recursive",
                label="Recursive",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
            ),
        ],
        outputs=[
            PortSchema(id="paths", label="Added", type="json"),
            PortSchema(id="count", label="Count", type="integer"),
        ],
        execution=ExecutionSpec(
            kind="python_function",
            entry="",
            resource_class="io",
            timeout_seconds=CHECKIN_TIMEOUT,
            cacheable=False,
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        backend = _backend(ctx)
        await _require_workspace(backend, ctx)
        paths = _paths_from(ctx)
        await _log_paths(ctx, paths)
        added = await backend.add(
            paths, recursive=bool(ctx.get("recursive", True)), log_fn=ctx.log
        )
        await ctx.log(f"added {len(added)} item(s)")
        return {"paths": added, "count": len(added)}


class PlasticCheckinNode(BaseNode):
    """The commit node: check the workspace in and report the changeset it created."""

    schema = NodeTypeSchema(
        id="plastic.checkin",
        name="Plastic Checkin",
        category=CATEGORY,
        color=ACCENT,
        icon="git-commit-horizontal",
        version="1.0.0",
        description=(
            "Checks the workspace (or just the given paths) into Plastic and outputs the "
            "changeset. Runs `cm checkin --all`, so items merely changed on disk are "
            "included without a prior checkout — which is how pipeline-written files arrive."
        ),
        inputs=[
            PortSchema(
                id="message",
                label="Comment",
                type="text",
                required=True,
                description="The checkin comment. Name the pipeline and the run if you can.",
            ),
            _file_port(),
            _paths_port(),
            PortSchema(
                id="add_private",
                label="Include private items",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
                description="Adds untracked files as part of the checkin (`--private`).",
            ),
            PortSchema(
                id="skip_if_unchanged",
                label="Skip when unchanged",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
                description="Off means an empty workspace fails the node instead of passing through.",
            ),
        ],
        outputs=[
            PortSchema(id="changeset", label="Changeset", type="string"),
            PortSchema(id="branch", label="Branch", type="string"),
            PortSchema(id="files", label="Files", type="json"),
            PortSchema(id="committed", label="Committed", type="boolean"),
        ],
        execution=ExecutionSpec(
            kind="python_function",
            entry="",
            resource_class="io",
            timeout_seconds=CHECKIN_TIMEOUT,
            cacheable=False,
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        message = str(ctx.get("message", "") or "").strip()
        if not message:
            raise ValueError("A checkin comment is required")

        backend = _backend(ctx)
        await _require_workspace(backend, ctx)
        paths = _paths_from(ctx)
        await _log_paths(ctx, paths)

        result = await backend.commit(
            message,
            paths=paths or None,
            add_untracked=bool(ctx.get("add_private", True)),
            timeout=CHECKIN_TIMEOUT,
            log_fn=ctx.log,
        )

        if not result.committed:
            if not bool(ctx.get("skip_if_unchanged", True)):
                raise ValueError("Nothing to check in — the workspace has no pending changes")
            await ctx.log("nothing to check in — the workspace is already up to date")
        else:
            await ctx.log(
                f"checked in {len(result.files)} item(s) as {result.revision or 'a new changeset'}"
            )

        # Keys are the declared output ports, not CommitResult's field names — an edge
        # attaches by port id, so `revision` here would arrive downstream as nothing.
        return {
            "changeset": result.revision or "",
            "branch": result.branch or "",
            "files": result.files,
            "committed": result.committed,
        }


async def _require_workspace(backend: PlasticBackend, ctx: NodeContext) -> None:
    """Fail with something actionable rather than letting ``cm`` complain obscurely."""
    available, reason = backend.is_available()
    if not available:
        raise VcsError(
            f"{reason}. Install the Unity Version Control client, or set the path to `cm` "
            "on the Plastic SCM settings page."
        )
    if not await backend.detect():
        root = Path(backend.root)
        raise VcsError(
            f"{root} is not inside a Plastic workspace. Point the plugin at one on the "
            "Plastic SCM settings page if the workspace root is elsewhere."
        )
    await ctx.log(f"workspace: {backend.workspace_root() or backend.root}")


def register(registry: PluginRegistry) -> None:
    # The backend first: it is what makes the Studio itself aware of Plastic, whether or
    # not any pipeline uses the nodes below.
    registry.add_vcs_backend(PlasticBackend)

    registry.add_node_type(PlasticCheckinNode)
    registry.add_node_type(PlasticAddNode)
    registry.add_node_type(PlasticStatusNode)

    registry.add_page(
        id="plastic-settings",
        title="Plastic SCM",
        route="/plugins/plastic/settings",
        icon="git-branch",
        component="frontend/SettingsPage.js",
    )
