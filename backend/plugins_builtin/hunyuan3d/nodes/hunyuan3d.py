"""Hunyuan3D as the generation backend for the plugin's two generation nodes.

Hunyuan3D is **image**-to-3D and only that. Every server in the family — a local
Hunyuan3D-2.1 API server and upstream's ``repo/api_server.py``
alike — validates the request body as "one image field, exactly one", with no text field
anywhere, and the packages behind the ``local`` transport condition on a picture just the
same. A run without one is refused here rather than 422'd a moment later.

**Two shape models, and the request says which.** ``hunyuan3d-dit-v2-1`` takes one picture;
``hunyuan3d-dit-v2-mv`` takes up to four views of the same object — ``front``, ``left``,
``back``, ``right`` — and gives each its own positional embedding, so it builds the back of
a model from the view of its back instead of inventing one. That is the whole difference,
and it is why the two are separate nodes rather than one node with three optional ports:
which model ran changes the mesh, so it belongs in the §8.2 cache key, and a node whose
identity is "the multi-view model" carries it there by construction.

On the wire the choice is two fields. ``image``/``image_path`` is one picture and
``images``/``image_paths`` is a map of views; sending more than one of them is a 422 from
the schema's own validator. ``shape_model`` then names the model — ``auto`` (a view map
means ``mv``, one picture means whatever the server was started with), ``single`` or
``mv``. :class:`Options` mirrors that exactly: ``image`` **or** ``views``, never both.

Two transports, because the two ways people run this model are very different:

``server``
    HTTP to a running Hunyuan3D API server. Nothing is imported here — stdlib only — so
    it works in an unprovisioned plugin env, and the GPU may sit on another machine. This
    is the default. The request/response contract is ``server/schemas.py``:
    ``GenerationRequest`` for what goes out and ``TaskStatus``/``GenerationResult`` for
    what comes back.
``local``
    Import the model into this plugin's own venv: torch plus the ``hy3dshape``/
    ``hy3dpaint`` (2.1) or ``hy3dgen`` (2.0) packages, and several GB of weights.
    ``requirements.txt`` deliberately pins none of it — provisioning the plugin env has
    to stay cheap for everyone running the model over a server, which needs nothing
    installed here at all.

Two details of the server contract shape most of the code below:

* **It exports ``glb`` and ``obj``, never ``stl``** — and ``obj`` *with* a texture comes
  back as a zip of the OBJ, its ``.mtl`` and the PBR maps. A request for STL is served by
  asking for GLB and converting locally through :mod:`meshio`, which is also what lets
  the reference pipeline keep feeding ``hunyuan3d.voxelize``.
* **A textured generation takes minutes** (95–130 s on a 12 GB card, per the server's own
  measurements), so the queued ``/tasks`` flow is preferred over holding one ``/generate``
  connection open: its ``progress`` field is what puts a stage name and a percentage on the
  node while it waits, and its result metadata carries the true face count instead of one
  parsed back out of the file.

That second point is also why ``hunyuan_timeout`` means what it does here. It bounds how
long the **server may be silent**, not how long the model may take: on the queued transport
every answered poll resets it, so a long queue, a slow card and a textured run at 512 views
are all waited out, and only a server that has actually stopped answering ends the node.
"""

from __future__ import annotations

import base64
import http.client
import importlib
import inspect
import ipaddress
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import _voxarium_studio as td
from meshio import MeshError, count_triangles, read_triangles, write_binary_stl

DEFAULT_MODEL_PATH = "tencent/Hunyuan3D-2.1"
DEFAULT_MV_MODEL_PATH = "tencent/Hunyuan3D-2mv"
"""The multi-view checkpoint lives in its own repository, and only ``local`` mode reads
either of these — in ``server`` mode the weights are whatever the server loaded."""

DEFAULT_SERVER_URL = "http://127.0.0.1:8081"

VIEWS = ("front", "left", "back", "right")
"""``schemas.GenerationRequest.images`` keys: the front and then clockwise seen from above.
At least one, never more than four. The order here is the model's; the request's is not
significant, because the server sorts the map into it itself."""

SHAPE_MODELS = ("auto", "single", "mv")
"""``auto`` lets the input decide — a view map is ``mv``, one picture is the server's own
``--shape-model`` default. ``single`` together with a view map is a 422, deliberately: the
one-view model would have looked at exactly one of the pictures."""

FORMATS = ("stl", "glb", "obj")
"""What the node offers. ``glb`` and ``obj`` are what the server actually exports; ``stl``
is GLB converted here, and is the only one the voxelizer's STL path was ever written for."""

SERVER_FORMATS = ("glb", "obj")
"""``schemas.GenerationRequest.output_format`` — a ``Literal["glb", "obj"]``, so anything
else is a 422 from the request boundary, not a fallback."""

MODES = ("server", "local")
TRANSPORTS = ("auto", "tasks", "sync")
"""``tasks`` queues and polls, ``sync`` holds ``POST /generate`` open, ``auto`` tries the
queue and falls back when the server 404s it — which is how an upstream ``api_server.py``,
which has no ``/tasks``, identifies itself without any version sniffing."""

IMAGE_PATH_MODES = ("auto", "always", "never")
"""Whether to send ``image_path`` (a path on the server's filesystem) instead of base64.
``auto`` means "only when the server is on this machine", which is the case it exists for."""

TEXTURE_RESOLUTIONS = (512, 768)


