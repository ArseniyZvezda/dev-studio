"""The hunyuan3d plugin's generation backend and its stdlib mesh IO.

The model itself is never touched here — the weights are several GB and the `local`
transport is by design a lazy import of packages CI does not have. What *is* pinned is
everything around it: the wire format the `server` transport speaks against the real
`server/schemas.py` contract, both of the flows it can speak it over, how an answer is
decoded, what each failure says, the mesh reading and writing the nodes run on, and the
settings-to-port-default wiring that decides what lands in the §8.2 cache key.

The node-side modules are imported the way a node script imports them — `nodes/` on
`sys.path`, no `voxarium_studio` package in sight — because that is the only environment they are
ever run in.
"""

from __future__ import annotations

import base64
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from voxarium_studio.core.config import AppConfig, PluginConfig
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.plugins import loader as plugin_loader

BUILTIN = plugin_loader.builtin_plugins_dir()
assert BUILTIN is not None, "the built-in plugin directory must be discoverable"
NODES = BUILTIN / "hunyuan3d" / "nodes"

NODE_MODULES = ("hunyuan3d", "imaging", "meshio", "_voxarium_studio")


@pytest.fixture
def registry(project: Path):
    """These node-contract tests explicitly opt into the optional 3D plugin."""
    return load_registry(project, {})


@pytest.fixture
def hunyuan(monkeypatch: pytest.MonkeyPatch):
    """Import `hunyuan3d` as a node script does, and leave `sys.modules` as it was."""
    monkeypatch.syspath_prepend(str(NODES))
    for name in NODE_MODULES:
        sys.modules.pop(name, None)
    import hunyuan3d

    yield hunyuan3d
    for name in NODE_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def meshes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(NODES))
    for name in NODE_MODULES:
        sys.modules.pop(name, None)
    import meshio

    yield meshio
    for name in NODE_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def images(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(NODES))
    for name in NODE_MODULES:
        sys.modules.pop(name, None)
    import imaging

    yield imaging
    for name in NODE_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def voxels(monkeypatch: pytest.MonkeyPatch):
    """Import `voxelize` as a node script does — it reaches for `meshio` and `_voxarium_studio`."""
    monkeypatch.syspath_prepend(str(NODES))
    for name in (*NODE_MODULES, "voxelize"):
        sys.modules.pop(name, None)
    import voxelize

    yield voxelize
    for name in (*NODE_MODULES, "voxelize"):
        sys.modules.pop(name, None)


