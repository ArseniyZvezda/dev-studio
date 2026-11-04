"""Version control over HTTP (§9.6, §10.5).

Three different callers, one abstraction underneath. The VCS settings page needs to see
every candidate backend and choose between them; the Explorer needs a normalised list of
changed files to colour its tree with; a commit button and a diff pane need the two write
and read operations :class:`~voxarium_studio.vcs.base.VcsBackend` already defines. None of them
mention git — that is the whole point of §9.6, and a plugin-contributed backend appears
here the moment its plugin is enabled.

Two behaviours are worth stating outright, because both were decided rather than fallen
into:

* **"No version control" is a normal state, not an error.** ``GET /vcs/status`` answers
  200 with ``backend: null`` and no changes. The Explorer calls it unconditionally on
  every refresh and must not have to special-case a 404 for the very common case of a
  project that is not a repository. The two endpoints that *do* something —
  ``commit`` and ``diff`` — answer 409 with a message naming what to do about it.
* **A burst of status calls is one subprocess.** The Explorer refreshes on file-change
  events, which arrive in bursts, and ``git status`` on a game project's asset tree is
  not free. Calls serialise behind a lock and share a very short cached answer, so N
  overlapping refreshes cost one ``git status`` rather than N.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.context import AppContext
from ..explorer.service import PathOutsideProject
from ..vcs import CHANGE_KINDS, VcsBackend, VcsError, candidate_backends, project_relative
from .deps import Ctx

log = logging.getLogger(__name__)

router = APIRouter(prefix="/vcs", tags=["vcs"])

STATUS_TTL = 1.5
"""Seconds a status answer is reused for.

Short enough that a user who saves a file sees the colour change on the next refresh,
long enough that the burst of ``explorer.file_changed`` events one save produces collapses
into a single call to the tool."""


# --------------------------------------------------------------------------- payloads


class VcsSettingsInput(BaseModel):
    """A patch. ``exclude_unset`` is what separates "sent" from "defaulted" below."""

    backend: str | None = None
    """``auto`` (or empty) to detect, ``none`` to switch off, or a backend id."""

    settings: dict[str, dict[str, Any]] | None = None
    """Per-backend settings, merged key by key onto what is stored."""


class VcsCommitInput(BaseModel):
    message: str
    paths: list[str] | None = None
    add_untracked: bool = False


# --------------------------------------------------------------------------- helpers


def _configured(ctx: AppContext) -> str:
    """What ``config.yaml`` asks for, as the page shows it: ``auto`` | ``none`` | an id."""
    if ctx.config.vcs.disabled:
        return "none"
    return ctx.config.vcs.selected() or "auto"


def _known_ids(ctx: AppContext) -> list[str]:
    return [backend.id for backend in candidate_backends(ctx.registry.vcs_backends())]


def _require_backend(ctx: AppContext) -> VcsBackend:
    """The active backend, or a 409 a user can act on rather than a bare "no backend"."""
    if ctx.vcs is not None:
        return ctx.vcs
    if ctx.config.vcs.disabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "Version control is switched off for this project. Choose a backend on "
                "the Version control page to turn it back on."
            ),
        )
    raise HTTPException(
        status_code=409,
        detail=(
            f"{ctx.paths.root} is not under any version control the Studio recognises. "
            "Initialise a repository there, or name a backend on the Version control page."
        ),
    )


def _tool_failed(backend: VcsBackend, exc: VcsError) -> HTTPException:
    """Turn the tool's own complaint into a status code, never a 500 traceback.

    An unusable tool is 503 — the request was fine, the machine is not, and it will keep
    failing until someone installs or points at the client. Anything else is the tool
    refusing this particular operation, which is a conflict with the workspace's state.
    """
    available, reason = backend.is_available()
    if not available:
        return HTTPException(status_code=503, detail=f"{backend.name} is not usable: {reason}")
    return HTTPException(status_code=409, detail=str(exc))


async def _detected(backend: VcsBackend) -> bool:
    """``detect`` is documented as not raising; a third-party one may disagree."""
    try:
        return bool(await backend.detect())
    except Exception as exc:  # noqa: BLE001 - one bad backend must not empty the page
        log.warning("Version control backend %r failed to detect: %s", backend.id, exc)
        return False


async def _head(backend: VcsBackend | None) -> tuple[str | None, str | None]:
    if backend is None:
        return None, None
    try:
        return await backend.current_branch(), await backend.revision()
    except VcsError as exc:
        # The overview is a page, not an operation: a missing tool costs the branch
        # label, and the row explaining *why* the tool is missing is right beside it.
        log.info("Could not read %s's position: %s", backend.id, exc)
        return None, None


async def _overview(ctx: AppContext) -> dict[str, Any]:
    active = ctx.vcs
    backends: list[dict[str, Any]] = []
    for backend_cls in candidate_backends(ctx.registry.vcs_backends()):
        is_active = active is not None and active.id == backend_cls.id
        # Reuse the live instance for the active one: it is configured identically, and
        # asking the object that is actually driving keeps the page honest.
        backend = (
            active
            if is_active and active is not None
            else backend_cls(ctx.paths.root, ctx.vcs_settings(backend_cls.id))
        )
        backends.append(
            {
                **backend.to_json(),
                "plugin_id": ctx.registry.plugin_for_vcs_backend(backend_cls.id),
                "program": backend_cls.program,
                "executable": backend.executable,
                "detected": await _detected(backend),
                "active": is_active,
            }
        )

    branch, revision = await _head(active)
    return {
        "active": active.to_json() if active is not None else None,
        "configured": _configured(ctx),
        "backends": backends,
        "branch": branch,
        "revision": revision,
    }


# --------------------------------------------------------------------------- status gate


@dataclass
class _StatusGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    payload: dict[str, Any] | None = None
    backend: VcsBackend | None = None
    """Which backend produced :attr:`payload` — a re-detection invalidates it by identity."""
    expires: float = 0.0


_gates: WeakKeyDictionary[AppContext, _StatusGate] = WeakKeyDictionary()


def _gate(ctx: AppContext) -> _StatusGate:
    """One lock and one cached answer per context.

    Per *context* rather than module-global for two reasons. Opening another project
    builds a second ``AppContext`` and retires the first (§5.1), and the previous
    project's status is not an answer about this one. And an ``asyncio.Lock`` binds to the
    loop it is first awaited on, so a module-level one would outlive the loop it belongs
    to — which is a hang in production and a cross-test failure in the suite.
    """
    gate = _gates.get(ctx)
    if gate is None:
        gate = _StatusGate()
        _gates[ctx] = gate
    return gate


def _invalidate(ctx: AppContext) -> None:
    """Drop the cached answer after anything that changes what it would say.

    The gate itself stays: replacing it would hand a caller waiting on the old lock a
    different lock from the one the next caller takes, and briefly allow two tools to run.
    """
    gate = _gates.get(ctx)
    if gate is not None:
        gate.payload = None


def _normalise(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Force every ``kind`` into the closed vocabulary the wire type declares.

    A backend that invents a word of its own would otherwise reach a client that colours
    files by that value and knows only the eight in :data:`~voxarium_studio.vcs.base.CHANGE_KINDS`.
    """
    return [
        {**change, "kind": change["kind"] if change["kind"] in CHANGE_KINDS else "unknown"}
        for change in changes
    ]