@dataclass
class Options:
    """Everything the backend needs, flattened out of node params and plugin settings.

    Values that change the *result* arrive as node params, because those participate in
    the §8.2 cache key; the transport fields below come from plugin settings and
    deliberately do not — the same model reached over a different URL is the same model.
    """

    # --- node params: part of the cache key
    image: Path | None = None
    views: dict[str, Path] = field(default_factory=dict)
    """The multi-view input, keyed by :data:`VIEWS`. Mutually exclusive with ``image``:
    the server takes one image field and answers a request carrying two with a 422."""

    shape_model: str = "auto"
    """Which checkpoint generates the shape. Sent only when it is not ``auto``, so a
    deployment too old to know the field never sees it."""

    model_path: str = DEFAULT_MODEL_PATH
    seed: int = 0
    steps: int = 30
    guidance_scale: float = 5.0
    octree_resolution: int = 256
    num_chunks: int = 8000
    texture: bool = False
    texture_resolution: int = 512
    max_num_view: int = 0
    """0 means "let the server decide": 4 views under 16 GB of VRAM, 6 above. On a 12 GB
    card 6 views overflow into driver paging and turn a ~25 s diffusion into tens of
    minutes, so this is the single most VRAM-sensitive number in the request."""

    face_count: int = 40000
    remove_background: bool = True
    output_format: str = "stl"

    # --- plugin settings: transport and installation details
    mode: str = "server"
    server_url: str = DEFAULT_SERVER_URL
    api_key_env: str = ""
    timeout: float = 1800.0
    """How long the server may say **nothing** before the node gives up — not how long a
    generation may take. On the queued transport every answered poll resets it, so this is
    a fault detector rather than a budget; see :func:`_await_task`."""

    transport: str = "auto"
    poll_interval: float = 2.0
    image_path_mode: str = "auto"
    subfolder: str = ""
    mv_subfolder: str = ""
    """Where the multi-view checkpoint sits inside its repository, for ``local`` mode.
    Separate from ``subfolder`` because the two models are two different downloads and a
    project running both would otherwise have to keep re-typing one of them."""

    texgen_path: str = ""
    device: str = "cuda"

    @property
    def reference(self) -> Path | None:
        """The one picture that stands for this run: the single image, or the front view.

        It names the output file and, in ``local`` mode, conditions the texture pass — the
        upstream paint pipeline takes exactly one image (``input_images[0:1]``), so a
        multi-view run textures from its front and falls back to whichever view it has.
        """
        if self.image is not None:
            return self.image
        for view in VIEWS:
            if view in self.views:
                return self.views[view]
        return next(iter(self.views.values()), None)


@dataclass
class Result:
    path: Path
    triangle_count: int | None
    backend: str
    stats: dict[str, Any] = field(default_factory=dict)
    """Whatever the server reported about the run — vertices, faces, and the shape and
    texture timings separately. Empty for the local transport and the sync endpoint."""