@pytest.fixture
def fake_server():
    """A stand-in for the Hunyuan3D API server, recording what the node sent it.

    It answers the queued `/tasks` flow by default and the synchronous `/generate` too,
    because `auto` reaches for one and falls back to the other — the tests need both to
    exist and need to be able to take either away.
    """
    state: dict = {
        "requests": [],
        "status": 200,
        "content_type": "model/gltf-binary",
        "body": binary_glb([12]),
        "health": {
            "status": "healthy",
            "device": "cuda",
            "gpu_name": "Fake 4070",
            "vram_total_gb": 12.0,
            "vram_used_gb": 4.0,
            "torch_reserved_gb": 2.0,
            "vram_mode": "swap",
            "texture_available": True,
            "queue_length": 0,
            # The multi-view half of `/health`, as the server publishes it: whether the
            # second shape checkpoint is on disk at all, which model answers a single
            # picture, and which of the two is currently resident (never both).
            "mv_available": True,
            "mv_unavailable_reason": None,
            "default_shape_model": "single",
            "shape_model_on_gpu": "single",
        },
        "tasks": True,
        "task_status": ["completed"],
        "task_result": {"faces": 12, "vertices": 8, "shape_seconds": 3.0},
        # `views` and `shape_model` join it on a multi-view run; the tests that care set
        # them, because a single-image server answers without either.
        "task_error": None,
        # Scripted like `task_status`: each poll takes the next code and holds on the last,
        # so a test can make the server go quiet for exactly N polls and then come back.
        # Anything but 200 is "no answer" to the node, which is what its timeout bounds.
        "task_http": [200],
        "task_progress": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # keep pytest output readable
            pass

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(status, "application/json", json.dumps(payload).encode("utf-8"))

        def do_GET(self) -> None:
            state["requests"].append({"path": self.path, "method": "GET"})
            if self.path == "/health":
                if state["health"] is None:
                    self._json({"detail": "not found"}, 404)
                else:
                    self._json(state["health"])
            elif self.path.startswith("/tasks/"):
                codes = state["task_http"]
                code = codes[0] if len(codes) == 1 else codes.pop(0)
                if code != 200:
                    self._json({"detail": "the server is not answering"}, code)
                    return
                # Each poll takes the next scripted status, holding on the last one.
                statuses = state["task_status"]
                status = statuses[0] if len(statuses) == 1 else statuses.pop(0)
                body: dict = {"uid": "abc123", "status": status}
                if status == "running" and state["task_progress"]:
                    body["progress"] = state["task_progress"]
                if status == "completed":
                    body["result"] = dict(state["task_result"], uid="abc123")
                if status == "failed":
                    body["error"] = state["task_error"]
                self._json(body)
            elif self.path.startswith("/files/"):
                self._send(state["status"], state["content_type"], state["body"])
            else:
                self._json({"detail": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            state["requests"].append(
                {
                    "path": self.path,
                    "method": "POST",
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if self.path == "/tasks":
                if not state["tasks"]:
                    self._json({"detail": "not found"}, 404)
                else:
                    self._json({"uid": "abc123", "status": "queued", "queued_ahead": 0})
            elif self.path == "/generate":
                self._send(state["status"], state["content_type"], state["body"])
            else:
                self._json({"detail": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # `shutdown()` waits for `serve_forever` to come round its poll loop, and the default
    # interval is half a second — paid on teardown by every test in this file.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def posts(state: dict, path: str) -> list[dict]:
    return [r for r in state["requests"] if r["method"] == "POST" and r["path"] == path]


# ----------------------------------------------------------------------------- fixtures


def binary_stl(triangles: int) -> bytes:
    return b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * (triangles * 50)


def binary_glb(triangles_per_primitive: list[int], *, geometry: bool = False) -> bytes:
    """A GLB whose JSON alone supports counting, and whose BIN supports reading.

    ``geometry=False`` keeps the accessors pointing at a buffer that is not there, which
    is what the counting path must cope with; ``geometry=True`` writes real vertices.
    """
    document: dict = {"accessors": [], "meshes": [{"primitives": []}]}
    binary = b""
    for count in triangles_per_primitive:
        vertices = count * 3
        if geometry:
            offset = len(binary)
            for i in range(vertices):
                binary += struct.pack("<3f", float(i), float(i % 3), 0.0)
            document.setdefault("bufferViews", []).append(
                {"buffer": 0, "byteOffset": offset, "byteLength": vertices * 12}
            )
            document["accessors"].append(
                {
                    "bufferView": len(document["bufferViews"]) - 1,
                    "componentType": 5126,
                    "type": "VEC3",
                    "count": vertices,
                }
            )
        else:
            document["accessors"].append({"count": vertices})
        index = len(document["accessors"]) - 1
        document["meshes"][0]["primitives"].append(
            {"mode": 4, "attributes": {"POSITION": index}}
        )
    if geometry:
        document["buffers"] = [{"byteLength": len(binary)}]
        document["nodes"] = [{"mesh": 0}]
        document["scenes"] = [{"nodes": [0]}]
        document["scene"] = 0

    chunk = json.dumps(document).encode("utf-8")
    chunk += b" " * ((-len(chunk)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)

    total = 12 + 8 + len(chunk) + (8 + len(binary) if binary else 0)
    out = b"glTF" + struct.pack("<II", 2, total)
    out += struct.pack("<II", len(chunk), 0x4E4F534A) + chunk
    if binary:
        out += struct.pack("<II", len(binary), 0x004E4942) + binary
    return out


def textured_obj_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("textured.obj", "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        archive.writestr("textured.mtl", "newmtl material0\n")
        archive.writestr("albedo.png", b"\x89PNG")
    return buffer.getvalue()


def options(hunyuan, server_url: str, image: Path | None = None, **overrides):
    base = dict(
        image=image,
        mode="server",
        server_url=server_url,
        timeout=30.0,
        poll_interval=0.01,
        steps=5,
        octree_resolution=128,
        output_format="glb",
        # Loopback would otherwise send `image_path`, which most of these tests do not
        # want to reason about; the two that do turn it back on explicitly.
        image_path_mode="never",
    )
    base.update(overrides)
    return hunyuan.Options(**base)


@pytest.fixture
def reference(tmp_path: Path) -> Path:
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n-not-really-a-png")
    return image


# --------------------------------------------------------------------------- the wire


def test_the_request_body_is_the_servers_own_generation_request(
    hunyuan, fake_server, reference, tmp_path
):
    """Every field is one `schemas.GenerationRequest` declares, under its own name."""
    result = hunyuan.generate(
        options(
            hunyuan,
            fake_server["url"],
            reference,
            texture=True,
            texture_resolution=768,
            num_chunks=6000,
            face_count=20000,
            max_num_view=4,
        ),
        tmp_path / "mesh.glb",
    )

    body = posts(fake_server, "/tasks")[0]["body"]
    assert base64.b64decode(body["image"]) == reference.read_bytes()
    assert body["num_inference_steps"] == 5
    assert body["octree_resolution"] == 128
    assert body["num_chunks"] == 6000
    assert body["face_count"] == 20000
    assert body["texture_resolution"] == 768
    assert body["max_num_view"] == 4
    assert body["texture"] is True
    assert body["remove_background"] is True
    assert body["output_format"] == "glb"
    assert body["response"] == "file"
    assert "text" not in body, "the server has no text field and would 422 on one"
    assert "type" not in body, "`type` was the old name and is silently ignored"

    assert result.path.read_bytes() == fake_server["body"]
    assert "hunyuan3d/server" in result.backend


def test_bake_views_are_omitted_rather_than_guessed_when_unset(
    hunyuan, fake_server, reference, tmp_path
):
    """Absent means "the server picks by card size", which beats any number we'd invent."""
    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, max_num_view=0), tmp_path / "m.glb"
    )
    assert "max_num_view" not in posts(fake_server, "/tasks")[0]["body"]


def test_an_image_is_required_before_any_request(hunyuan, fake_server, tmp_path, capsys):
    """Hunyuan3D is image-to-3D: there is no text field anywhere to fall back on."""
    with pytest.raises(SystemExit):
        hunyuan.generate(options(hunyuan, fake_server["url"]), tmp_path / "mesh.glb")

    assert "image-to-3D" in capsys.readouterr().err
    assert fake_server["requests"] == []


def test_a_local_server_is_given_the_path_instead_of_two_megabytes_of_base64(
    hunyuan, fake_server, reference, tmp_path
):
    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, image_path_mode="auto"),
        tmp_path / "mesh.glb",
    )

    body = posts(fake_server, "/tasks")[0]["body"]
    assert body["image_path"] == str(reference.resolve())
    assert "image" not in body, "sending both is a 422 from the schema's own validator"


def test_a_remote_server_gets_base64_because_it_cannot_read_our_disk(
    hunyuan, fake_server, reference, tmp_path, monkeypatch
):
    monkeypatch.setattr(hunyuan, "_is_loopback", lambda url: False)
    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, image_path_mode="auto"),
        tmp_path / "mesh.glb",
    )

    body = posts(fake_server, "/tasks")[0]["body"]
    assert "image" in body and "image_path" not in body


# ------------------------------------------------------------------------- multi-view


def views(tmp_path: Path, *names: str) -> dict[str, Path]:
    """One small distinct picture per view, so a mix-up in the map is visible."""
    made = {}
    for index, name in enumerate(names):
        path = tmp_path / f"{name}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n-view-" + bytes([index]) * (index + 1))
        made[name] = path
    return made


def test_the_multi_view_request_is_a_map_of_views_and_names_the_model(
    hunyuan, fake_server, tmp_path
):
    """`images` plus `shape_model`, which is exactly what turns on hunyuan3d-dit-v2-mv."""
    pictures = views(tmp_path, "front", "left", "back")
    hunyuan.generate(
        options(hunyuan, fake_server["url"], views=pictures, shape_model="mv"),
        tmp_path / "mesh.glb",
    )

    body = posts(fake_server, "/tasks")[0]["body"]
    assert body["shape_model"] == "mv"
    assert sorted(body["images"]) == ["back", "front", "left"]
    for view, path in pictures.items():
        assert base64.b64decode(body["images"][view]) == path.read_bytes()
    assert "image" not in body, "one image field or the other; both is a 422"
    assert "image_paths" not in body


def test_the_views_are_sent_in_the_models_own_order(hunyuan, fake_server, tmp_path):
    """Front, then clockwise from above — whatever order the ports were filled in."""
    hunyuan.generate(
        options(
            hunyuan,
            fake_server["url"],
            views=views(tmp_path, "right", "back", "front"),
            shape_model="mv",
        ),
        tmp_path / "mesh.glb",
    )

    assert list(posts(fake_server, "/tasks")[0]["body"]["images"]) == ["front", "back", "right"]


def test_a_local_server_reads_the_views_off_disk_too(hunyuan, fake_server, tmp_path):
    pictures = views(tmp_path, "front", "back")
    hunyuan.generate(
        options(
            hunyuan,
            fake_server["url"],
            views=pictures,
            shape_model="mv",
            image_path_mode="auto",
        ),
        tmp_path / "mesh.glb",
    )

    body = posts(fake_server, "/tasks")[0]["body"]
    assert body["image_paths"] == {view: str(p.resolve()) for view, p in pictures.items()}
    assert "images" not in body


def test_a_single_image_run_says_nothing_about_the_shape_model(
    hunyuan, fake_server, reference, tmp_path
):
    """`auto` is the field's own default, and a server predating it has no such field."""
    hunyuan.generate(options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb")

    assert "shape_model" not in posts(fake_server, "/tasks")[0]["body"]


def test_an_image_and_a_view_map_together_are_refused_before_the_server_sees_them(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(
                hunyuan, fake_server["url"], reference, views=views(tmp_path, "front")
            ),
            tmp_path / "mesh.glb",
        )

    assert "one image field" in capsys.readouterr().err
    assert fake_server["requests"] == []


def test_the_single_view_model_is_never_asked_to_read_a_view_map(
    hunyuan, fake_server, tmp_path, capsys
):
    """The server answers this 422; saying so here explains which node to use instead."""
    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(
                hunyuan,
                fake_server["url"],
                views=views(tmp_path, "front", "left"),
                shape_model="single",
            ),
            tmp_path / "mesh.glb",
        )

    assert "look at exactly one of the pictures" in capsys.readouterr().err
    assert fake_server["requests"] == []


def test_a_view_the_model_has_no_embedding_for_is_refused(hunyuan, fake_server, tmp_path, capsys):
    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(
                hunyuan,
                fake_server["url"],
                views={"front": tmp_path / "f.png", "above": tmp_path / "a.png"},
                shape_model="mv",
            ),
            tmp_path / "mesh.glb",
        )

    assert "Unknown view(s) ['above']" in capsys.readouterr().err


def test_a_multi_view_run_is_refused_with_the_reason_the_server_published(
    hunyuan, fake_server, tmp_path, capsys
):
    """It would answer 409; the preflight attaches where it looked for the checkpoint."""
    fake_server["health"] = dict(
        fake_server["health"],
        mv_available=False,
        mv_unavailable_reason="no checkpoint under models/hy3dgen/tencent/Hunyuan3D-2mv",
    )

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(
                hunyuan, fake_server["url"], views=views(tmp_path, "front"), shape_model="mv"
            ),
            tmp_path / "mesh.glb",
        )

    error = capsys.readouterr().err
    assert "no checkpoint under models/hy3dgen/tencent/Hunyuan3D-2mv" in error
    assert posts(fake_server, "/tasks") == [], "nothing is queued that cannot be served"


def test_a_single_image_run_is_not_stopped_by_a_missing_multi_view_checkpoint(
    hunyuan, fake_server, reference, tmp_path
):
    fake_server["health"] = dict(fake_server["health"], mv_available=False)

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    assert result.path.exists()


def test_the_server_says_which_model_ran_and_on_which_views(
    hunyuan, fake_server, tmp_path, capsys
):
    """`auto` on one picture resolves against a server flag this side cannot predict."""
    fake_server["task_result"] = dict(
        fake_server["task_result"], shape_model="mv", views=["front", "back"]
    )

    hunyuan.generate(
        options(
            hunyuan, fake_server["url"], views=views(tmp_path, "front", "back"), shape_model="mv"
        ),
        tmp_path / "mesh.glb",
    )

    assert "mv on front, back" in capsys.readouterr().out


def test_a_multi_view_stl_is_converted_and_named_after_the_front(hunyuan, fake_server, tmp_path):
    """The STL header carries the subject, which for a view map is the front picture."""
    fake_server["body"] = binary_glb([3], geometry=True)
    output = tmp_path / "mesh.stl"

    result = hunyuan.generate(
        options(
            hunyuan,
            fake_server["url"],
            views=views(tmp_path, "front", "left"),
            shape_model="mv",
            output_format="stl",
        ),
        output,
    )

    assert result.path == output
    assert b"front" in output.read_bytes()[:80]
    assert result.triangle_count == 3


def test_a_texture_baked_from_a_side_view_is_called_out(hunyuan, fake_server, tmp_path, capsys):
    """Upstream's paint pipeline takes one image, so a run without a front says so."""
    hunyuan.generate(
        options(
            hunyuan,
            fake_server["url"],
            views=views(tmp_path, "left", "back"),
            shape_model="mv",
            texture=True,
        ),
        tmp_path / "mesh.glb",
    )

    assert "no front view, so the texture will be baked from 'left'" in capsys.readouterr().out


# ---------------------------------------------------------------------- the two flows


def test_the_queued_flow_polls_to_completion_and_downloads_the_result(
    hunyuan, fake_server, reference, tmp_path
):
    fake_server["task_status"] = ["queued", "running", "completed"]

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    paths = [r["path"] for r in fake_server["requests"]]
    assert paths.count("/tasks/abc123") == 3
    assert paths[-1] == "/files/abc123"
    assert result.path.read_bytes() == fake_server["body"]
    # The server counted the mesh it made; parsing the file back would be second-hand.
    assert result.triangle_count == 12
    assert result.stats["shape_seconds"] == 3.0