def _rebase(changes: list[dict[str, Any]], workspace: Path, project: Path) -> list[dict[str, Any]]:
    """Re-express changes in the **project's** frame, dropping what falls outside it.

    A backend reports paths against its own workspace root (``VcsStatus.root``), which is
    not always the managed project: ``git status --porcelain`` prints repository-relative
    paths whatever directory it ran in, so pointing the Studio at ``<repo>/game/`` gives
    every change a ``game/`` on the front, and lists the rest of the monorepo besides.
    The Explorer looks each of its own project-relative paths up in this list, so offset
    paths do not colour one file wrongly — they colour *nothing*, which reads as a broken
    feature rather than a wrong one.

    The rule, and it is worth stating both halves:

    * the project is inside the workspace (or is the workspace) — re-base onto the
      project, and drop changes elsewhere in the workspace, because the Explorer has
      nowhere to show them;
    * the workspace is somewhere else entirely — leave every path alone. There is no
      relationship to re-base along, and inventing one would empty the list.

    "Or is the workspace" is not a shortcut past the pass, and it used to be one: with the
    two roots equal the list was returned untouched, on the reasoning that there was no
    prefix to strip. But the frame is not only about prefixes — ``cm status`` prints
    **absolute** paths, and a Plastic workspace whose ``.plastic`` sits in the project
    itself is the common layout, so every path reached the Explorer as
    ``d:\\Unreal\\Game\\Script\\x.as`` against a tree keyed by ``Script/x.as`` and nothing
    was coloured. Git never showed it, because porcelain output is already relative.

    The backends themselves are untouched: pipeline nodes and ``CommitResult.files``
    already speak the workspace's frame, and that is the frame the tool takes back. The
    rule for one path is :func:`~voxarium_studio.vcs.base.project_relative`, shared with the task
    commit (§10.4), which has to decide in the same frame which of a draft's files the
    workspace still lists.
    """
    rebased: list[dict[str, Any]] = []
    for change in changes:
        path = project_relative(change["path"], workspace=workspace, project=project)
        if path is None:
            continue
        origin = change.get("origin")
        rebased.append(
            {
                **change,
                "path": path,
                # A rename whose source is outside the project has no project-relative
                # origin to give; the field's contract is a path in this frame or None.
                "origin": (
                    project_relative(origin, workspace=workspace, project=project)
                    if origin
                    else None
                ),
            }
        )
    return rebased


