"""Built-in node types.

These all use ``kind="python_function"``: they are cheap, dependency-free and safe to
run inside the event loop. Anything heavier belongs in a plugin with its own venv and
``kind="python_script"`` so it runs out of process (§2.1).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from voxarium_studio.core.paths import current_data_path
from voxarium_studio.plugin_api import (
    BaseNode,
    ExecutionSpec,
    NodeContext,
    NodeTypeSchema,
    PluginRegistry,
    PortSchema,
)

CATEGORY_INPUT = "Input"
CATEGORY_TEXT = "Text"
CATEGORY_FILES = "Files"
CATEGORY_FLOW = "Flow"


class PromptInputNode(BaseNode):
    """Entry point of the reference pipeline in §8.3 — a free-text prompt."""

    schema = NodeTypeSchema(
        id="core.prompt_input",
        name="Prompt Input",
        category=CATEGORY_INPUT,
        description="A free-text prompt that feeds the rest of the pipeline.",
        icon="message-square",
        color="#4c9aff",
        inputs=[
            PortSchema(
                id="text",
                label="Prompt",
                type="text",
                required=True,
                connectable=False,
                description="The prompt text.",
            )
        ],
        outputs=[PortSchema(id="text", label="Text", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        return {"text": str(ctx.get("text", ""))}


class NumberInputNode(BaseNode):
    schema = NodeTypeSchema(
        id="core.number_input",
        name="Number Input",
        category=CATEGORY_INPUT,
        icon="hash",
        color="#4c9aff",
        inputs=[
            PortSchema(id="value", label="Value", type="number", default=0, connectable=False)
        ],
        outputs=[PortSchema(id="value", label="Value", type="number")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        return {"value": float(ctx.get("value", 0) or 0)}


class EchoNode(BaseNode):
    """Smoke-test node: proves the run -> live status -> log -> result loop end to end."""

    schema = NodeTypeSchema(
        id="core.echo",
        name="Echo",
        category=CATEGORY_FLOW,
        description="Passes a value through, optionally prefixed. Useful for debugging.",
        icon="megaphone",
        color="#9aa0aa",
        inputs=[
            PortSchema(id="value", label="Value", type="string", required=True),
            PortSchema(
                id="prefix", label="Prefix", type="string", required=False, default=""
            ),
        ],
        outputs=[PortSchema(id="value", label="Value", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        value = f"{ctx.get('prefix', '')}{ctx.get('value', '')}"
        await ctx.log(value)
        return {"value": value}


class TemplateNode(BaseNode):
    schema = NodeTypeSchema(
        id="core.template",
        name="Template",
        category=CATEGORY_TEXT,
        description="Formats a template string with {a}/{b}/{c} placeholders.",
        icon="braces",
        color="#7c5cff",
        inputs=[
            PortSchema(
                id="template",
                label="Template",
                type="text",
                default="{a} {b}",
                connectable=False,
            ),
            PortSchema(id="a", label="A", type="string", required=False, default=""),
            PortSchema(id="b", label="B", type="string", required=False, default=""),
            PortSchema(id="c", label="C", type="string", required=False, default=""),
        ],
        outputs=[PortSchema(id="text", label="Text", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        template = str(ctx.get("template", ""))
        values = {key: str(ctx.get(key, "") or "") for key in ("a", "b", "c")}
        try:
            text = template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"Template error: {exc}") from exc
        return {"text": text}


class DelayNode(BaseNode):
    """Deliberate pause — handy for watching parallel branches in the UI.

    Also the built-in demonstration of `ctx.progress()`: it is the one node whose
    remaining time is known exactly, so it is the honest place to show what the canvas
    does with a progress frame without installing anything.
    """

    schema = NodeTypeSchema(
        id="core.delay",
        name="Delay",
        category=CATEGORY_FLOW,
        icon="timer",
        color="#9aa0aa",
        inputs=[
            PortSchema(id="value", label="Value", type="string", required=False, default=""),
            PortSchema(
                id="seconds",
                label="Seconds",
                type="number",
                default=1,
                minimum=0,
                maximum=600,
                connectable=False,
            ),
        ],
        outputs=[PortSchema(id="value", label="Value", type="string")],
        execution=ExecutionSpec(
            kind="python_function", entry="", resource_class="io", cacheable=False
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        seconds = max(0.0, min(600.0, float(ctx.get("seconds", 1) or 0)))
        await ctx.log(f"sleeping {seconds}s")
        # Ten frames whatever the duration: a progress bar is read by eye, so the useful
        # number is how often it moves, not how much of the wait each step covers.
        steps = 10
        for step in range(steps):
            await asyncio.sleep(seconds / steps)
            await ctx.progress(
                percent=(step + 1) * 100 / steps,
                label="Sleeping",
                eta_s=seconds * (steps - step - 1) / steps,
            )
        return {"value": ctx.get("value", "")}


def _resolve_under_project(ctx: NodeContext, raw: str) -> Path:
    """Keep file nodes inside the managed project — they run with the server's rights."""
    path = Path(current_data_path(raw, ctx.project_root))
    if not path.is_absolute():
        path = ctx.project_root / path
    resolved = path.resolve()
    root = ctx.project_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Path escapes the managed project: {raw}")
    return resolved