def test_a_server_without_tasks_falls_back_to_the_synchronous_endpoint(
    hunyuan, fake_server, reference, tmp_path
):
    """An upstream `api_server.py` has no queue; `auto` finds that out by asking."""
    fake_server["tasks"] = False

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    assert len(posts(fake_server, "/tasks")) == 1, "it probed once"
    assert len(posts(fake_server, "/generate")) == 1
    assert result.path.read_bytes() == fake_server["body"]


def test_asking_for_the_queue_explicitly_does_not_fall_back(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    fake_server["tasks"] = False

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference, transport="tasks"),
            tmp_path / "mesh.glb",
        )

    assert posts(fake_server, "/generate") == []
    assert "404" in capsys.readouterr().err


def test_a_failed_task_reports_the_servers_reason(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    fake_server["task_status"] = ["running", "failed"]
    fake_server["task_error"] = "OutOfMemoryError: CUDA out of memory"

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
        )

    assert "CUDA out of memory" in capsys.readouterr().err


# ------------------------------------------------------------- the timeout is about silence

# `hunyuan_timeout` bounds how long the server may say *nothing*, not how long a generation
# may take. The distinction is the whole reason the node polls: a textured run on a busy
# 12 GB card is minutes of legitimate work, and any wall clock loose enough to allow it is
# far too loose to catch a server that has died.


def test_a_server_that_keeps_answering_is_waited_out_far_past_the_timeout(
    hunyuan, fake_server, reference, tmp_path
):
    fake_server["task_status"] = ["running"] * 12 + ["completed"]

    started = time.monotonic()
    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, timeout=0.05, poll_interval=0.05),
        tmp_path / "mesh.glb",
    )
    elapsed = time.monotonic() - started

    assert elapsed > 0.5, "it ran for ten times its timeout without giving up"
    assert [r["path"] for r in fake_server["requests"]].count("/tasks/abc123") == 13
    assert result.path.read_bytes() == fake_server["body"]


def test_a_poll_that_goes_unanswered_is_retried_rather_than_ending_the_node(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    """One dropped poll is not a dead server, and re-running the model would cost minutes."""
    fake_server["task_http"] = [503, 503, 503, 200]
    fake_server["task_status"] = ["completed"]

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, timeout=5.0, poll_interval=0.01),
        tmp_path / "mesh.glb",
    )

    assert result.path.read_bytes() == fake_server["body"]
    out = capsys.readouterr().out
    assert "no answer" in out and "answering again" in out


def test_the_node_gives_up_once_the_server_has_been_silent_for_the_timeout(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    """The GPU keeps working; naming the uid is what stops it being paid for twice."""
    fake_server["task_http"] = [503]

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference, timeout=0.2, poll_interval=0.01),
            tmp_path / "mesh.glb",
        )

    stderr = capsys.readouterr().err
    assert "stopped answering" in stderr
    assert "/files/abc123" in stderr, "so the result can be collected instead of re-run"


def test_a_status_poll_does_not_wait_out_the_whole_silence_budget(hunyuan):
    """It is a probe, not the work: a poll that hung for the budget would be the budget."""
    assert hunyuan._poll_timeout(hunyuan.Options(timeout=1800.0)) == 30.0
    # ...and never longer than the budget itself, or a short one could not fire at all.
    assert hunyuan._poll_timeout(hunyuan.Options(timeout=5.0)) == 5.0


# ----------------------------------------------------------------------------- progress


def progress_frames(captured: str) -> list[dict]:
    """The `::voxarium_studio:progress::` lines the node wrote, as the engine would parse them."""
    prefix = "::voxarium_studio:progress::"
    return [
        json.loads(line.removeprefix(prefix))
        for line in captured.splitlines()
        if line.startswith(prefix)
    ]


def test_the_servers_own_progress_reaches_the_node_as_frames(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    """`GET /tasks/{uid}`'s `progress` object is what the canvas draws on the node."""
    fake_server["task_status"] = ["queued", "running", "completed"]
    fake_server["task_progress"] = {
        "stage": "multiview_diffusion",
        "label": "Multiview diffusion",
        "stage_index": 8,
        "stage_count": 13,
        "stage_percent": 60.0,
        "percent": 48.3,
        "detail": "9/15",
        "elapsed_s": 52.1,
        "eta_s": 55.7,
    }

    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    captured = capsys.readouterr()
    frames = progress_frames(captured.out)
    assert {"label": "Queued"} in frames
    assert {
        "percent": 48.3,
        "label": "Multiview diffusion",
        "detail": "9/15",
        "eta_s": 55.7,
        "step": 8,
        "steps": 13,
    } in frames
    # The stage is in the log too, once, where the whole run can be read back afterwards.
    assert captured.out.count("Multiview diffusion (8/13)") == 1


def test_a_server_too_old_to_report_progress_still_says_it_is_working(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    """No `progress` field means an indeterminate bar, not a node that looks hung."""
    fake_server["task_status"] = ["running", "completed"]
    fake_server["task_progress"] = None

    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    frames = progress_frames(capsys.readouterr().out)
    assert {"label": "Generating"} in frames
    assert all(frame.get("percent") is None for frame in frames if frame["label"] == "Generating")


def test_the_synchronous_endpoint_reports_a_stage_and_no_percentage(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    """`/generate` sends nothing until it is finished, so a percentage would be invented."""
    fake_server["tasks"] = False

    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    assert {"label": "Generating"} in progress_frames(capsys.readouterr().out)


# ------------------------------------------------------------------------ what is written


def test_stl_is_converted_from_glb_because_the_server_has_no_stl_exporter(
    hunyuan, fake_server, reference, tmp_path
):
    fake_server["body"] = binary_glb([4], geometry=True)

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, output_format="stl"),
        tmp_path / "mesh.stl",
    )

    assert posts(fake_server, "/tasks")[0]["body"]["output_format"] == "glb"
    assert result.path == tmp_path / "mesh.stl"
    assert result.path.read_bytes()[:5] != b"glTF "
    assert struct.unpack("<I", result.path.read_bytes()[80:84])[0] == 4
    assert result.triangle_count == 4
    assert not (tmp_path / "mesh.glb").exists(), "the intermediate is not left behind"


def test_a_textured_obj_is_saved_as_the_zip_it_actually_is(
    hunyuan, fake_server, reference, tmp_path
):
    """Calling the bundle `.obj` would break every consumer that trusts the suffix."""
    fake_server["body"] = textured_obj_zip()
    fake_server["task_result"] = {"format": "obj", "textured": True}

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, output_format="obj", texture=True),
        tmp_path / "mesh.obj",
    )

    assert result.path == tmp_path / "mesh.zip"
    assert zipfile.is_zipfile(result.path)
    assert result.triangle_count == 1


def test_an_untextured_obj_is_written_as_a_plain_obj(hunyuan, fake_server, reference, tmp_path):
    fake_server["body"] = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    fake_server["content_type"] = "text/plain"
    fake_server["task_result"] = {"format": "obj-plain", "faces": 1}

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, output_format="obj"),
        tmp_path / "mesh.obj",
    )

    assert result.path == tmp_path / "mesh.obj"
    assert result.triangle_count == 1


# ------------------------------------------------------------------------- health and errors


def test_a_texture_request_is_refused_with_the_reason_the_server_published(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    fake_server["health"] = {
        "status": "healthy",
        "device": "cuda",
        "vram_mode": "shape",
        "texture_available": False,
        "texture_unavailable_reason": "started with --vram-mode shape",
        "queue_length": 0,
    }

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference, texture=True),
            tmp_path / "mesh.glb",
        )

    assert "--vram-mode shape" in capsys.readouterr().err
    assert posts(fake_server, "/tasks") == [], "refused before anything was queued"


def test_a_missing_health_endpoint_is_not_fatal(hunyuan, fake_server, reference, tmp_path):
    """An older deployment may not serve it, and the generate call errors far better."""
    fake_server["health"] = None

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )
    assert result.path.exists()


def test_an_http_error_explains_the_status_the_server_documents(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    fake_server["tasks"] = False
    fake_server["status"] = 507
    fake_server["content_type"] = "application/json"
    fake_server["body"] = json.dumps({"detail": "CUDA out of memory"}).encode("utf-8")

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
        )

    stderr = capsys.readouterr().err
    assert "507" in stderr
    assert "CUDA out of memory" in stderr
    assert "octree resolution" in stderr, "507 is the OOM status and should say what to lower"


def test_an_unreachable_server_says_how_to_start_one(hunyuan, reference, tmp_path, capsys):
    # Port 1 on loopback: nothing binds it, and connecting fails immediately. The window
    # is short because what is asserted is the message, not how long the node waits — the
    # timeout itself is covered by the three tests under "the timeout is about silence".
    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, "http://127.0.0.1:1", reference, timeout=0.5), tmp_path / "m.glb"
        )

    assert "run_server.bat" in capsys.readouterr().err