def _clamped(values: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    """One node param as an int inside the range the Hunyuan3D server declares.

    The port schema already bounds these, but a param can also arrive from a pipeline file
    edited by hand, and the server answers an out-of-range field with a 422 from its
    request boundary — a worse error, arriving later, than clamping here.
    """
    try:
        number = int(float(values.get(key)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def options_from(
    values: dict[str, Any],
    settings: dict[str, Any],
    *,
    image: Path | None = None,
    views: dict[str, Path] | None = None,
    seed: int = 0,
    shape_model: str = "auto",
) -> Options:
    """Node params and plugin settings flattened into one options object.

    Shared by both generation node scripts, which differ only in what they put on the
    front of it: one picture, or a map of views and ``shape_model="mv"``.

    Anything that changes the result is read from ``values`` — those are the node's params,
    and params are what the §8.2 cache key covers. The transport fields come from settings,
    which it does not: pointing the plugin at a different machine running the same model is
    not a different result.

    ``face_count`` used to be on the wrong side of that line. It decimates the mesh, so it
    belongs in the key; the old ``hunyuan_face_count`` setting is still read as the
    fallback, so a project that set it before keeps its value.
    """
    views = views or {}
    face_count = values.get("face_count")
    if face_count in (None, ""):
        face_count = settings.get("hunyuan_face_count")
    background = values.get("remove_background")
    if background is None:
        background = settings.get("hunyuan_remove_background", True)
    fallback_model = DEFAULT_MV_MODEL_PATH if views else DEFAULT_MODEL_PATH

    return Options(
        image=image,
        views=views,
        shape_model=shape_model,
        model_path=str(values.get("model_path") or fallback_model).strip(),
        seed=seed,
        steps=_clamped(values, "steps", 30, 1, 100),
        guidance_scale=max(0.0, min(20.0, float(values.get("guidance_scale") or 5.0))),
        octree_resolution=_clamped(values, "octree_resolution", 256, 64, 512),
        num_chunks=_clamped(values, "num_chunks", 8000, 1000, 200000),
        texture=bool(values.get("texture")),
        texture_resolution=(
            768 if _clamped(values, "texture_resolution", 512, 512, 768) >= 640 else 512
        ),
        # 0 is not a value the server takes; it is this plugin's way of saying "unset",
        # which is what makes the server pick 4 or 6 views by how much VRAM the card has.
        # Anything else is clamped into 4–9 rather than sent as-is, because 1–3 is a 422.
        max_num_view=_clamped(values, "max_num_view", 4, 4, 9) if values.get("max_num_view") else 0,
        face_count=_clamped({"face_count": face_count}, "face_count", 40000, 1000, 500000),
        remove_background=background is not False,
        output_format=str(values.get("output_format") or "stl").lower(),

        mode=str(settings.get("hunyuan_mode") or "server").strip().lower(),
        server_url=str(settings.get("hunyuan_server_url") or DEFAULT_SERVER_URL).strip(),
        api_key_env=str(settings.get("hunyuan_api_key_env") or "").strip(),
        timeout=float(settings.get("hunyuan_timeout") or 1800),
        transport=str(settings.get("hunyuan_transport") or "auto").strip().lower(),
        poll_interval=max(0.5, float(settings.get("hunyuan_poll_interval") or 2.0)),
        image_path_mode=str(settings.get("hunyuan_image_path") or "auto").strip().lower(),
        subfolder=str(settings.get("hunyuan_subfolder") or "").strip(),
        mv_subfolder=str(settings.get("hunyuan_mv_subfolder") or "").strip(),
        texgen_path=str(settings.get("hunyuan_texgen_path") or "").strip(),
        device=str(settings.get("hunyuan_device") or "cuda").strip(),
    )


def generate(options: Options, output: Path) -> Result:
    """Produce a mesh at ``output``. The format is ``options.output_format``.

    The returned path is not always ``output``: a textured OBJ is a zip, and naming a zip
    ``.obj`` would break every consumer downstream that trusts the suffix.
    """
    if options.output_format not in FORMATS:
        td.fail(f"Unsupported output format {options.output_format!r}; expected one of {FORMATS}")
    if options.texture and options.output_format == "stl":
        td.log(
            "  note: texture is on but the output format is stl, which carries no "
            "material — switch the node to glb to keep it"
        )
    if options.image is None and not options.views:
        td.fail(
            "Hunyuan3D needs a reference image: it is image-to-3D, its API server takes "
            "`image` or `image_path` and has no text field at all, and the local packages "
            "condition on a picture too. Connect one to the node's Reference image port."
        )
    if options.image is not None and options.views:
        # The server's own validator answers this with a 422; saying it here names the two
        # fields that collided, which the status code on its own does not.
        td.fail(
            "Hunyuan3D takes one image field: a single picture or a map of views, never "
            f"both. This run has an image and {sorted(options.views)}."
        )
    unknown = sorted(set(options.views) - set(VIEWS))
    if unknown:
        td.fail(f"Unknown view(s) {unknown}; the multi-view model is conditioned on {list(VIEWS)}")
    if options.shape_model not in SHAPE_MODELS:
        td.fail(f"Unknown shape_model {options.shape_model!r}; expected one of {SHAPE_MODELS}")
    if options.shape_model == "single" and options.views:
        td.fail(
            "shape_model 'single' cannot generate from a view map — the one-view model "
            "would look at exactly one of the pictures, so the server refuses it with a "
            "422. Use the Hunyuan3D (Image → Mesh) node, or leave the shape model on 'mv'."
        )

    started = time.monotonic()
    if options.mode == "server":
        result = _generate_server(options, output)
    elif options.mode == "local":
        result = _generate_local(options, output)
    else:
        td.fail(f"Unknown hunyuan_mode {options.mode!r}; expected one of {MODES}")
    td.log(f"  hunyuan3d finished in {time.monotonic() - started:.1f}s")
    return result


# --------------------------------------------------------------------------- server


def _generate_server(options: Options, output: Path) -> Result:
    base = options.server_url.rstrip("/")
    td.progress(label="Contacting the server")
    health = _health(base, options)
    _check_texture_supported(options, health)
    _check_shape_model_supported(options, health)

    # The server has no STL exporter, so ask for the format it does have and convert.
    wanted = options.output_format
    requested = "glb" if wanted == "stl" else wanted
    if wanted == "stl":
        td.log("  the server exports glb and obj only — requesting glb and converting to stl here")

    body = _request_body(options, base, requested)
    payload, reported = _fetch(base, body, options)

    result_path = _write_result(payload, output, wanted, requested, options)
    triangles = reported.get("faces")
    if not isinstance(triangles, int) or wanted == "stl":
        # A converted mesh is not the one the server counted, and the sync endpoint
        # reports nothing at all — either way, count what actually landed on disk.
        triangles = count_triangles(result_path)
    return Result(result_path, triangles, f"hunyuan3d/server {base}", reported)


def _request_body(options: Options, base: str, requested: str) -> dict[str, Any]:
    """A ``schemas.GenerationRequest``. Every key here is one the server declares."""
    body: dict[str, Any] = {
        "texture": options.texture,
        "remove_background": options.remove_background,
        "seed": options.seed,
        "num_inference_steps": options.steps,
        "guidance_scale": options.guidance_scale,
        "octree_resolution": options.octree_resolution,
        "num_chunks": options.num_chunks,
        "face_count": options.face_count,
        "texture_resolution": options.texture_resolution,
        "output_format": requested,
        "response": "file",
    }
    # Unset means "the server picks by card size", which is the documented default and a
    # better answer than any number this plugin could guess for someone else's GPU.
    if options.max_num_view:
        body["max_num_view"] = options.max_num_view
    # Left off when it is `auto`, which is the field's own default: a server predating the
    # multi-view model has no `shape_model` at all, and a single-image run has nothing to
    # say about it either way.
    if options.shape_model != "auto":
        body["shape_model"] = options.shape_model

    # Exactly one image field. `image_path`/`image_paths` are paths on the *server's*
    # filesystem; sending a path field beside its base64 twin is a 422 from the schema's
    # own validator, and so is sending the single field beside the map.
    by_path = _use_image_path(options, base)
    if options.views:
        ordered = {view: options.views[view] for view in VIEWS if view in options.views}
        if by_path:
            body["image_paths"] = {view: str(path.resolve()) for view, path in ordered.items()}
            td.log(f"  conditioning on {len(ordered)} view(s) by path: {', '.join(ordered)}")
        else:
            body["images"] = {
                view: base64.b64encode(path.read_bytes()).decode("ascii")
                for view, path in ordered.items()
            }
            total = sum(path.stat().st_size for path in ordered.values())
            td.log(f"  conditioning on {len(ordered)} view(s) ({total} bytes): {', '.join(ordered)}")
        if options.texture and "front" not in ordered:
            # Upstream's texture pass takes `input_images[0:1]`, so the bake sees one view
            # whatever the shape saw. Worth saying out loud — a texture conditioned on a
            # side view looks wrong in a way the node's inputs do not explain.
            td.log(
                f"  note: no front view, so the texture will be baked from '{next(iter(ordered))}' "
                "— the texture pass is conditioned on a single image"
            )
        return body

    assert options.image is not None  # `generate` refuses a run with neither
    if by_path:
        body["image_path"] = str(options.image.resolve())
        td.log(f"  conditioning on {options.image.name} by path (same machine, no base64)")
    else:
        body["image"] = base64.b64encode(options.image.read_bytes()).decode("ascii")
        td.log(f"  conditioning on {options.image.name} ({options.image.stat().st_size} bytes)")
    return body


def _use_image_path(options: Options, base: str) -> bool:
    """Whether the server can read the image off disk instead of out of the request.

    Worth the check: the README measures base64 of a multi-megabyte picture as the slow
    part of an otherwise local call. It is only ever correct when the server shares this
    filesystem, so ``auto`` restricts it to loopback and ``always`` is for the case this
    code cannot detect — a share both sides mount at the same path.
    """
    if options.image_path_mode == "never":
        return False
    if options.image_path_mode == "always":
        return True
    if options.image_path_mode != "auto":
        td.log(f"  note: unknown hunyuan_image_path {options.image_path_mode!r}; treating it as auto")
    return _is_loopback(base)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _health(base: str, options: Options) -> dict[str, Any]:
    """``GET /health``, best effort.

    Never fatal: an older or partial deployment may not serve it, and the generate call
    below produces a far better error than a failed preflight would. What it buys when it
    does answer is the diagnosis the server's own README asks for — how much of the card
    somebody else's process is holding, and whether texturing is available at all.
    """
    try:
        with urllib.request.urlopen(base + "/health", timeout=min(15.0, options.timeout)) as response:
            health = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        td.log(f"  /health did not answer ({exc}); continuing anyway")
        return {}
    if not isinstance(health, dict):
        return {}

    device = health.get("gpu_name") or health.get("device") or "unknown device"
    line = f"  server: {device}, vram_mode={health.get('vram_mode', '?')}"
    used, total = health.get("vram_used_gb"), health.get("vram_total_gb")
    if isinstance(used, (int, float)) and isinstance(total, (int, float)):
        reserved = health.get("torch_reserved_gb")
        line += f", vram {used:.1f}/{total:.1f} GB used"
        if isinstance(reserved, (int, float)):
            # The gap is other processes on the card, and on a 12 GB box it is exactly
            # the margin that decides whether texturing runs in seconds or in minutes.
            line += f" ({reserved:.1f} GB this server, {max(0.0, used - reserved):.1f} GB elsewhere)"
    queue = health.get("queue_length")
    if isinstance(queue, int) and queue:
        line += f", {queue} job(s) already queued"
    if options.views:
        # Which shape model is already resident decides whether this run pays ~15 s to load
        # one or ~2 s to swap it in — the two never sit on the card together.
        resident = health.get("shape_model_on_gpu")
        if isinstance(resident, str) and resident:
            line += f", {resident} shape model on the card"
    td.log(line)
    return health


def _check_texture_supported(options: Options, health: dict[str, Any]) -> None:
    """Refuse a texture request the server has already said it cannot serve.

    It would answer 409 anyway; failing here attaches the reason it published, which is
    the difference between "409" and "started with --vram-mode shape".
    """
    if not options.texture or health.get("texture_available") is not False:
        return
    reason = health.get("texture_unavailable_reason") or "the server did not say why"
    td.fail(
        f"The Hunyuan3D server cannot generate textures: {reason}. Restart it without "
        "`--vram-mode shape`, or turn Generate texture off on the node."
    )


def _check_shape_model_supported(options: Options, health: dict[str, Any]) -> None:
    """Refuse a multi-view run against a server with no multi-view checkpoint.

    Same reasoning as the texture check, and the server agrees: it answers 409 on a
    multi-view request rather than queueing one it cannot serve. Reading its own
    ``mv_unavailable_reason`` turns "409" into the directory it looked in.
    """
    if not options.views and options.shape_model != "mv":
        return
    if health.get("mv_available") is not False:
        return
    reason = health.get("mv_unavailable_reason") or "the server did not say why"
    td.fail(
        f"The Hunyuan3D server has no multi-view checkpoint: {reason}. Download it with "
        "`scripts/download_models.py`, point the server at it with `--mv-model-path`, or "
        "use the Hunyuan3D (Image → Mesh) node, which runs the single-view model."
    )


def _fetch(base: str, body: dict[str, Any], options: Options) -> tuple[bytes, dict[str, Any]]:
    """The model bytes, plus whatever the server reported about producing them."""
    transport = options.transport if options.transport in TRANSPORTS else "auto"
    if options.transport not in TRANSPORTS:
        td.log(f"  note: unknown hunyuan_transport {options.transport!r}; treating it as auto")

    if transport in ("auto", "tasks"):
        queued = _submit_task(base, body, options, required=transport == "tasks")
        if queued is not None:
            return _await_task(base, queued, options)
        td.log("  this server has no /tasks endpoint — falling back to POST /generate")

    # No progress is possible on this path and that is the endpoint's nature, not an
    # omission: `/generate` is one request that sends nothing until the model is finished,
    # so the only honest report is that it is running. It is also why the timeout means
    # less here — a silent socket and a working GPU look identical from this side, which
    # is the strongest argument for leaving the transport on `auto`.
    td.progress(label="Generating")
    td.log(f"POST {base}/generate (nothing expected back for up to {options.timeout:.0f}s)")
    content_type, payload = _post_json(base + "/generate", body, options)
    return _decode_body(content_type, payload, base + "/generate"), {}


def _submit_task(base: str, body: dict[str, Any], options: Options, *, required: bool) -> str | None:
    """``POST /tasks`` → a uid, or None when this deployment does not have the endpoint."""
    try:
        _, payload = _post_json(base + "/tasks", body, options, missing_is_fatal=required)
    except _NotFound:
        return None
    try:
        task = json.loads(payload.decode("utf-8", errors="replace"))
        uid = task["uid"]
    except (ValueError, KeyError, TypeError) as exc:
        td.fail(f"{base}/tasks did not return a task uid: {exc}")
    ahead = task.get("queued_ahead") or 0
    td.log(f"queued as {uid}" + (f", {ahead} job(s) ahead of it" if ahead else ""))
    return str(uid)


def _await_task(base: str, uid: str, options: Options) -> tuple[bytes, dict[str, Any]]:
    """Poll ``GET /tasks/{uid}`` to completion, then download the result.

    Polling rather than one long request is what the server's README recommends for
    textured runs, and it is also the only way the node can say anything at all during
    the two minutes a textured generation takes — the stage and the percentage from the
    server's own ``progress`` field go straight onto the node on the canvas.

    **``options.timeout`` bounds silence, not the generation.** Every answered poll resets
    it, so a slow run, a long queue and a card that is paging are all things the node waits
    out; what it refuses to wait out is a server that has stopped answering. A wall clock
    over the whole job would have to be set for the worst textured run on the slowest card,
    and would then be far too loose to detect the fault it exists for.
    """
    # The floor is only a guard against a hand-edited `0` spinning on the server; the
    # settings page will not offer less than 0.5s. It used to be 0.5 here too, which was
    # invisible while the old deadline clamped the last sleep and is not now — nothing
    # else bounds this loop, so every wait is exactly this long.
    interval = max(0.05, options.poll_interval)
    poll_timeout = _poll_timeout(options)
    announced = ""
    stage = ""
    last_answer = time.monotonic()
    quiet_since: float | None = None
    last_change, signature, warned_stalled = time.monotonic(), "", False

    while True:
        try:
            # Its own short timeout rather than `options.timeout`: a poll that gets no
            # answer has to come back and be counted as silence, not sit on a dead socket
            # for the whole budget.
            status = _get_json(
                f"{base}/tasks/{uid}", options, timeout=poll_timeout, retryable=True
            )
        except _Unanswered as exc:
            silent_for = time.monotonic() - last_answer
            if silent_for >= options.timeout:
                td.fail(
                    f"The Hunyuan3D server stopped answering about task {uid}: {exc}. "
                    f"Nothing has come back for {silent_for:.0f}s. Check that it is still "
                    f"running (its own log, and GET {base}/health); if the task finished "
                    f"without us, the result is at {base}/files/{uid}."
                )
            if quiet_since is None:
                quiet_since = time.monotonic()
                td.log(f"  {uid}: no answer ({exc}) — retrying for up to {options.timeout:.0f}s")
            time.sleep(interval)
            continue

        if quiet_since is not None:
            td.log(f"  {uid}: answering again after {time.monotonic() - quiet_since:.0f}s")
            quiet_since = None
        last_answer = time.monotonic()

        state = str(status.get("status") or "")
        if state != announced:
            td.log(f"  {uid}: {state or 'no status'}")
            announced = state
        stage = _report_progress(state, status, stage)

        # A server that answers but reports the same thing for a very long time is the
        # other way a run goes wrong — a job paging through VRAM at a hundredth of its
        # normal speed, which the server's own README says to check `nvidia-smi` for. It is
        # said out loud and nothing more: the API is answering, so ending the node here
        # would be the wall clock this method exists to avoid, and the job would keep
        # running on the GPU regardless.
        current = _signature(state, status)
        if current != signature:
            last_change, signature, warned_stalled = time.monotonic(), current, False
        elif not warned_stalled and time.monotonic() - last_change >= options.timeout:
            warned_stalled = True
            td.log(
                f"  warning: {uid} has reported nothing new for "
                f"{time.monotonic() - last_change:.0f}s. Still waiting, because the server "
                "is answering — but check nvidia-smi for another process on the card."
            )

        if state == "completed":
            break
        if state == "failed":
            td.fail(f"Hunyuan3D failed on task {uid}: {status.get('error') or 'no reason given'}")
        if state not in ("queued", "running"):
            td.fail(f"Hunyuan3D returned an unexpected task status {state!r} for {uid}")
        time.sleep(interval)

    reported = status.get("result") if isinstance(status.get("result"), dict) else {}
    _log_task_stats(reported)
    td.progress(percent=100.0, label="Downloading result")
    _, payload = _get(f"{base}/files/{uid}", options)
    if not payload:
        td.fail(f"{base}/files/{uid} returned an empty body")
    return payload, reported


def _signature(state: str, status: dict[str, Any]) -> str:
    """What the server just said, reduced to the parts that change while a job progresses."""
    progress = status.get("progress")
    progress = progress if isinstance(progress, dict) else {}
    return json.dumps(
        [state, progress.get("stage"), progress.get("percent"), progress.get("detail")],
        default=str,
    )


def _poll_timeout(options: Options) -> float:
    """How long one status poll may hang before it counts as no answer.

    Short, because it is a probe and not the work: a healthy server answers
    ``/tasks/{uid}`` in milliseconds. Only the *silence* budget is allowed to span a whole
    generation, and a poll that waited that long would collapse the two back into one
    number.
    """
    return max(1.0, min(30.0, options.timeout))


def _report_progress(state: str, status: dict[str, Any], previous_stage: str) -> str:
    """Put the server's own progress onto the node, and log each stage as it is entered.

    Returns the stage now running, so the caller can hand it back next time: the log line
    is written on the *change*, since the poll repeats every couple of seconds and a line
    per poll would bury everything else the node says.

    The shape read here is `GET /tasks/{uid}`'s `progress` object. `stage`/`label` name the
    step, `percent` blends the stages by their measured share of the run so the bar climbs
    evenly instead of jumping, and `detail` locates the run inside the current stage
    wherever the pipeline reports it. A server too old to send any of it still gets a bar —
    an indeterminate one, which is what `percent=None` draws.
    """
    if state == "queued":
        td.progress(label="Queued")
        return previous_stage
    if state not in ("running", "completed"):
        return previous_stage

    progress = status.get("progress")
    if not isinstance(progress, dict):
        if state == "running":
            td.progress(label="Generating")
        return previous_stage

    stage = str(progress.get("stage") or "")
    label = str(progress.get("label") or "").strip() or stage.replace("_", " ").capitalize()
    index, count = progress.get("stage_index"), progress.get("stage_count")
    if stage and stage != previous_stage:
        numbered = isinstance(index, int) and isinstance(count, int)
        td.log(f"  {label}" + (f" ({index}/{count})" if numbered else ""))

    td.progress(
        percent=_as_number(progress.get("percent")),
        label=label or None,
        detail=str(progress.get("detail") or "").strip() or None,
        eta_s=_as_number(progress.get("eta_s")),
        step=index if isinstance(index, int) else None,
        steps=count if isinstance(count, int) else None,
    )
    return stage or previous_stage


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _log_task_stats(reported: dict[str, Any]) -> None:
    parts = []
    # Which model actually ran, said by the server rather than assumed from the request —
    # `shape_model: auto` on a single picture resolves against its `--shape-model` flag,
    # which is the one case this side cannot predict.
    if isinstance(reported.get("shape_model"), str):
        model = reported["shape_model"]
        views = reported.get("views")
        if isinstance(views, list) and views:
            model += f" on {', '.join(str(view) for view in views)}"
        parts.append(model)
    if isinstance(reported.get("faces"), int):
        parts.append(f"{reported['faces']} faces / {reported.get('vertices', '?')} vertices")
    for key, label in (("shape_seconds", "shape"), ("texture_seconds", "texture")):
        value = reported.get(key)
        if isinstance(value, (int, float)) and value:
            parts.append(f"{label} {value:.1f}s")
    if parts:
        td.log("  " + ", ".join(parts))


def _write_result(
    payload: bytes, output: Path, wanted: str, requested: str, options: Options
) -> Path:
    """Put the bytes on disk under a suffix that describes what they actually are."""
    output.parent.mkdir(parents=True, exist_ok=True)

    if wanted == "stl":
        # Convert through the GLB the server sent, rather than renaming it and hoping.
        source = output.with_suffix(".glb")
        source.write_bytes(payload)
        try:
            triangles = read_triangles(source)
        except MeshError as exc:
            td.fail(f"Could not convert the server's glb to stl: {exc}")
        reference = options.reference
        stem = reference.stem if reference is not None else output.stem
        write_binary_stl(output, triangles, f"voxarium_studio hunyuan3d | {stem[:50]}")
        source.unlink(missing_ok=True)
        td.log(f"  converted {len(triangles)} triangles from glb to {output.name}")
        return output

    if requested == "obj" and _is_zip(payload):
        # A textured OBJ is a bundle: the .obj, its .mtl and the PBR maps. Calling that
        # file `.obj` would break the voxelizer and every previewer downstream of it.
        bundle = output.with_suffix(".zip")
        bundle.write_bytes(payload)
        td.log(f"  received {len(payload)} bytes — a textured OBJ bundle, saved as {bundle.name}")
        return bundle

    output.write_bytes(payload)
    td.log(f"  received {len(payload)} bytes")
    return output


def _is_zip(payload: bytes) -> bool:
    return payload[:2] == b"PK"


# ----------------------------------------------------------------------- server: http


class _NotFound(Exception):
    """A 404, raised rather than reported so ``auto`` can probe for ``/tasks``."""


class _Unanswered(Exception):
    """The server did not answer: refused, dropped, timed out, or a 5xx.

    Raised rather than reported so a poll can retry — this is precisely the condition
    ``hunyuan_timeout`` bounds, and one dropped poll is not it. A 4xx is *not* this: the
    server answered, and it said no.
    """


def _headers(options: Options) -> dict[str, str]:
    headers = {"Accept": "*/*"}
    # Secrets stay out of the committed config: the setting names an env var (§12).
    if options.api_key_env:
        key = os.environ.get(options.api_key_env, "")
        if not key:
            td.log(f"  warning: ${options.api_key_env} is empty; sending no Authorization header")
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _post_json(
    url: str, body: dict[str, Any], options: Options, *, missing_is_fatal: bool = True
) -> tuple[str, bytes]:
    headers = dict(_headers(options), **{"Content-Type": "application/json"})
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    return _send(request, url, options, missing_is_fatal=missing_is_fatal)


def _get(
    url: str, options: Options, *, timeout: float | None = None, retryable: bool = False
) -> tuple[str, bytes]:
    return _send(
        urllib.request.Request(url, headers=_headers(options)),
        url,
        options,
        timeout=timeout,
        retryable=retryable,
    )


def _get_json(
    url: str, options: Options, *, timeout: float | None = None, retryable: bool = False
) -> dict[str, Any]:
    _, payload = _get(url, options, timeout=timeout, retryable=retryable)
    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace"))
    except ValueError as exc:
        td.fail(f"{url} did not return JSON: {exc}")
    if not isinstance(parsed, dict):
        td.fail(f"{url} returned {type(parsed).__name__}, expected an object")
    return parsed


def _send(
    request: urllib.request.Request,
    url: str,
    options: Options,
    *,
    missing_is_fatal: bool = True,
    timeout: float | None = None,
    retryable: bool = False,
) -> tuple[str, bytes]:
    """One request. ``retryable`` decides what "no answer" means for the caller.

    ``timeout`` is a **socket** timeout, which is what makes the distinction the whole
    design rests on possible: it bounds how long the connection may go without a byte, not
    how long the exchange takes. A generation that runs for twenty minutes and then sends
    its result never trips it; a server that has died at any point does.

    With ``retryable`` set, a connection failure or a 5xx raises :class:`_Unanswered` for
    the caller to sit out. Without it the same condition is fatal, which is right for a
    one-shot call where there is nothing to wait for.
    """
    limit = options.timeout if timeout is None else timeout
    try:
        with urllib.request.urlopen(request, timeout=limit) as response:
            return response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500] or "(empty body)"
        if exc.code == 404 and not missing_is_fatal:
            raise _NotFound(url) from exc
        # 5xx is the server failing to answer rather than refusing to: a restart, a proxy
        # in front of it, a worker that died. Everything else is an answer, and a wrong
        # request does not become right by being asked again.
        if retryable and exc.code >= 500:
            raise _Unanswered(f"{exc.code} {exc.reason}") from exc
        td.fail(_http_message(exc.code, exc.reason, detail, url))
    except urllib.error.URLError as exc:
        if retryable:
            raise _Unanswered(str(exc.reason)) from exc
        td.fail(
            f"Cannot reach the Hunyuan3D server at {url}: {exc.reason}. Start it with "
            "`run_server.bat` in the Hunyuan3D installation, or point hunyuan_server_url "
            "at the machine running it."
        )
    except TimeoutError as exc:
        if retryable:
            raise _Unanswered(f"silent for {limit:.0f}s") from exc
        td.fail(
            f"The Hunyuan3D server sent nothing on {url} for {limit:.0f}s. Raise "
            "hunyuan_timeout, or lower the step count / octree resolution."
        )
    except (http.client.HTTPException, OSError) as exc:
        # A connection reset or a truncated body arrives here rather than as a URLError:
        # urllib only wraps what happens while the request is being opened, and these
        # happen while the answer is being read.
        if retryable:
            raise _Unanswered(f"{type(exc).__name__}: {exc}") from exc
        td.fail(f"The connection to {url} broke: {type(exc).__name__}: {exc}")
    raise AssertionError("unreachable")  # td.fail raises SystemExit


def _http_message(code: int, reason: str, detail: str, url: str) -> str:
    """Turn the statuses the server documents into something actionable.

    409, 429 and 507 are each raised deliberately by ``server/main.py`` with a specific
    cause; passing them through as a bare status code throws that away.
    """
    prefix = f"Hunyuan3D server answered {code} {reason}: {detail}"
    if code == 409:
        return (
            prefix + " — the server refuses what it cannot load: textures when it was "
            "started with `--vram-mode shape`, and `shape_model 'mv'` when the multi-view "
            "checkpoint is not where it looked."
        )
    if code == 422:
        return (
            prefix + " — the request was rejected at the boundary. Check the node's "
            "octree resolution (64–512), steps (1–100) and face count (1000–500000), and "
            "that it sent one image field rather than a picture and a view map."
        )
    if code == 429:
        return prefix + " — the queue is full. Wait for the running jobs, or raise --max-queue."
    if code == 507:
        return (
            prefix + " — the card ran out of memory. Lower the octree resolution "
            "(192 or 128), lower num_chunks, or close other GPU applications."
        )
    return prefix


_MODEL_KEYS = ("model_base64", "mesh_base64", "model", "mesh", "data", "result", "file")
"""Keys a JSON-answering deployment might wrap the model in. ``/generate`` with
``response=file`` streams the file itself, but ``response=json`` and various hosted
wrappers base64 it into ``model_base64`` instead."""


def _decode_body(content_type: str, payload: bytes, url: str) -> bytes:
    if "json" not in content_type.lower():
        if not payload:
            td.fail(f"{url} returned an empty body")
        return payload

    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        td.fail(f"{url} announced JSON but sent something else: {exc}")
    if not isinstance(parsed, dict):
        td.fail(f"{url} returned {type(parsed).__name__}, expected an object")
    for key in _MODEL_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value:
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                td.fail(f"{url} returned {key!r} that is not valid base64: {exc}")
    # A stray `{"status": "processing"}` lands here: writing that JSON out as a mesh
    # would be worse than saying the answer carried no model.
    td.fail(f"{url} returned JSON with no model payload; keys were {sorted(parsed)[:10]}")
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- local

_SHAPE_PIPELINES = (
    ("hy3dshape.pipelines", "Hunyuan3DDiTFlowMatchingPipeline"),  # Hunyuan3D 2.1
    ("hy3dgen.shapegen", "Hunyuan3DDiTFlowMatchingPipeline"),  # Hunyuan3D 2.0
)
_PAINT_PIPELINES = (
    ("hy3dpaint.textureGenPipeline", "Hunyuan3DPaintPipeline"),  # 2.1
    ("hy3dgen.texgen", "Hunyuan3DPaintPipeline"),  # 2.0
)


def _import_first(candidates: tuple[tuple[str, str], ...]) -> Any | None:
    """First importable ``(module, attribute)`` pair, or None with the reasons logged."""
    reasons: list[str] = []
    for module_name, attribute in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # a partial install raises more than ImportError
            reasons.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        target = getattr(module, attribute, None)
        if target is None:
            reasons.append(f"{module_name}: no attribute {attribute}")
            continue
        td.log(f"  using {module_name}.{attribute}")
        return target
    for reason in reasons:
        td.log(f"  unavailable — {reason}")
    return None


def _call_filtered(target: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``target`` with only the keyword arguments its signature accepts.

    The 2.0 and 2.1 pipelines took different keywords, and distilled variants drop some
    again. Passing an unknown one is a hard TypeError several GB of weights into a run,
    which is the worst possible moment to find out.
    """
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(*args, **kwargs)  # builtins and C extensions: hope for the best

    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_any:
        return target(*args, **kwargs)

    accepted = {k: v for k, v in kwargs.items() if k in signature.parameters}
    dropped = sorted(set(kwargs) - set(accepted))
    if dropped:
        td.log(f"  note: {getattr(target, '__name__', target)} ignores {', '.join(dropped)}")
    return target(*args, **accepted)


def _generate_local(options: Options, output: Path) -> Result:
    shape_class = _import_first(_SHAPE_PIPELINES)
    if shape_class is None:
        td.fail(
            "Hunyuan3D is not installed in this plugin's environment. Install it into "
            "the hunyuan3d venv (see plugins_builtin/hunyuan3d/README.md), or set "
            "hunyuan_mode to 'server' on the Hunyuan3D Settings page and run "
            "run_server.bat instead."
        )

    device = _resolve_device(options.device)
    # The multi-view pipeline is conditioned on a dict keyed by view — the same shape the
    # server's `images` map has — while the single-view one takes the image itself. The
    # texture pass takes one picture either way, which is what `reference` answers.
    if options.views:
        conditioning: Any = {
            view: _load_image(options.views[view], options.remove_background)
            for view in VIEWS
            if view in options.views
        }
        reference = conditioning[next(iter(conditioning))]
    else:
        assert options.image is not None  # `generate` refuses a run without one
        conditioning = reference = _load_image(options.image, options.remove_background)

    # Stage names with no percentage: the shape pipeline takes a step callback and the
    # paint pipeline reports nothing at all, so a number here would be invented for half
    # the run. The bar goes indeterminate, which says "working" without claiming to know.
    td.progress(label="Loading the model")
    subfolder = (options.mv_subfolder if options.views else options.subfolder) or None
    td.log(f"loading {options.model_path} on {device}" + (f" from {subfolder}" if subfolder else ""))
    pipeline = _call_filtered(
        shape_class.from_pretrained,
        options.model_path,
        subfolder=subfolder,
        device=device,
    )

    td.progress(label="Generating the shape")
    td.log(
        f"  shape: steps={options.steps} guidance={options.guidance_scale} "
        f"octree={options.octree_resolution} chunks={options.num_chunks} seed={options.seed}"
        + (f" views={','.join(options.views)}" if options.views else "")
    )
    mesh = _first_mesh(
        _call_filtered(
            pipeline,
            image=conditioning,
            num_inference_steps=options.steps,
            guidance_scale=options.guidance_scale,
            octree_resolution=options.octree_resolution,
            num_chunks=options.num_chunks,
            generator=_generator(options.seed),
            output_type="trimesh",
        )
    )

    # Cleanup runs before texturing: reducing faces afterwards would invalidate the UVs
    # the paint pipeline just wrote.
    td.progress(label="Cleaning up the mesh")
    mesh = _postprocess(mesh, options)
    if options.texture:
        td.progress(label="Texturing")
        mesh = _paint(mesh, reference, options, device)

    td.progress(label="Exporting")
    output.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(output))
    faces = getattr(mesh, "faces", None)
    triangles = len(faces) if faces is not None else count_triangles(output)
    return Result(output, triangles, f"hunyuan3d/local {options.model_path}")


def _resolve_device(requested: str) -> str:
    if requested != "cuda":
        return requested
    try:
        import torch
    except ImportError:
        return requested
    if not torch.cuda.is_available():
        td.log("  warning: cuda requested but unavailable; falling back to cpu (very slow)")
        return "cpu"
    return requested


def _generator(seed: int) -> Any | None:
    try:
        import torch
    except ImportError:
        return None
    # The server seeds a CPU generator even for CUDA inference, so that a seed reproduces
    # across the two transports rather than only within one of them.
    return torch.Generator(device="cpu").manual_seed(seed)


def _first_mesh(outputs: Any) -> Any:
    """The shape pipelines return a list of meshes; some wrappers return one."""
    if isinstance(outputs, (list, tuple)):
        if not outputs:
            td.fail("Hunyuan3D returned no mesh")
        return outputs[0]
    return outputs


def _load_image(path: Path, remove_background: bool) -> Any:
    try:
        from PIL import Image
    except ImportError:
        td.fail("Pillow is not installed in this plugin's environment; it ships with Hunyuan3D")
    image = Image.open(str(path)).convert("RGBA")

    if not remove_background:
        return image
    # The shape model segments the subject by its alpha channel, so an image without one
    # has to go through rembg first — which is why the server runs it regardless there.
    remover = _import_first(
        (("hy3dshape.rembg", "BackgroundRemover"), ("hy3dgen.rembg", "BackgroundRemover"))
    )
    if remover is None:
        td.log("  note: no background remover available — using the image as it is")
        return image
    return remover()(image)


def _postprocess(mesh: Any, options: Any) -> Any:
    """Floater / degenerate-face cleanup, then decimation to ``face_count``."""
    for module_name, attribute in (
        ("hy3dshape.postprocessors", "FloaterRemover"),
        ("hy3dgen.shapegen", "FloaterRemover"),
    ):
        cleaner = _import_first(((module_name, attribute),))
        if cleaner is not None:
            mesh = cleaner()(mesh)
            break

    for module_name, attribute in (
        ("hy3dshape.postprocessors", "DegenerateFaceRemover"),
        ("hy3dgen.shapegen", "DegenerateFaceRemover"),
    ):
        cleaner = _import_first(((module_name, attribute),))
        if cleaner is not None:
            mesh = cleaner()(mesh)
            break

    if options.face_count > 0:
        reducer = _import_first(
            (
                ("hy3dshape.postprocessors", "FaceReducer"),
                ("hy3dgen.shapegen", "FaceReducer"),
            )
        )
        if reducer is not None:
            mesh = _call_filtered(reducer(), mesh, max_facenum=options.face_count)
    return mesh


def _paint(mesh: Any, reference: Any, options: Options, device: str) -> Any:
    paint_class = _import_first(_PAINT_PIPELINES)
    if paint_class is None:
        td.log("  warning: no texture pipeline installed — returning the untextured mesh")
        return mesh
    td.log(f"texturing with {options.texgen_path or options.model_path}")
    pipeline = _call_filtered(
        paint_class.from_pretrained,
        options.texgen_path or options.model_path,
        device=device,
    )
    return _call_filtered(
        pipeline,
        mesh,
        image=reference,
        max_num_view=options.max_num_view or None,
        resolution=options.texture_resolution,
    )