def _destination(ctx: NodeContext, raw: str, default_name: str) -> Path:
    """Where a write node puts what it was handed: the path given, or a sensible default.

    An empty path means ``.voxarium_studio/assets`` — where a pipeline's own output belongs when
    nobody chose somewhere better. A path that names a *directory* — one that exists, or one
    typed with a trailing separator — is treated as one rather than written over: "put it in
    here" is what a folder in that field means, and answering it by replacing the folder with
    a file is never what was meant.
    """
    stripped = raw.strip()
    if not stripped:
        target = ctx.assets_dir / default_name
    else:
        target = _resolve_under_project(ctx, stripped)
        if target.is_dir() or stripped.endswith(("/", "\\")):
            target = target / default_name
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


class WriteTextFileNode(BaseNode):
    """Text out — the other half of ``core.read_text_file``, and the plain case."""

    schema = NodeTypeSchema(
        id="core.write_text_file",
        name="Write Text File",
        category=CATEGORY_FILES,
        description=(
            "Writes text to a file inside the project, encoded UTF-8. Defaults to "
            ".voxarium_studio/assets when no path is given."
        ),
        icon="file-plus",
        color="#2fbf71",
        inputs=[
            PortSchema(
                id="text",
                label="Text",
                type="text",
                required=True,
                description="What lands in the file.",
            ),
            PortSchema(
                id="path",
                label="Path",
                type="string",
                required=False,
                default="",
                description=(
                    "Relative to the project root. Empty writes .voxarium_studio/assets/output.txt, "
                    "and a directory takes output.txt inside it."
                ),
                connectable=True,
            ),
        ],
        outputs=[PortSchema(id="file", label="File", type="file")],
        execution=ExecutionSpec(
            kind="python_function", entry="", resource_class="io", cacheable=False
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        text = str(ctx.get("text", "") or "")
        target = _destination(ctx, str(ctx.get("path", "") or ""), "output.txt")
        await asyncio.to_thread(target.write_text, text, "utf-8")
        await ctx.log(f"wrote {len(text)} chars to {target}")
        return {"file": str(target)}


class ReadTextFileNode(BaseNode):
    """Text in: a file decoded as UTF-8, for anything downstream that works on strings."""

    schema = NodeTypeSchema(
        id="core.read_text_file",
        name="Read Text File",
        category=CATEGORY_FILES,
        description="Reads a file inside the project as UTF-8 text.",
        icon="file-text",
        color="#2fbf71",
        inputs=[
            PortSchema(
                id="path",
                label="Path",
                type="file",
                required=True,
                description="Relative to the project root, or absolute inside it.",
            )
        ],
        outputs=[
            PortSchema(id="text", label="Text", type="string"),
            PortSchema(
                id="size",
                label="Size",
                type="integer",
                description="Characters decoded — not bytes on disk, which UTF-8 rarely matches.",
            ),
        ],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="io"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("path", "") or "")
        if not raw.strip():
            raise ValueError("No path provided")
        path = _resolve_under_project(ctx, raw)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        text = await asyncio.to_thread(path.read_text, "utf-8")
        await ctx.log(f"read {len(text)} chars from {path}")
        return {"text": text, "size": len(text)}


class WriteBinFileNode(BaseNode):
    """Bytes out, whatever they are — a picture, a mesh, a ``.vox``.

    What travels between nodes here is a *path*, not a blob: a node that produces something
    binary writes it and answers with where it is (§8.1), and a file port carries that. So
    this node's job is to put those bytes where the user wants them, under the name they
    want, and it copies without looking inside — the one file node that works for a format
    the Studio has never heard of.
    """

    schema = NodeTypeSchema(
        id="core.write_bin_file",
        name="Write Bin File",
        category=CATEGORY_FILES,
        description=(
            "Copies any file an upstream node produced — an image, a mesh, a .vox — to a "
            "path inside the project, byte for byte. Defaults to .voxarium_studio/assets under the "
            "source's own name."
        ),
        icon="file-output",
        color="#2fbf71",
        inputs=[
            PortSchema(
                id="data",
                label="Data",
                type="file",
                required=True,
                description=(
                    "The binary data to write, as the file an upstream node answered with. "
                    "Nothing here decodes it, so any format works."
                ),
            ),
            PortSchema(
                id="path",
                label="Path",
                type="string",
                required=False,
                default="",
                description=(
                    "Relative to the project root. Empty writes into .voxarium_studio/assets under "
                    "the source's own name, and a directory keeps that name inside it."
                ),
                connectable=True,
            ),
        ],
        outputs=[PortSchema(id="file", label="File", type="file")],
        execution=ExecutionSpec(
            kind="python_function", entry="", resource_class="io", cacheable=False
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("data", "") or "")
        if not raw.strip():
            raise ValueError("No data provided")
        source = _resolve_under_project(ctx, raw)
        if not source.is_file():
            raise FileNotFoundError(f"File not found: {source}")
        target = _destination(ctx, str(ctx.get("path", "") or ""), source.name)
        if target == source:
            # The file is already where it was asked for. Copying it onto itself empties it.
            await ctx.log(f"{target} is already the destination; nothing copied")
            return {"file": str(target)}
        await asyncio.to_thread(shutil.copyfile, source, target)
        await ctx.log(f"wrote {target.stat().st_size} bytes to {target}")
        return {"file": str(target)}


class ReadBinFileNode(BaseNode):
    """Bytes in: a file taken as it is, handed on to a node that knows the format.

    It answers with a *file* port rather than the contents, which is the currency
    ``core.write_bin_file`` accepts and the one every binary node in the Studio consumes.
    What it adds over pointing that node straight at the path is the check: the file exists,
    it is a file, it is inside the project, and its size is in the run's log before anything
    downstream tries to decode it.
    """

    schema = NodeTypeSchema(
        id="core.read_bin_file",
        name="Read Bin File",
        category=CATEGORY_FILES,
        description=(
            "Takes a file inside the project as binary data — an image, a mesh, a .vox — "
            "and passes it on to any node that reads that format. Nothing is decoded here."
        ),
        icon="file-input",
        color="#2fbf71",
        inputs=[
            PortSchema(
                id="path",
                label="Path",
                type="file",
                required=True,
                description="Relative to the project root, or absolute inside it.",
            )
        ],
        outputs=[
            PortSchema(
                id="data",
                label="Data",
                type="file",
                description="The file itself, for a node that knows what is in it.",
            ),
            PortSchema(id="size", label="Size", type="integer", description="Bytes on disk."),
        ],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="io"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("path", "") or "")
        if not raw.strip():
            raise ValueError("No path provided")
        path = _resolve_under_project(ctx, raw)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        size = path.stat().st_size
        await ctx.log(f"{path} ({size} bytes)")
        return {"data": str(path), "size": size}


class AppendNode(BaseNode):
    """Two strings, joined. The plainest thing a pipeline keeps needing.

    ``core.template`` can already do this and more, but it costs a format string to say
    "these two, in order", and a `{a}` typed into the wrong field fails the node rather
    than the eye.
    """

    schema = NodeTypeSchema(
        id="core.append",
        name="Append",
        category=CATEGORY_TEXT,
        description="Joins two strings, optionally with a separator between them.",
        icon="plus",
        color="#7c5cff",
        inputs=[
            PortSchema(id="a", label="A", type="string", required=False, default=""),
            PortSchema(id="b", label="B", type="string", required=False, default=""),
            PortSchema(
                id="separator",
                label="Separator",
                type="string",
                required=False,
                default="",
                connectable=False,
                description="Placed between the two, and only when both have content.",
            ),
        ],
        outputs=[PortSchema(id="text", label="Text", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        a = str(ctx.get("a", "") or "")
        b = str(ctx.get("b", "") or "")
        separator = str(ctx.get("separator", "") or "")
        # Only ever *between* two values: a separator emitted next to an empty side is a
        # trailing comma in the output, which is never what "join these" meant.
        return {"text": f"{a}{separator}{b}" if a and b else f"{a}{b}"}


def join_path(a: str, b: str) -> str:
    """``a`` and ``b`` with exactly one ``/`` between them, and none doubled inside either.

    Forward slashes only; a backslash is left exactly as it was typed. Every path the
    Studio hands around is forward-slashed — the Explorer reports them that way and so does
    ``core.path`` — while a backslash is a separator on one platform and an ordinary
    character in a filename on the other. Rewriting it would be guessing which.

    The two ends are deliberately kept: a leading ``/`` says "from the root" and a trailing
    one says "this is a directory", and both are things the caller wrote on purpose.
    """
    joined = f"{a}/{b}" if a and b else f"{a}{b}"
    parts = [part for part in joined.split("/") if part]
    leading = "/" if joined.startswith("/") else ""
    trailing = "/" if parts and joined.endswith("/") else ""
    return leading + "/".join(parts) + trailing


class AppendPathNode(BaseNode):
    """The same join, for paths: one ``/`` between the parts, however many were typed."""

    schema = NodeTypeSchema(
        id="core.append_path",
        name="Append Path",
        category=CATEGORY_TEXT,
        description=(
            "Joins two path fragments with a single `/`, collapsing any doubled slashes "
            "the pieces already carried. Nothing is resolved and nothing has to exist, so "
            "it can address a file some later node is going to write."
        ),
        icon="folder-tree",
        color="#7c5cff",
        inputs=[
            PortSchema(
                id="a",
                label="A",
                type="string",
                required=False,
                default="",
                description="The leading fragment. A leading `/` on it is kept.",
            ),
            PortSchema(
                id="b",
                label="B",
                type="string",
                required=False,
                default="",
                description="Appended after exactly one separator. A trailing `/` is kept.",
            ),
        ],
        outputs=[PortSchema(id="path", label="Path", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        a = str(ctx.get("a", "") or "").strip()
        b = str(ctx.get("b", "") or "").strip()
        return {"path": join_path(a, b)}


def basename(path: str) -> str:
    """The last segment of ``path``, with any trailing separators ignored.

    Reads **both** separators, where :func:`join_path` writes only ``/`` and leaves a
    backslash alone. That looks inconsistent and is the point: joining must not rewrite what
    the caller typed, while reading has to understand what it is handed — and what it is
    handed is often a Windows path, since a node that resolves a file answers with
    ``str(Path)``. Splitting on ``/`` alone would take the whole of
    ``D:\\Assets\\hero.png`` for its own name.
    """
    trimmed = path.strip().rstrip("/\\")
    if not trimmed:
        return ""
    return trimmed.replace("\\", "/").rsplit("/", 1)[-1]


def drop_extension(name: str) -> str:
    """``name`` without its final suffix — the stem, and only the last one.

    Two rules, both from `pathlib`: a leading dot is not a suffix, so `.gitignore` keeps its
    name rather than becoming nothing, and only the last one goes, so `archive.tar.gz`
    becomes `archive.tar`.
    """
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


class PathnameNode(BaseNode):
    """The tail of a path: what the file or folder at the end of it is called.

    The other half of ``core.append_path`` — one takes a path apart where the other puts one
    together — and the reason both are string work is the same. Nothing here touches the
    disk, so it names a file a later node is going to write just as readily as one that is
    already there.
    """

    schema = NodeTypeSchema(
        id="core.pathname",
        name="Pathname",
        category=CATEGORY_TEXT,
        description=(
            "Takes the name of the file or folder at the end of a path, dropping every "
            "directory in front of it. Reads `/` and `\\` alike, so an absolute Windows "
            "path works as well as a project-relative one, and a trailing separator is "
            "ignored rather than answered with an empty name."
        ),
        icon="tag",
        color="#7c5cff",
        inputs=[
            PortSchema(
                id="path",
                label="Path",
                type="string",
                required=True,
                description="Anything path-shaped. Nothing has to exist — this is string work.",
            ),
            PortSchema(
                id="remove_extension",
                label="Remove extension",
                type="boolean",
                required=False,
                default=False,
                connectable=False,
                description=(
                    "Off keeps `hero.png`; on gives `hero`. Only the last suffix goes, and a "
                    "leading dot is not one — `.gitignore` keeps its name. It is decided by "
                    "the text, not by what is on disk, so a folder called `v1.2` becomes "
                    "`v1` with this on."
                ),
            ),
        ],
        outputs=[PortSchema(id="name", label="Name", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("path", "") or "")
        if not raw.strip():
            raise ValueError("No path provided")
        name = basename(raw)
        if not name:
            # A path that is nothing but separators — `/`, `\\`, `//`. There is no final
            # segment to name, and an empty string travelling downstream is worse than
            # saying so here.
            raise ValueError(f"Path has no final segment: {raw!r}")
        if bool(ctx.get("remove_extension", False)):
            name = drop_extension(name)
        return {"name": name}


def _is_drive(path: str) -> bool:
    """``C:`` — a Windows drive letter with nothing after it, not even its separator."""
    return len(path) == 2 and path[1] == ":"


def _is_root(path: str) -> bool:
    """Nowhere left to climb: a POSIX root, or a Windows drive root such as ``C:\\``."""
    return path in ("/", "\\") or (len(path) == 3 and path[1] == ":")


def parent_path(path: str, levels: int) -> str:
    """``path`` with its last ``levels`` segments cut off — the folder it sits in.

    A cut, never a rewrite: whatever separators survive are the ones that were typed, so a
    Windows path stays a Windows path and doubled slashes inside one stay doubled. That is
    the same split :func:`basename` makes, for the same reason — reading has to understand
    both separators, writing must not guess which one the caller meant.

    Nothing distinguishes a file from a folder, because nothing here looks at the disk: one
    level above ``a/b/file.txt`` and one above ``a/b/subfolder`` are both ``a/b``. Trailing
    separators are ignored rather than counted as a level of their own.

    Answers ``""`` when there is no such ancestor — asked to climb past the first segment of
    a relative path, or past the root of an absolute one.
    """
    remaining = path.strip().rstrip("/\\")
    for _ in range(levels):
        if not remaining or _is_root(remaining):
            return ""
        cut = max(remaining.rfind("/"), remaining.rfind("\\"))
        if cut < 0:
            # A single segment: `hero.png` has a parent only in the sense that everything
            # does, and answering `.` would be inventing a path nobody wrote.
            return ""
        head = remaining[:cut].rstrip("/\\")
        if head and not _is_drive(head):
            remaining = head
        else:
            # A root is spelled with its separator. Above `/hero.png` is `/`, and above
            # `D:/hero.png` is `D:/` — a bare `D:` means "wherever D: is currently
            # pointed", which is a different place entirely.
            remaining = head + remaining[cut]
    return remaining


class ParentPathNode(BaseNode):
    """The head of a path: the folder the thing at the end of it sits in.

    ``core.pathname`` keeps the last segment and this one drops it, so between them a path
    comes apart into the two halves ``core.append_path`` puts back together. Same string
    work as both, and the same indifference to what is on disk — one level above a file and
    one above a folder are the same answer, because the text is all there is to go on.
    """

    schema = NodeTypeSchema(
        id="core.parent_path",
        name="Parent Path",
        category=CATEGORY_TEXT,
        description=(
            "Climbs a path by `levels` folders: `Assets/Models/hero.png` gives "
            "`Assets/Models`, and so does `Assets/Models/lods` — a file and a folder are "
            "the same thing to it. Reads `/` and `\\` alike and cuts rather than rewrites, "
            "so the separators come back the way they were typed."
        ),
        icon="folder-up",
        color="#7c5cff",
        inputs=[
            PortSchema(
                id="path",
                label="Path",
                type="string",
                required=True,
                description="Anything path-shaped. Nothing has to exist — this is string work.",
            ),
            PortSchema(
                id="levels",
                label="Levels",
                type="integer",
                required=False,
                default=1,
                minimum=1,
                maximum=32,
                connectable=False,
                description=(
                    "How many folders to climb. 1 is the folder the path sits in, 2 is that "
                    "folder's own parent. Asking for more than the path has is an error, "
                    "not an empty answer."
                ),
            ),
        ],
        outputs=[PortSchema(id="path", label="Path", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("path", "") or "")
        if not raw.strip():
            raise ValueError("No path provided")
        given = ctx.get("levels", 1)
        levels = 1 if given is None else int(given)
        if levels < 1:
            raise ValueError(f"Levels must be at least 1, got {levels}")
        parent = parent_path(raw, levels)
        if not parent:
            # Climbing higher than the path goes. Downstream, an empty path is read as the
            # project root — a real place, and not the one that was asked for.
            raise ValueError(f"Path has no parent {levels} level(s) up: {raw!r}")
        return {"path": parent}


_ANCHOR = re.compile(r"^(?:[A-Za-z]:[/\\]?|[/\\])")
"""What a path can start with before its first named segment: a POSIX root, a Windows
drive root, or a bare ``D:``. A doubled leading slash is read as the one root — a UNC
share is not a thing this splits apart."""


def path_components(path: str) -> list[str]:
    """``path`` split at its separators, outermost piece first.

    An absolute path's **root is a component of its own, and the first one** — ``C:\\`` is
    genuinely the folder ``C:\\Assets`` sits in, so a re-root that counted from ``Assets``
    would leave a drive letter buried in the middle of the answer. It keeps whichever
    separator it was written with, because ``D:`` and ``D:/`` are different places (the
    point :func:`parent_path` makes).

    Reads ``/`` and ``\\`` alike, for the reason :func:`basename` gives: writing must not
    guess which separator the caller meant, but reading has to understand both.
    """
    trimmed = path.strip()
    match = _ANCHOR.match(trimmed)
    anchor = match.group(0) if match else ""
    segments = [s for s in trimmed[len(anchor) :].replace("\\", "/").split("/") if s]
    return ([anchor] if anchor else []) + segments


def join_components(components: list[str]) -> str:
    """Put back together what :func:`path_components` took apart, forward-slashed.

    Unlike :func:`parent_path`, which cuts and leaves the survivors as they were typed,
    this one rebuilds the whole path and so has to choose a separator. It chooses ``/``,
    the same one :func:`join_path` writes and the one every path the Studio hands around
    is spelled with.
    """
    if not components:
        return ""
    head, *rest = components
    if head.endswith(("/", "\\", ":")):
        # A root already carries its separator — or, for a bare `D:`, deliberately none.
        return head.replace("\\", "/") + "/".join(rest)
    return "/".join([head, *rest])


def reparent_path(path: str, parents: int, new_parent: str) -> str:
    """``path`` with its outermost ``parents`` components replaced by ``new_parent``.

    The mirror of :func:`parent_path`, which cuts from the other end: this one keeps the
    tail and rewrites the head, which is what re-rooting a tree of files under a different
    top folder is. Nothing has to exist — it is string work like the rest of them.
    """
    components = path_components(path)
    if parents >= len(components):
        raise ValueError(
            f"Removing {parents} parent(s) leaves nothing of {path!r} — it has "
            f"{len(components)} component(s), and the last one is the file itself"
        )
    kept = components[parents:]
    head = new_parent.strip().rstrip("/\\").replace("\\", "/")
    if not head:
        return join_components(kept)
    if _ANCHOR.fullmatch(kept[0]):
        raise ValueError(
            f"Cannot put {new_parent!r} in front of {path!r} — its root would end up in "
            "the middle. Remove at least one parent."
        )
    return join_components([head, *kept])


class ReparentPathNode(BaseNode):
    """The other end of the path from ``core.parent_path``: the folders it starts with.

    ``core.for_each_file`` hands out paths under the folder it walked, and what is usually
    wanted next is the same file *somewhere else* — same shape, different top. Doing that
    with ``core.parent_path`` and ``core.append_path`` means knowing the depth in advance,
    which is exactly what a walk does not give you.
    """

    schema = NodeTypeSchema(
        id="core.reparent_path",
        name="Reparent Path",
        category=CATEGORY_TEXT,
        description=(
            "Swaps the top of a path: drops the first `parents` folders and puts "
            "`new_parent` in front of what is left. With 1 and `Source`, "
            "`Assets/some/folder/file.txt` becomes `Source/some/folder/file.txt`; with 2 "
            "and `Export` it becomes `Export/folder/file.txt`. The depth below the part "
            "being replaced is whatever it was, so it works on a whole tree at once."
        ),
        icon="folder-symlink",
        color="#7c5cff",
        inputs=[
            PortSchema(
                id="path",
                label="Path",
                type="string",
                required=True,
                description="Anything path-shaped. Nothing has to exist — this is string work.",
            ),
            PortSchema(
                id="parents",
                label="Parents to remove",
                type="integer",
                required=False,
                default=1,
                minimum=0,
                maximum=32,
                connectable=False,
                description=(
                    "How many folders to drop from the front. 0 keeps the path whole and "
                    "only puts the new parent above it. An absolute path's root counts as "
                    "the first one, so 1 is what replaces `C:/` or `/`."
                ),
            ),
            PortSchema(
                id="new_parent",
                label="New parent",
                type="string",
                required=False,
                default="",
                connectable=False,
                description=(
                    "What goes in front — one folder or several, `Source` or "
                    "`Build/Source`. Left empty, the removed folders are simply gone."
                ),
            ),
        ],
        outputs=[PortSchema(id="path", label="Path", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("path", "") or "")
        if not raw.strip():
            raise ValueError("No path provided")
        given = ctx.get("parents", 1)
        parents = 1 if given is None else int(given)
        if parents < 0:
            raise ValueError(f"Parents to remove cannot be negative, got {parents}")
        return {"path": reparent_path(raw, parents, str(ctx.get("new_parent", "") or ""))}


class PathNode(BaseNode):
    """A place in the project, chosen once and handed on as a string.

    The point is that it is *one* node rather than the same path retyped into four others:
    move where the assets live and every consumer moves with it.
    """

    schema = NodeTypeSchema(
        id="core.path",
        name="Path",
        category=CATEGORY_FILES,
        description=(
            "Points at a file or a folder inside the project and passes it on as a string. "
            "Pick it with Browse or type it — either way it is checked against the project "
            "boundary before anything downstream sees it."
        ),
        icon="folder-search",
        color="#2fbf71",
        inputs=[
            PortSchema(
                id="path",
                label="Path",
                type="path",
                required=True,
                connectable=False,
                description="A file or a folder, relative to the project root.",
            ),
            PortSchema(
                id="absolute",
                label="Absolute",
                type="boolean",
                required=False,
                default=False,
                connectable=False,
                description=(
                    "Off gives the project-relative form, which is what keeps a pipeline "
                    "working after the project moves. On gives the full path, for handing "
                    "to something outside the Studio."
                ),
            ),
            PortSchema(
                id="must_exist",
                label="Must exist",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
                description=(
                    "On by default, because a picked path does exist and a typo should not "
                    "travel. Turn it off to name somewhere a later node will create."
                ),
            ),
        ],
        outputs=[PortSchema(id="path", label="Path", type="string")],
        execution=ExecutionSpec(
            kind="python_function",
            entry="",
            resource_class="io",
            # What the path points at changes underneath it, and the node produces nothing
            # of its own — a cached answer would only mean the boundary check stopped being
            # re-run (8.2).
            cacheable=False,
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("path", "") or "").strip()
        if not raw:
            raise ValueError("No path provided")
        resolved = _resolve_under_project(ctx, raw)
        if bool(ctx.get("must_exist", True)) and not resolved.exists():
            raise FileNotFoundError(f"Path not found: {resolved}")
        if bool(ctx.get("absolute", False)):
            answer = str(resolved)
        else:
            relative = resolved.relative_to(ctx.project_root.resolve())
            # Forward-slashed even on Windows: that is the spelling the Explorer reports
            # and the one every other node here is handed.
            answer = relative.as_posix()
        await ctx.log(f"{answer} ({'directory' if resolved.is_dir() else 'file'})")
        return {"path": answer}


def list_files(directory: Path, *, recursive: bool) -> list[Path]:
    """Every file in ``directory``, in a stable order.

    A recursive walk takes each folder's own files before descending, and takes its
    subfolders in name order — so the same tree gives the same sequence twice running,
    which is what lets a pipeline over it be re-run and compared.

    It does not **descend into** a dotted folder. A game project's root
    holds `.git`, and stepping into it means tens of thousands of objects nobody meant to
    process; the same goes for `.venv` and the rest of them. Only the descent is skipped:
    a dotted folder the node is pointed *at* is walked normally, and no file is ever
    hidden — a `.gitignore` sitting in an ordinary folder is one of the files.

    Blocking, so callers hand it to a thread: a deep tree is real disk work and this
    module otherwise runs in the event loop.
    """
    if not recursive:
        return sorted(child for child in directory.iterdir() if child.is_file())
    found: list[Path] = []
    for parent, dirnames, filenames in os.walk(directory):
        # Assigning into the slice is what `os.walk` reads back to decide where to go.
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        found.extend(Path(parent) / name for name in sorted(filenames))
    return found


def parse_extensions(raw: str) -> tuple[str, ...]:
    """The filter field read as lowercase suffixes, in the shape ``str.endswith`` takes.

    Separated by commas, whitespace or newlines, so ``.png .jpg``, ``png, jpg`` and one per
    line all say the same thing — the same latitude ``plastic``'s path field allows, for
    the same reason: which separator a person reaches for is not worth being strict about.
    A leading dot is optional, and a leading ``*`` is tolerated because `*.png` is what a
    lifetime of file dialogs teaches the fingers to type.

    Compound suffixes work as written: `.tar.gz` matches `archive.tar.gz`, and so does
    `.gz`. That is why the comparison is against the end of the name rather than against
    `Path.suffix`, which knows only the last one — the same thing `drop_extension` says
    about the other end of the problem.
    """
    found: list[str] = []
    for token in raw.replace(",", " ").split():
        suffix = token.lstrip("*").lower()
        if not suffix or suffix == ".":
            continue
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix not in found:
            found.append(suffix)
    return tuple(found)


class ForEachFileNode(BaseNode):
    """The one built-in node that repeats: a folder in, the rest of the graph run per file.

    It is an ordinary node with an ordinary output — what makes it a loop is
    ``iterates="path"`` on its schema, which tells the engine that the list it answers
    with is a list of *turns*. Everything downstream on the canvas is the body, so the
    loop is written by connecting nodes to it and closed by not connecting any more; there
    is no second node to pair it with and nothing to keep in sync (§8.2).
    """

    schema = NodeTypeSchema(
        id="core.for_each_file",
        name="For Each File",
        category=CATEGORY_FLOW,
        description=(
            "Walks a folder and runs everything to its right once per file, handing over "
            "one path at a time. Takes an extension filter, and a recursive walk does not "
            "descend into dotted folders such as `.git`."
        ),
        icon="repeat",
        color="#39c5cf",
        inputs=[
            PortSchema(
                id="directory",
                label="Directory",
                type="directory",
                required=True,
                description="The folder to walk, relative to the project root.",
            ),
            PortSchema(
                id="recursive",
                label="Recursive",
                type="boolean",
                required=False,
                default=False,
                connectable=False,
                description=(
                    "Off takes the files sitting in the folder itself. On takes every "
                    "file underneath it as well, however deep."
                ),
            ),
            PortSchema(
                id="extensions",
                label="Extensions",
                type="string",
                required=False,
                default="",
                connectable=False,
                description=(
                    "Only files ending in one of these — `.png, .jpg` or `png jpg`, the "
                    "dot and the case optional. `.tar.gz` works as written. Empty takes "
                    "every file."
                ),
            ),
        ],
        outputs=[
            PortSchema(
                id="path",
                label="Path",
                type="string",
                description=(
                    "One file per turn, project-relative and forward-slashed — the "
                    "spelling `core.path` answers with, and what `core.reparent_path` "
                    "expects."
                ),
            )
        ],
        iterates="path",
        execution=ExecutionSpec(
            kind="python_function",
            entry="",
            resource_class="io",
            # What is in the folder changes underneath it, and a cached list would also
            # mean the body never ran (`NodeTypeSchema.iterates`).
            cacheable=False,
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("directory", "") or "").strip()
        if not raw:
            raise ValueError("No directory provided")
        directory = _resolve_under_project(ctx, raw)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {directory}")
        recursive = bool(ctx.get("recursive", False))
        extensions = parse_extensions(str(ctx.get("extensions", "") or ""))
        files = await asyncio.to_thread(list_files, directory, recursive=recursive)
        root = ctx.project_root.resolve()
        matched = [
            file for file in files if not extensions or file.name.lower().endswith(extensions)
        ]
        paths = [file.relative_to(root).as_posix() for file in matched]
        where = f"{raw}{' and everything under it' if recursive else ''}"
        if extensions:
            # Both numbers, because the interesting failure is a filter that matched
            # nothing: "0 file(s)" alone cannot say whether the folder or the filter is
            # the empty one, and the body being skipped is the same either way.
            await ctx.log(
                f"{len(paths)} of {len(files)} file(s) in {where} "
                f"match {', '.join(extensions)}"
            )
        else:
            await ctx.log(f"{len(paths)} file(s) in {where}")
        return {"path": paths}


class JsonExtractNode(BaseNode):
    schema = NodeTypeSchema(
        id="core.json_extract",
        name="JSON Extract",
        category=CATEGORY_TEXT,
        description="Reads a dotted path out of a JSON document, e.g. `data.items.0.url`.",
        icon="file-json",
        color="#7c5cff",
        inputs=[
            PortSchema(id="json", label="JSON", type="json", required=True),
            PortSchema(id="path", label="Path", type="string", default="", connectable=False),
        ],
        outputs=[PortSchema(id="value", label="Value", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        document = ctx.get("json")
        if isinstance(document, str):
            document = json.loads(document)
        pointer = str(ctx.get("path", "") or "")
        value: Any = document
        for segment in filter(None, pointer.split(".")):
            if isinstance(value, list):
                value = value[int(segment)]
            elif isinstance(value, dict):
                value = value[segment]
            else:
                raise KeyError(f"Cannot descend into {type(value).__name__} at {segment!r}")
        return {"value": value if isinstance(value, (str, int, float, bool)) else json.dumps(value)}


class HttpRequestNode(BaseNode):
    """Generic HTTP call — the built-in example of ``kind="http_api"`` (§8.1).

    The execution kind exists because many "3D generation" backends are a hosted API
    rather than local inference, so the engine has to treat an HTTP round trip as a first
    class node. This one keeps the URL in a port: ``entry`` is a template, and the values
    it consumes are not repeated in the query string or body.

    No implementation body — ``http_api`` nodes are driven entirely by the engine.
    """

    schema = NodeTypeSchema(
        id="core.http_request",
        name="HTTP Request",
        category=CATEGORY_FLOW,
        description=(
            "Calls an HTTP endpoint. A JSON response becomes the node's outputs; anything "
            "else is saved as a file artifact. Remaining inputs go in the query string "
            "(GET) or the JSON body."
        ),
        icon="globe",
        color="#39c5cf",
        inputs=[
            PortSchema(
                id="url",
                label="URL",
                type="string",
                required=True,
                description="Supports ${ENV_VAR} so an API key never lands in a committed pipeline.",
            ),
        ],
        outputs=[
            PortSchema(
                id="result",
                label="Result",
                type="json",
                description="The JSON body, or a path to the saved response for other types.",
            )
        ],
        execution=ExecutionSpec(
            kind="http_api",
            entry="{url}",
            method="GET",
            resource_class="io",
            timeout_seconds=120,
            # Responses change under the same inputs, so memoising them would be wrong.
            cacheable=False,
        ),
    )


NODE_TYPES = [
    PromptInputNode,
    NumberInputNode,
    EchoNode,
    TemplateNode,
    DelayNode,
    WriteTextFileNode,
    ReadTextFileNode,
    WriteBinFileNode,
    ReadBinFileNode,
    PathNode,
    ForEachFileNode,
    AppendNode,
    AppendPathNode,
    PathnameNode,
    ParentPathNode,
    ReparentPathNode,
    JsonExtractNode,
    HttpRequestNode,
]


def register(registry: PluginRegistry) -> None:
    for node_type in NODE_TYPES:
        registry.add_node_type(node_type)