def test_the_bearer_token_comes_from_an_env_var_not_the_config(
    hunyuan, fake_server, reference, tmp_path, monkeypatch
):
    """§12: a secret is named in `config.yaml`, never stored in it."""
    monkeypatch.setenv("HUNYUAN_TEST_KEY", "s3cret")

    hunyuan.generate(
        options(hunyuan, fake_server["url"], reference, api_key_env="HUNYUAN_TEST_KEY"),
        tmp_path / "mesh.glb",
    )

    assert posts(fake_server, "/tasks")[0]["authorization"] == "Bearer s3cret"


def test_a_base64_json_answer_is_decoded(hunyuan, fake_server, reference, tmp_path):
    """`response=json` and hosted wrappers both base64 the model instead of streaming it."""
    fake_server["tasks"] = False
    fake_server["content_type"] = "application/json"
    fake_server["body"] = json.dumps(
        {"model_base64": base64.b64encode(binary_glb([3])).decode("ascii")}
    ).encode("utf-8")

    result = hunyuan.generate(
        options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
    )

    assert result.path.read_bytes() == binary_glb([3])
    assert result.triangle_count == 3


def test_json_without_a_model_payload_is_refused(
    hunyuan, fake_server, reference, tmp_path, capsys
):
    """A stray `{"status": "processing"}` must not be written to disk as a mesh."""
    fake_server["tasks"] = False
    fake_server["content_type"] = "application/json"
    fake_server["body"] = json.dumps({"status": "processing"}).encode("utf-8")

    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference), tmp_path / "mesh.glb"
        )

    assert "no model payload" in capsys.readouterr().err
    assert not (tmp_path / "mesh.glb").exists()


def test_an_unknown_mode_or_format_fails_before_any_request(
    hunyuan, fake_server, reference, tmp_path
):
    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference, output_format="ply"),
            tmp_path / "mesh.ply",
        )
    with pytest.raises(SystemExit):
        hunyuan.generate(
            options(hunyuan, fake_server["url"], reference, mode="magic"), tmp_path / "mesh.glb"
        )
    assert fake_server["requests"] == []


# --------------------------------------------------------------------------------- meshio


def test_triangles_are_counted_in_every_format_a_node_can_hold(meshes, tmp_path):
    stl = tmp_path / "a.stl"
    stl.write_bytes(binary_stl(41))
    assert meshes.count_triangles(stl) == 41

    obj = tmp_path / "a.obj"
    obj.write_text("v 0 0 0\nf 1 2 3\nf 1 2 3 4\n", encoding="utf-8")
    assert meshes.count_triangles(obj) == 3, "a quad is two triangles"

    glb = tmp_path / "a.glb"
    glb.write_bytes(binary_glb([10, 7]))
    assert meshes.count_triangles(glb) == 17

    bundle = tmp_path / "a.zip"
    bundle.write_bytes(textured_obj_zip())
    assert meshes.count_triangles(bundle) == 1


def test_an_unreadable_mesh_reports_no_count_rather_than_failing(meshes, tmp_path):
    """A count is informational: losing it must not lose the mesh that was generated."""
    truncated = tmp_path / "a.glb"
    truncated.write_bytes(b"glTF" + b"\0" * 4)
    assert meshes.count_triangles(truncated) is None

    unknown = tmp_path / "a.ply"
    unknown.write_bytes(b"ply")
    assert meshes.count_triangles(unknown) is None


def test_geometry_reads_back_out_of_all_three_formats(meshes, tmp_path):
    triangles = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]

    stl = tmp_path / "a.stl"
    meshes.write_binary_stl(stl, triangles, "test")
    assert meshes.read_triangles(stl) == triangles

    obj = tmp_path / "a.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1/1 2/2/2 3//3\n", encoding="utf-8")
    assert meshes.read_triangles(obj) == triangles

    glb = tmp_path / "a.glb"
    glb.write_bytes(binary_glb([2], geometry=True))
    assert len(meshes.read_triangles(glb)) == 2


def test_a_glb_node_transform_is_applied_rather_than_ignored(meshes, tmp_path):
    """Geometry read in local space voxelises at the wrong scale and never looks wrong."""
    document = {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [10.0, 0.0, 0.0], "scale": [2.0, 2.0, 2.0]}],
        "meshes": [{"primitives": [{"mode": 4, "attributes": {"POSITION": 0}}]}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 3}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}],
        "buffers": [{"byteLength": 36}],
    }
    binary = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    chunk = json.dumps(document).encode("utf-8")
    chunk += b" " * ((-len(chunk)) % 4)
    glb = tmp_path / "a.glb"
    glb.write_bytes(
        b"glTF"
        + struct.pack("<II", 2, 12 + 8 + len(chunk) + 8 + len(binary))
        + struct.pack("<II", len(chunk), 0x4E4F534A)
        + chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )

    assert meshes.read_triangles(glb) == [
        ((10.0, 0.0, 0.0), (12.0, 0.0, 0.0), (10.0, 2.0, 0.0))
    ]


def test_reading_a_format_with_no_reader_says_so(meshes, tmp_path):
    unknown = tmp_path / "a.ply"
    unknown.write_bytes(b"ply")
    with pytest.raises(meshes.MeshError):
        meshes.read_triangles(unknown)


# ------------------------------------------------------------------ the node end to end