async def _status_payload(ctx: AppContext) -> dict[str, Any]:
    backend = ctx.vcs
    if backend is None:
        # Not an error: most projects the Studio opens are not repositories, and the
        # Explorer asks on every refresh regardless.
        return {
            "backend": None,
            "root": str(ctx.paths.root),
            "branch": None,
            "revision": None,
            "clean": True,
            "changes": [],
        }
    try:
        status = await backend.status()
    except VcsError as exc:
        raise _tool_failed(backend, exc) from exc

    payload = status.to_json()
    payload["changes"] = _rebase(
        _normalise(payload["changes"]), Path(payload["root"]), ctx.paths.root
    )
    # `root` names the frame the paths above are in, so once they are the project's it has
    # to be the project's too — a payload whose root and paths disagree is worse than
    # either. It is also what the no-backend answer says, so the field never changes
    # meaning underneath a client.
    payload["root"] = str(ctx.paths.root)
    # `clean` is the backend's own answer about the whole *workspace*, and re-basing has
    # just thrown away everything outside the project: a monorepo whose other directories
    # are dirty would otherwise answer `clean: false` beside an empty change list, which is
    # a payload that contradicts itself. It describes what this response describes.
    payload["clean"] = not payload["changes"]
    return payload


# --------------------------------------------------------------------------- endpoints


@router.get("")
async def get_vcs(ctx: Ctx) -> dict[str, Any]:
    """Everything the Version control page renders: what is driving, and what could."""
    return await _overview(ctx)


@router.get("/status")
async def get_vcs_status(ctx: Ctx) -> dict[str, Any]:
    """The working copy's changes, normalised — what the Explorer colours files from.

    The lock is held across the subprocess deliberately: overlapping callers wait for the
    one call in flight and then find its answer already cached, rather than each starting
    a tool of their own.
    """
    gate = _gate(ctx)
    async with gate.lock:
        now = time.monotonic()
        if gate.payload is not None and gate.backend is ctx.vcs and now < gate.expires:
            return gate.payload
        payload = await _status_payload(ctx)
        gate.payload, gate.backend, gate.expires = payload, ctx.vcs, now + STATUS_TTL
        return payload


@router.put("/settings")
async def update_vcs_settings(payload: VcsSettingsInput, ctx: Ctx) -> dict[str, Any]:
    """Choose the backend and/or configure one, then re-detect and answer with the overview.

    This is a patch and it **merges**, per section *and* per key: sending
    ``{"settings": {"git": {"executable": "…"}}}`` must not drop a `plastic` section, nor
    a `workspace` key already set on git's own. Assigning a nested section wholesale has
    been a real bug here before (see the settings endpoint in `project.py`), which is why
    `exclude_unset` decides what was actually sent rather than what Pydantic defaulted.

    Clearing a key removes it rather than storing an empty string, because these keys are
    overlays: an empty ``executable`` stored here would shadow the one a plugin's own
    settings page set, and the field would look cleared while the tool it names is still
    what the plugin configured.
    """
    sent = payload.model_dump(exclude_unset=True)
    known = _known_ids(ctx)

    if "backend" in sent:
        ctx.config.vcs.backend = _checked_choice(sent["backend"], known)

    if "settings" in sent:
        stored = {key: dict(values) for key, values in ctx.config.vcs.settings.items()}
        for backend_id, values in (sent["settings"] or {}).items():
            if backend_id not in known:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown version control backend {backend_id!r}. "
                        f"Available: {', '.join(known) or 'none'}"
                    ),
                )
            section = stored.get(backend_id, {})
            for key, value in (values or {}).items():
                if value is None or (isinstance(value, str) and not value.strip()):
                    section.pop(key, None)
                else:
                    section[key] = value
            if section:
                stored[backend_id] = section
            else:
                stored.pop(backend_id, None)
        ctx.config.vcs.settings = stored

    ctx.save_config()
    await ctx.detect_vcs()
    _invalidate(ctx)
    return await _overview(ctx)


def _checked_choice(value: str | None, known: list[str]) -> str | None:
    """``auto`` is stored as absent, ``none`` as itself, an id only if something has it."""
    choice = (value or "").strip().lower()
    if choice in ("", "auto"):
        return None
    if choice in ("none", "off"):
        return choice
    if choice not in known:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown version control backend {value!r}. "
                f"Available: {', '.join(['auto', 'none', *known])}"
            ),
        )
    return choice


@router.post("/commit")
async def commit(payload: VcsCommitInput, ctx: Ctx) -> dict[str, Any]:
    """Commit through the active backend.

    ``committed: false`` is a normal answer — a pipeline that regenerated a byte-identical
    asset has nothing to commit, and neither has a user who clicked the button twice.
    """
    backend = _require_backend(ctx)
    try:
        result = await backend.commit(
            payload.message,
            paths=payload.paths or None,
            add_untracked=payload.add_untracked,
        )
    except VcsError as exc:
        raise _tool_failed(backend, exc) from exc
    _invalidate(ctx)
    return result.to_json()


@router.get("/diff")
async def get_diff(ctx: Ctx, path: str | None = None) -> dict[str, str]:
    """Uncommitted changes, for the whole workspace or one project-relative path."""
    backend = _require_backend(ctx)

    target = None
    if path and path.strip():
        try:
            target = ctx.explorer.resolve(path)
        except PathOutsideProject as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        return {"diff": await backend.diff(target)}
    except VcsError as exc:
        raise _tool_failed(backend, exc) from exc