def run_node(script: str, payload: dict, tmp_path: Path) -> dict:
    """Drive a node script exactly as `_run_python_script` does (§8.1)."""
    output_file = tmp_path / "node.output.json"
    environment = dict(os.environ, VOXARIUM_STUDIO_OUTPUT_FILE=str(output_file), PYTHONIOENCODING="utf-8")
    process = subprocess.run(
        [sys.executable, str(NODES / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=120,
    )
    assert process.returncode == 0, f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
    return json.loads(output_file.read_text(encoding="utf-8"))


def node_payload(tmp_path: Path, params: dict, settings: dict) -> dict:
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    return {
        "inputs": {},
        "params": params,
        "resolved": params,
        "settings": settings,
        "context": {
            "project_root": str(tmp_path),
            "assets_dir": str(assets),
            "work_dir": str(tmp_path),
            "node_id": "n1",
            "run_id": "r1",
        },
    }


def test_the_node_generates_from_the_image_and_reports_what_it_wrote(
    fake_server, reference, tmp_path
):
    outputs = run_node(
        "image_to_mesh.py",
        node_payload(
            tmp_path,
            {
                "image": str(reference),
                "output_format": "glb",
                "seed": 7,
                "steps": 5,
                "guidance_scale": 5.0,
                "octree_resolution": 128,
                "face_count": 20000,
                "texture": False,
            },
            {
                "hunyuan_mode": "server",
                "hunyuan_server_url": fake_server["url"],
                "hunyuan_timeout": 30,
                "hunyuan_poll_interval": 0.01,
                "hunyuan_image_path": "never",
            },
        ),
        tmp_path,
    )

    mesh = Path(outputs["mesh_file"])
    # The slug comes from the image, which is the only thing naming the result now.
    assert mesh.name == "reference-7.glb"
    assert mesh.read_bytes() == fake_server["body"]
    assert outputs["triangle_count"] == 12
    body = posts(fake_server, "/tasks")[0]["body"]
    assert body["seed"] == 7
    assert body["face_count"] == 20000


def test_the_node_still_reads_the_face_count_from_its_old_settings_key(
    fake_server, reference, tmp_path
):
    """It became a port; a project that set it as a setting must not silently lose it."""
    outputs = run_node(
        "image_to_mesh.py",
        node_payload(
            tmp_path,
            {"image": str(reference), "output_format": "glb"},
            {
                "hunyuan_server_url": fake_server["url"],
                "hunyuan_poll_interval": 0.01,
                "hunyuan_image_path": "never",
                "hunyuan_face_count": 12345,
            },
        ),
        tmp_path,
    )

    assert outputs["mesh_file"].endswith(".glb")
    assert posts(fake_server, "/tasks")[0]["body"]["face_count"] == 12345


def test_the_multi_view_node_sends_every_view_it_was_given(fake_server, tmp_path):
    pictures = views(tmp_path, "front", "left", "back")
    outputs = run_node(
        "multiview_to_mesh.py",
        node_payload(
            tmp_path,
            {
                "front": str(pictures["front"]),
                "left": str(pictures["left"]),
                "back": str(pictures["back"]),
                "output_format": "glb",
                "seed": 3,
                "steps": 5,
                "octree_resolution": 128,
            },
            {
                "hunyuan_server_url": fake_server["url"],
                "hunyuan_poll_interval": 0.01,
                "hunyuan_image_path": "never",
            },
        ),
        tmp_path,
    )

    mesh = Path(outputs["mesh_file"])
    # `-mv-` so the same front picture through the other node cannot land on this file.
    assert mesh.name == "front-mv-3.glb"
    assert outputs["views_used"] == 3
    assert outputs["triangle_count"] == 12
    body = posts(fake_server, "/tasks")[0]["body"]
    assert sorted(body["images"]) == ["back", "front", "left"]
    assert body["shape_model"] == "mv"
    assert body["seed"] == 3


def test_the_multi_view_node_runs_on_the_front_view_alone(fake_server, tmp_path):
    """One view is a legitimate multi-view request — the server takes at least one."""
    outputs = run_node(
        "multiview_to_mesh.py",
        node_payload(
            tmp_path,
            {"front": str(views(tmp_path, "front")["front"]), "output_format": "glb"},
            {
                "hunyuan_server_url": fake_server["url"],
                "hunyuan_poll_interval": 0.01,
                "hunyuan_image_path": "never",
            },
        ),
        tmp_path,
    )

    assert outputs["views_used"] == 1
    assert list(posts(fake_server, "/tasks")[0]["body"]["images"]) == ["front"]


def test_the_multi_view_node_needs_a_front_view_and_says_which_node_does_not(
    fake_server, tmp_path
):
    pictures = views(tmp_path, "left", "back")
    with pytest.raises(AssertionError) as failure:
        run_node(
            "multiview_to_mesh.py",
            node_payload(
                tmp_path,
                {"left": str(pictures["left"]), "back": str(pictures["back"])},
                {"hunyuan_server_url": fake_server["url"], "hunyuan_poll_interval": 0.01},
            ),
            tmp_path,
        )

    assert "needs at least a front view" in str(failure.value)
    assert fake_server["requests"] == [], "nothing is sent that the node already refused"


def test_the_voxelizer_reads_what_hunyuan3d_actually_exports(tmp_path):
    """The server has no STL exporter, so a voxelizer that only spoke STL was unreachable."""
    mesh = tmp_path / "shape.glb"
    mesh.write_bytes(binary_glb([2], geometry=True))

    outputs = run_node(
        "voxelize.py",
        node_payload(tmp_path, {"mesh_file": str(mesh), "resolution": 16}, {}),
        tmp_path,
    )

    assert Path(outputs["vox_file"]).suffix == ".vox"
    assert outputs["voxel_count"] > 0


# ------------------------------------------------------------------ the up axis

AXIS_VECTORS = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "z": (0.0, 0.0, 1.0),
    "-x": (-1.0, 0.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "-z": (0.0, 0.0, -1.0),
}


def box_triangles(size: tuple[float, float, float]) -> list:
    """An axis-aligned box as the twelve triangles of its closed surface.

    Closed on purpose: the voxelizer flood-fills from the grid boundary, so an open shell
    would come back hollow and the dimensions being asserted would still be right for the
    wrong reason.
    """
    sx, sy, sz = size
    corners = [(x * sx, y * sy, z * sz) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [  # corner index is x * 4 + y * 2 + z
        (0, 1, 3), (0, 3, 2),
        (4, 7, 5), (4, 6, 7),
        (0, 4, 5), (0, 5, 1),
        (2, 3, 7), (2, 7, 6),
        (0, 2, 6), (0, 6, 4),
        (1, 5, 7), (1, 7, 3),
    ]
    return [tuple(corners[i] for i in face) for face in faces]


def vox_size(path: Path) -> tuple[int, int, int]:
    """The SIZE chunk of a .vox — x, y, z, where z is the axis MagicaVoxel draws upward."""
    data = path.read_bytes()
    start = data.index(b"SIZE") + 12  # chunk id, content length, children length
    return struct.unpack("<3i", data[start : start + 12])


def determinant(rows) -> float:
    (a, b, c), (d, e, f), (g, h, i) = rows
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def test_every_up_axis_is_a_rotation_and_never_a_mirror(voxels):
    """Moving a component would also put the axis on Z — and flip the model's handedness.

    That failure is invisible on anything symmetrical, which is most test geometry and a
    fair amount of generated geometry too, so it is checked on the matrix rather than on a
    render: determinant +1 is a rotation, -1 is a reflection.
    """
    assert set(voxels.UP_AXES) == set(AXIS_VECTORS), "every named axis needs a rotation"

    for axis in voxels.UP_AXES:
        rotate = voxels._ROTATIONS[axis]
        assert rotate(*AXIS_VECTORS[axis]) == (0.0, 0.0, 1.0), f"{axis} does not end up on Z"
        images = [rotate(*basis) for basis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        assert determinant(images) == pytest.approx(1.0), f"{axis} mirrors the model"


def test_the_up_axis_decides_which_way_the_voxel_model_stands(meshes, tmp_path):
    """Four times as tall in Y as it is wide — which is what glTF means by upright.

    The default has to be `y`, because every mesh this plugin produces comes out of a GLB
    and glTF's own specification puts up on +Y, while .vox puts it on +Z.
    """
    triangles = box_triangles((1.0, 4.0, 1.0))
    upright = tmp_path / "upright.stl"
    sideways = tmp_path / "sideways.stl"
    meshes.write_binary_stl(upright, triangles, "test")
    meshes.write_binary_stl(sideways, triangles, "test")

    default = run_node(
        "voxelize.py",
        node_payload(tmp_path, {"mesh_file": str(upright), "resolution": 32}, {}),
        tmp_path,
    )
    identity = run_node(
        "voxelize.py",
        node_payload(tmp_path, {"mesh_file": str(sideways), "resolution": 32, "up_axis": "z"}, {}),
        tmp_path,
    )

    x, y, z = vox_size(Path(default["vox_file"]))
    assert z > x and z > y, f"the default should stand the model up, got {x}x{y}x{z}"

    x, y, z = vox_size(Path(identity["vox_file"]))
    assert y > x and y > z, f"`z` should rotate nothing, got {x}x{y}x{z}"

    # Same model either way round: a rotation moves voxels, it does not create them.
    assert default["voxel_count"] == identity["voxel_count"]


def test_an_unknown_up_axis_is_refused_before_anything_is_written(tmp_path):
    """A pipeline file edited by hand does not go through the port's enum."""
    mesh = tmp_path / "shape.stl"
    mesh.write_bytes(bytes(84))  # never read: the axis is checked first

    payload = node_payload(tmp_path, {"mesh_file": str(mesh), "up_axis": "up"}, {})
    process = subprocess.run(
        [sys.executable, str(NODES / "voxelize.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        timeout=60,
    )

    assert process.returncode == 1
    assert "Unknown up axis 'up'" in process.stderr
    assert not list(tmp_path.glob("*.vox"))


# ------------------------------------------------------------------ settings and cache


def load_registry(project: Path, settings: dict):
    config = AppConfig()
    config.plugins["hunyuan3d"] = PluginConfig(settings=settings)
    return plugin_loader.load_all(ProjectPaths.for_project(project), config)


def test_hunyuan_settings_become_the_port_defaults_that_the_cache_key_sees(project: Path):
    """§8.2: a default lands in a node's params, and params are what get hashed."""
    registry = load_registry(
        project,
        {
            "default_model_path": "tencent/Hunyuan3D-2mini",
            "default_steps": 8,
            "default_guidance_scale": 7.5,
            "default_octree_resolution": 384,
            "default_num_chunks": 4000,
            "default_max_num_view": 4,
            "default_texture_resolution": "768",
            "default_face_count": 25000,
            "default_texture": True,
            "default_remove_background": False,
            "default_output_format": "glb",
        },
    )

    ports = {
        port.id: port.default for port in registry.node_type("hunyuan3d.image_to_mesh").inputs
    }
    assert ports["model_path"] == "tencent/Hunyuan3D-2mini"
    assert ports["steps"] == 8
    assert ports["guidance_scale"] == 7.5
    assert ports["octree_resolution"] == 384
    assert ports["num_chunks"] == 4000
    assert ports["max_num_view"] == 4
    assert ports["texture_resolution"] == "768"
    assert ports["face_count"] == 25000
    assert ports["texture"] is True
    assert ports["remove_background"] is False
    assert ports["output_format"] == "glb"


def test_a_setting_the_page_would_never_write_leaves_the_schema_default(project: Path):
    """`config.yaml` is hand-editable, so a bad value must not reach a node's schema."""
    registry = load_registry(
        project,
        {
            "default_steps": 9000,  # above the port maximum
            "default_octree_resolution": 32,  # below what the server accepts
            "default_output_format": "",  # blank
            "default_model_path": "   ",  # whitespace only
            "default_texture_resolution": 1024,  # not one of the two the server takes
        },
    )

    ports = {
        port.id: port.default for port in registry.node_type("hunyuan3d.image_to_mesh").inputs
    }
    assert ports["seed"] == 0, "no setting writes this port; the schema default stands"
    assert ports["steps"] == 100, "clamped to the port maximum, not taken as given"
    assert ports["octree_resolution"] == 64, "clamped up to the server's own minimum"
    assert ports["output_format"] == "stl"
    assert ports["model_path"] == "tencent/Hunyuan3D-2.1"
    assert ports["texture_resolution"] == "512"


def test_the_multi_view_node_takes_the_same_project_defaults(project: Path):
    """One set of settings, both generation nodes — except the checkpoint, which differs."""
    registry = load_registry(
        project,
        {
            "default_steps": 8,
            "default_octree_resolution": 384,
            "default_output_format": "glb",
            "default_model_path": "tencent/Hunyuan3D-2mini",
            "default_mv_model_path": "/models/Hunyuan3D-2mv",
        },
    )

    ports = {
        port.id: port.default
        for port in registry.node_type("hunyuan3d.multiview_to_mesh").inputs
    }
    assert ports["steps"] == 8
    assert ports["octree_resolution"] == 384
    assert ports["output_format"] == "glb"
    assert ports["model_path"] == "/models/Hunyuan3D-2mv"
    single = {p.id: p.default for p in registry.node_type("hunyuan3d.image_to_mesh").inputs}
    assert single["model_path"] == "tencent/Hunyuan3D-2mini", "the two checkpoints stay apart"


def test_the_multi_view_node_asks_for_four_views_and_needs_one(project: Path):
    registry = load_registry(project, {})

    schema = registry.node_type("hunyuan3d.multiview_to_mesh")
    required = {port.id for port in schema.inputs if port.required}
    assert [port.id for port in schema.inputs][:4] == ["front", "left", "back", "right"]
    assert required == {"front"}
    assert {port.id for port in schema.outputs} == {"mesh_file", "triangle_count", "views_used"}
    # No engine wall clock, for the same reason the single-image node has none.
    assert schema.execution.timeout_seconds is None
    assert schema.execution.resource_class == "gpu"


def test_the_transport_settings_are_deliberately_not_port_defaults(project: Path):
    """Reaching the same checkpoint over another URL is not a different result."""
    registry = load_registry(
        project,
        {
            "hunyuan_mode": "local",
            "hunyuan_server_url": "http://gpu-box:8081",
            "hunyuan_transport": "tasks",
        },
    )

    port_ids = {port.id for port in registry.node_type("hunyuan3d.image_to_mesh").inputs}
    assert not port_ids & {"hunyuan_mode", "hunyuan_server_url", "hunyuan_transport"}


# ---------------------------------------------------------------------------- colour

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)

CHECKER = [[RED, GREEN], [BLUE, YELLOW]]
"""Two by two, top row first — which is the row order both PNG and glTF UVs use."""


def png(rows: list[list[tuple[int, int, int]]], *, filters: list[int] | None = None) -> bytes:
    """A truecolour 8-bit PNG, optionally with a different row filter on every scanline.

    The filters are written out here rather than left at 0 because they are the half of
    the decoder that indexes into the previous row, which is exactly where an off-by-one
    hides — and a real texture out of any exporter uses all five.
    """
    height = len(rows)
    width = len(rows[0])
    stride = width * 3
    raw = bytearray()
    previous = bytearray(stride)
    for index, row in enumerate(rows):
        line = bytearray(value for pixel in row for value in pixel)
        kind = (filters or [0] * height)[index]
        encoded = bytearray(stride)
        for i in range(stride):
            left = line[i - 3] if i >= 3 else 0
            up = previous[i]
            upper_left = previous[i - 3] if i >= 3 else 0
            if kind == 1:
                encoded[i] = (line[i] - left) & 0xFF
            elif kind == 2:
                encoded[i] = (line[i] - up) & 0xFF
            elif kind == 3:
                encoded[i] = (line[i] - ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else upper_left
                encoded[i] = (line[i] - predictor) & 0xFF
            else:
                encoded[i] = line[i]
        raw += bytes([kind]) + encoded
        previous = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        payload = kind + body
        return struct.pack(">I", len(body)) + payload + struct.pack(">I", zlib.crc32(payload))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def pack_glb(document: dict, binary: bytes) -> bytes:
    chunk = json.dumps(document).encode("utf-8")
    chunk += b" " * ((-len(chunk)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(chunk) + 8 + len(binary)
    out = b"glTF" + struct.pack("<II", 2, total)
    out += struct.pack("<II", len(chunk), 0x4E4F534A) + chunk
    out += struct.pack("<II", len(binary), 0x004E4942) + binary
    return out


QUAD_UVS = [(0.25, 0.75), (0.75, 0.75), (0.75, 0.25), (0.25, 0.25)]
"""Texel centres, on purpose: a UV of exactly 1.0 wraps back to column 0 under REPEAT,
so a quad unwrapped edge to edge would make every expectation here a coin toss."""


def textured_quad_glb(image: bytes, *, uvs: bool = True, mime: str = "image/png") -> bytes:
    """A unit quad in the XY plane whose four corners sit on the four texels of a 2x2."""
    binary = b"".join(
        struct.pack("<3f", *vertex) for vertex in ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0))
    )
    binary += b"".join(struct.pack("<2f", *uv) for uv in QUAD_UVS)
    binary += struct.pack("<6H", 0, 1, 2, 0, 2, 3)
    binary += image

    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {
                "primitives": [
                    {
                        "mode": 4,
                        "attributes": {"POSITION": 0, **({"TEXCOORD_0": 2} if uvs else {})},
                        "indices": 1,
                        "material": 0,
                    }
                ]
            }
        ],
        "materials": [
            {"name": "skin", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}
        ],
        "textures": [{"source": 0}],
        "images": [{"name": "albedo", "bufferView": 3, "mimeType": mime}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 4},
            {"bufferView": 1, "componentType": 5123, "type": "SCALAR", "count": 6},
            {"bufferView": 2, "componentType": 5126, "type": "VEC2", "count": 4},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 48},
            {"buffer": 0, "byteOffset": 80, "byteLength": 12},
            {"buffer": 0, "byteOffset": 48, "byteLength": 32},
            {"buffer": 0, "byteOffset": 92, "byteLength": len(image)},
        ],
        "buffers": [{"byteLength": 92 + len(image)}],
    }
    return pack_glb(document, binary)


TEXTURED_QUAD_OBJ = """mtllib quad.mtl
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
vt 0.25 0.25
vt 0.75 0.25
vt 0.75 0.75
vt 0.25 0.75
usemtl skin
f 1/1 2/2 3/3
f 1/1 3/3 4/4
"""
"""The same quad. OBJ's V runs upward, so these are the glTF UVs mirrored — which is the
one thing `meshio` has to undo, and what the corner colours below actually pin."""


def vox_model(path: Path) -> tuple[dict[tuple[int, int, int], tuple[int, int, int]], list]:
    """Every voxel's colour, resolved through the file's own palette."""
    data = path.read_bytes()
    at = data.index(b"XYZI") + 12
    count = struct.unpack("<I", data[at : at + 4])[0]
    at += 4
    indices = {}
    for _ in range(count):
        x, y, z, index = struct.unpack("<4B", data[at : at + 4])
        indices[(x, y, z)] = index
        at += 4

    rgba = data.index(b"RGBA") + 12
    palette = [struct.unpack("<4B", data[rgba + i * 4 : rgba + i * 4 + 4]) for i in range(256)]
    # Colour index 0 means empty, so index i reads palette entry i - 1.
    voxels = {cell: palette[index - 1][:3] for cell, index in indices.items()}
    return voxels, palette


def quad_corners(path: Path, size: int = 16) -> dict[str, tuple[int, int, int]]:
    voxels, _ = vox_model(path)
    return {
        "origin": voxels[(0, 0, 0)],
        "x": voxels[(size - 1, 0, 0)],
        "xy": voxels[(size - 1, size - 1, 0)],
        "y": voxels[(0, size - 1, 0)],
    }


def test_a_textured_glb_voxelises_into_its_own_colours(tmp_path):
    """The whole point of the texture pass, and the .vox threw all of it away before."""
    mesh = tmp_path / "quad.glb"
    mesh.write_bytes(textured_quad_glb(png(CHECKER)))

    outputs = run_node(
        "voxelize.py",
        node_payload(tmp_path, {"mesh_file": str(mesh), "resolution": 16, "up_axis": "z"}, {}),
        tmp_path,
    )

    corners = quad_corners(Path(outputs["vox_file"]))
    assert corners == {"origin": BLUE, "x": YELLOW, "xy": GREEN, "y": RED}


def test_the_texture_port_colours_a_mesh_whose_maps_were_never_bundled(tmp_path):
    """An OBJ on its own has UVs and no image; the port is what completes it."""
    mesh = tmp_path / "quad.obj"
    mesh.write_text(TEXTURED_QUAD_OBJ, encoding="utf-8")
    texture = tmp_path / "albedo.png"
    texture.write_bytes(png(CHECKER))

    outputs = run_node(
        "voxelize.py",
        node_payload(
            tmp_path,
            {
                "mesh_file": str(mesh),
                "texture": str(texture),
                "resolution": 16,
                "up_axis": "z",
            },
            {},
        ),
        tmp_path,
    )

    corners = quad_corners(Path(outputs["vox_file"]))
    assert corners == {"origin": BLUE, "x": YELLOW, "xy": GREEN, "y": RED}


def test_the_texture_port_wins_over_the_one_inside_the_mesh(tmp_path):
    """Re-skinning a generated mesh must not mean exporting it again."""
    mesh = tmp_path / "quad.glb"
    mesh.write_bytes(textured_quad_glb(png(CHECKER)))
    override = tmp_path / "flat.png"
    override.write_bytes(png([[GREEN, GREEN], [GREEN, GREEN]]))

    outputs = run_node(
        "voxelize.py",
        node_payload(
            tmp_path,
            {
                "mesh_file": str(mesh),
                "texture": str(override),
                "resolution": 16,
                "up_axis": "z",
            },
            {},
        ),
        tmp_path,
    )

    # Green throughout — the checker's four colours are gone — but shaded rather than
    # flat, because one colour over a whole model says nothing about its shape.
    for corner, (red, green, blue) in quad_corners(Path(outputs["vox_file"])).items():
        assert red == 0 and blue == 0, f"{corner} is not a shade of the override"
        assert 0 < green <= 255, f"{corner} lost the colour entirely"


def test_a_texture_that_cannot_be_read_ends_the_node_rather_than_being_ignored(tmp_path):
    """Connected on purpose: voxelising grey anyway would look like the port doing nothing."""
    mesh = tmp_path / "quad.glb"
    mesh.write_bytes(textured_quad_glb(png(CHECKER)))
    texture = tmp_path / "albedo.png"
    texture.write_bytes(b"\x89PNG\r\n\x1a\nnot really")

    process = subprocess.run(
        [sys.executable, str(NODES / "voxelize.py")],
        input=json.dumps(
            node_payload(tmp_path, {"mesh_file": str(mesh), "texture": str(texture)}, {})
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        timeout=60,
    )

    assert process.returncode == 1
    assert not list(tmp_path.glob("*.vox"))


def test_a_mesh_with_no_colour_in_it_keeps_the_height_ramp(meshes, tmp_path):
    """An STL names no material at all, and a grey gradient still reads as a model."""
    mesh = tmp_path / "box.stl"
    meshes.write_binary_stl(mesh, box_triangles((1.0, 1.0, 1.0)), "test")

    outputs = run_node(
        "voxelize.py",
        node_payload(tmp_path, {"mesh_file": str(mesh), "resolution": 16}, {}),
        tmp_path,
    )

    _, palette = vox_model(Path(outputs["vox_file"]))
    assert palette[0] == (90, 210, 140, 255), "the ramp's first entry"
    assert palette[21][:3] != palette[0][:3], "and it is still a ramp"


def test_the_texture_is_carried_through_the_zip_a_textured_obj_arrives_as(meshes, tmp_path):
    """`output_format=obj` comes back as the OBJ, its MTL and its maps in one archive."""
    bundle = tmp_path / "textured.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("quad.obj", TEXTURED_QUAD_OBJ)
        archive.writestr("quad.mtl", "newmtl skin\nKd 1 1 1\nmap_Kd albedo.png\n")
        archive.writestr("albedo.png", png(CHECKER))

    surface = meshes.read_parts(bundle)
    assert len(surface.textures) == 1
    assert surface.parts[0].texture == 0
    assert surface.parts[0].uvs is not None


def test_read_parts_finds_the_base_colour_texture_inside_a_glb(meshes, tmp_path):
    mesh = tmp_path / "quad.glb"
    image = png(CHECKER)
    mesh.write_bytes(textured_quad_glb(image))

    surface = meshes.read_parts(mesh)

    assert surface.warnings == []
    assert [texture.name for texture in surface.textures] == ["albedo"]
    assert surface.textures[0].data == image
    assert surface.parts[0].texture == 0
    assert surface.parts[0].uvs[0] == (QUAD_UVS[0], QUAD_UVS[1], QUAD_UVS[2])


def test_a_texture_with_no_uvs_to_land_on_is_reported_and_not_guessed_at(meshes, tmp_path):
    mesh = tmp_path / "quad.glb"
    # The material still points at the image; only the coordinates that place it are gone.
    mesh.write_bytes(textured_quad_glb(png(CHECKER), uvs=False))

    surface = meshes.read_parts(mesh)

    assert surface.parts[0].texture is None
    assert any("TEXCOORD_0" in warning for warning in surface.warnings), surface.warnings


def test_reading_a_mesh_that_names_no_material_still_answers_with_one_part(meshes, tmp_path):
    mesh = tmp_path / "shape.stl"
    meshes.write_binary_stl(mesh, box_triangles((1.0, 1.0, 1.0)), "test")

    surface = meshes.read_parts(mesh)

    assert len(surface.parts) == 1
    assert surface.parts[0].texture is None and surface.parts[0].color is None
    assert surface.triangle_count() == 12


# ------------------------------------------------------------------------- imaging


def test_every_png_row_filter_decodes_to_the_same_image(images):
    rows = [[(i * 8 % 256, j * 16 % 256, (i + j) * 4 % 256) for j in range(6)] for i in range(5)]

    decoded = images.decode_png(png(rows, filters=[0, 1, 2, 3, 4]))

    assert (decoded.width, decoded.height) == (6, 5)
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            at = (y * 6 + x) * 3
            assert tuple(decoded.pixels[at : at + 3]) == pixel, f"pixel {x},{y}"


def png_raw(
    width: int,
    height: int,
    depth: int,
    colour: int,
    rows: list[bytes],
    palette: bytes = b"",
) -> bytes:
    """A PNG of any colour type, every scanline unfiltered — the fixture for the widths."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        payload = kind + body
        return struct.pack(">I", len(body)) + payload + struct.pack(">I", zlib.crc32(payload))

    out = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    )
    if palette:
        out += chunk(b"PLTE", palette)
    return (
        out
        + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + row for row in rows)))
        + chunk(b"IEND", b"")
    )


def test_the_other_png_widths_a_real_texture_arrives_in(images):
    """Greyscale, indexed, 16-bit and alpha are all ordinary exporter output."""
    grey = images.decode_png(png_raw(2, 1, 8, 0, [bytes([0, 255])]))
    assert grey.texel(0.25, 0.5) == (0, 0, 0) and grey.texel(0.75, 0.5) == (255, 255, 255)

    # Four bits per pixel, two pixels to the byte: entries 1 and 0 of the palette.
    indexed = images.decode_png(
        png_raw(2, 1, 4, 3, [bytes([0x10])], palette=bytes(RED + GREEN))
    )
    assert indexed.texel(0.25, 0.5) == GREEN and indexed.texel(0.75, 0.5) == RED

    deep = images.decode_png(png_raw(1, 1, 16, 2, [struct.pack(">3H", 65535, 0, 32768)]))
    assert deep.texel(0.5, 0.5) == (255, 0, 128)

    # Alpha is read past, not read: a voxel is opaque.
    with_alpha = images.decode_png(png_raw(1, 1, 8, 6, [bytes((*GREEN, 0))]))
    assert with_alpha.texel(0.5, 0.5) == GREEN


def test_a_texel_is_looked_up_with_the_row_order_gltf_means(images):
    decoded = images.decode_png(png(CHECKER))

    # v runs downward from the top of the image, so 0.25 is the top row.
    assert decoded.texel(0.25, 0.25) == RED
    assert decoded.texel(0.75, 0.25) == GREEN
    assert decoded.texel(0.25, 0.75) == BLUE
    assert decoded.texel(0.75, 0.75) == YELLOW


def test_a_uv_outside_the_image_is_wrapped_the_way_its_sampler_asked(images):
    decoded = images.decode_png(png(CHECKER))

    assert decoded.texel(1.25, 0.25) == RED, "1.25 repeats to 0.25; repeat is the default"
    assert decoded.texel(1.25, 0.25, images.CLAMP_TO_EDGE) == GREEN, "held at the last column"
    assert decoded.texel(-0.25, 0.25, images.CLAMP_TO_EDGE) == RED, "held at the first"
    assert decoded.texel(-0.25, 0.25) == GREEN, "and wrapped round to the far column"


def test_an_interlaced_png_is_refused_rather_than_mis_decoded(images):
    bytes_ = bytearray(png(CHECKER))
    bytes_[8 + 8 + 12] = 1  # the interlace byte of IHDR
    # The CRC is now wrong, which nothing here checks — the point is the refusal above it.
    with pytest.raises(images.ImageError, match="Interlaced"):
        images.decode_png(bytes(bytes_))


def test_a_format_that_needs_a_real_codec_names_what_is_missing(images):
    try:
        import PIL  # noqa: F401

        pytest.skip("Pillow is installed, so this environment takes the other path")
    except ImportError:
        pass

    # WebP, which neither built-in decoder handles — unlike PNG and JPEG, which both do.
    with pytest.raises(images.ImageError, match="image/webp"):
        images.decode(b"RIFF\x00\x00\x00\x00WEBPVP8 ", name="albedo.webp")


# ------------------------------------------------------------------------- palette


def test_the_palette_is_cut_down_to_what_a_vox_can_hold(voxels):
    """255 entries, not 256: index 0 means empty."""
    colours = {(x, 0, 0): (x, x % 7, 255 - x) for x in range(1000)}

    entries, index_of = voxels.palette_for(colours)

    assert 0 < len(entries) <= 255
    assert set(index_of.values()) <= set(range(1, len(entries) + 1))
    assert len(index_of) == len({colour for colour in colours.values()})


def test_a_model_of_four_colours_keeps_all_four_exactly(voxels):
    colours = {(i, 0, 0): colour for i, colour in enumerate([RED, GREEN, BLUE, YELLOW])}

    entries, index_of = voxels.palette_for(colours)

    assert sorted(entries) == sorted([RED, GREEN, BLUE, YELLOW])
    assert len({index_of[colour] for colour in (RED, GREEN, BLUE, YELLOW)}) == 4


def test_the_interior_takes_the_colour_of_the_shell_it_sits_behind(voxels):
    solid = {(x, 0, 0) for x in range(5)}
    coloured = voxels.spread_colours({(0, 0, 0): RED}, solid)

    assert coloured == {cell: RED for cell in solid}


# ---------------------------------------------------------------------------- jpeg

class _BitWriter:
    """MSB-first, with JPEG's 0xFF byte stuffing — the encoder half of the fixture."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.buffer = 0
        self.count = 0

    def write(self, value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            self.buffer = (self.buffer << 1) | ((value >> shift) & 1)
            self.count += 1
            if self.count == 8:
                self.out.append(self.buffer)
                if self.buffer == 0xFF:
                    self.out.append(0x00)
                self.buffer = 0
                self.count = 0

    def flush(self) -> bytes:
        while self.count:
            self.write(1, 1)  # pad the last byte with ones, as the spec asks
        return bytes(self.out)


DC_CODE_LENGTH = 4
"""Every magnitude category gets a 4-bit code, so the fixture's tables are one line each."""

QUANT_DC = 8
"""With a DC quantum of 8 the reconstruction is exactly ``coefficient + 128``, which is
what lets these tests assert pixel values rather than approximations."""


def baseline_jpeg(width: int, height: int, components: list, blocks: list) -> bytes:
    """A baseline JPEG carrying only DC coefficients.

    ``components`` is a list of ``(h, v)`` sampling factors; ``blocks`` gives, per
    component, the flat 8x8-block grid of *pixel values* that component should decode to.
    Every block is written as its DC coefficient followed by an immediate end-of-block, so
    the file is small and the expected output is exactly what was asked for.
    """

    def marker(kind: int, body: bytes) -> bytes:
        return bytes([0xFF, kind]) + struct.pack(">H", len(body) + 2) + body

    h_max = max(h for h, _ in components)
    v_max = max(v for _, v in components)
    mcus_x = -(-width // (8 * h_max))
    mcus_y = -(-height // (8 * v_max))

    out = b"\xff\xd8"
    out += marker(0xDB, bytes([0x00]) + bytes([QUANT_DC] + [1] * 63))

    frame = struct.pack(">BHHB", 8, height, width, len(components))
    for index, (h, v) in enumerate(components):
        frame += bytes([index + 1, (h << 4) | v, 0])
    out += marker(0xC0, frame)

    # DC: twelve magnitude categories, each a 4-bit code equal to its own symbol.
    counts = [0] * 16
    counts[DC_CODE_LENGTH - 1] = 12
    out += marker(0xC4, bytes([0x00]) + bytes(counts) + bytes(range(12)))
    # AC: one symbol, end-of-block, as the 2-bit code 00.
    ac_counts = [0] * 16
    ac_counts[1] = 1
    out += marker(0xC4, bytes([0x10]) + bytes(ac_counts) + bytes([0x00]))

    scan = bytes([len(components)])
    for index in range(len(components)):
        scan += bytes([index + 1, 0x00])
    scan += bytes([0, 63, 0])
    out += marker(0xDA, scan)

    writer = _BitWriter()
    predictions = [0] * len(components)
    for mcu in range(mcus_x * mcus_y):
        mcu_x, mcu_y = mcu % mcus_x, mcu // mcus_x
        for index, (h, v) in enumerate(components):
            stride = mcus_x * h
            for by in range(v):
                for bx in range(h):
                    at = (mcu_y * v + by) * stride + mcu_x * h + bx
                    coefficient = blocks[index][at] - 128
                    diff = coefficient - predictions[index]
                    predictions[index] = coefficient
                    if diff == 0:
                        writer.write(0, DC_CODE_LENGTH)
                    else:
                        category = abs(diff).bit_length()
                        writer.write(category, DC_CODE_LENGTH)
                        writer.write(
                            diff if diff > 0 else diff + (1 << category) - 1, category
                        )
                    writer.write(0b00, 2)  # end of block
    return out + writer.flush() + b"\xff\xd9"


def test_a_baseline_jpeg_decodes_to_one_pixel_per_block(images):
    """1/8 scale is the contract, and the DC coefficient is the block's own mean."""
    values = [0, 85, 170, 255]
    decoded = images.decode_jpeg(baseline_jpeg(16, 16, [(1, 1)], [values]))

    assert (decoded.width, decoded.height) == (2, 2)
    assert [decoded.pixels[i * 3] for i in range(4)] == values
    assert decoded.texel(0.25, 0.25) == (0, 0, 0)
    assert decoded.texel(0.75, 0.75) == (255, 255, 255)


def test_a_subsampled_jpeg_upsamples_its_chroma(images):
    """4:2:0 is what every texture out of Hunyuan3D is, so the factors have to be read."""
    luma = [40, 90, 140, 190]  # 2x2 blocks of luma in one MCU
    neutral = [128]            # one Cb and one Cr block covering all four
    decoded = images.decode_jpeg(
        baseline_jpeg(16, 16, [(2, 2), (1, 1), (1, 1)], [luma, neutral, neutral])
    )

    assert (decoded.width, decoded.height) == (2, 2)
    # Neutral chroma means the luma passes straight through as grey.
    assert [decoded.pixels[i * 3] for i in range(4)] == luma
    assert decoded.texel(0.25, 0.25) == (40, 40, 40)
    assert decoded.texel(0.75, 0.75) == (190, 190, 190)


def test_jpeg_chroma_reaches_the_colour(images):
    grey = images.decode_jpeg(baseline_jpeg(8, 8, [(1, 1), (1, 1), (1, 1)], [[128], [128], [128]]))
    assert grey.texel(0.5, 0.5) == (128, 128, 128)

    # Cr above neutral is red; Cb above neutral is blue. The exact values are the JFIF
    # matrix, which is what pins that the conversion is the standard one and not a guess.
    red = images.decode_jpeg(baseline_jpeg(8, 8, [(1, 1), (1, 1), (1, 1)], [[128], [128], [228]]))
    r, g, b = red.texel(0.5, 0.5)
    assert r == 255 and g < 90 and b == 128, (r, g, b)

    blue = images.decode_jpeg(baseline_jpeg(8, 8, [(1, 1), (1, 1), (1, 1)], [[128], [228], [128]]))
    r, g, b = blue.texel(0.5, 0.5)
    assert b == 255 and g < 128 and r == 128, (r, g, b)


def test_a_progressive_jpeg_is_refused_by_name(images):
    baseline = bytearray(baseline_jpeg(16, 16, [(1, 1)], [[0, 85, 170, 255]]))
    baseline[baseline.index(b"\xff\xc0") + 1] = 0xC2  # SOF0 -> SOF2

    with pytest.raises(images.ImageError, match="[Pp]rogressive"):
        images.decode_jpeg(bytes(baseline))


def test_the_container_is_decided_by_the_bytes_and_never_by_the_declaration(images):
    """Hunyuan3D writes `data:image/png;base64,` in front of JPEG bytes, every time.

    Believing that label is what sent a perfectly decodable texture down the "this needs
    Pillow" path and left the model one flat colour.
    """
    jpeg = baseline_jpeg(16, 16, [(1, 1)], [[0, 85, 170, 255]])
    assert images.sniff(jpeg) == "image/jpeg"

    decoded = images.decode(jpeg, mime="image/png", name="albedo.png")
    assert (decoded.width, decoded.height) == (2, 2)

    # And the other way round, for symmetry.
    assert images.sniff(png(CHECKER)) == "image/png"
    assert images.decode(png(CHECKER), mime="image/jpeg").texel(0.25, 0.25) == RED


def test_a_glb_whose_texture_is_a_mislabelled_jpeg_still_colours_the_voxels(tmp_path):
    """The whole reported bug, end to end: this is exactly what the generator writes."""
    # Every component 1x1, so the image is 2x2 blocks and the mislabelled MIME type is
    # the only thing wrong with the file — which is the case that was failing.
    jpeg = baseline_jpeg(
        16,
        16,
        [(1, 1), (1, 1), (1, 1)],
        [[40, 90, 140, 190], [128, 128, 128, 128], [128, 128, 128, 128]],
    )
    mesh = tmp_path / "quad.glb"
    mesh.write_bytes(textured_quad_glb(jpeg, mime="image/png"))

    outputs = run_node(
        "voxelize.py",
        node_payload(tmp_path, {"mesh_file": str(mesh), "resolution": 16, "up_axis": "z"}, {}),
        tmp_path,
    )

    corners = quad_corners(Path(outputs["vox_file"]))
    assert len(set(corners.values())) == 4, f"the four texels collapsed: {corners}"


# -------------------------------------------------------------------------- shading


def test_a_model_of_one_flat_colour_is_shaded_rather_than_written_as_a_block(voxels):
    """A `baseColorFactor` with no texture used to paint every voxel the same value."""
    shade = voxels.shading({(0, 0, z): (200, 40, 40) for z in range(8)})

    assert shade.entries == [], "one colour is not a palette"
    assert shade.tint == (200, 40, 40), "and the colour is not thrown away either"
    entries = [voxels._palette_entry(shade, i) for i in range(22)]
    assert len(set(entries)) > 1, "the ramp has to actually vary"
    assert all(red > green and red > blue for red, green, blue in entries), "and stay red"
    assert entries[0] < entries[21], "dark at the bottom, light at the top"


def test_a_model_with_real_colours_keeps_them(voxels):
    shade = voxels.shading({(x, 0, 0): (x * 20, 0, 0) for x in range(6)})

    assert shade.tint is None
    assert len(shade.entries) == 6


def test_a_model_with_no_colour_at_all_gets_the_original_ramp(voxels):
    shade = voxels.shading(None)

    assert shade.entries == [] and shade.tint is None
    assert voxels._palette_entry(shade, 0) == (90, 210, 140)
