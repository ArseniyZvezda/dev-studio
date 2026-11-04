"""Up to four views of one object -> mesh, through ``hunyuan3d-dit-v2-mv``.

The multi-view half of the plugin. Where ``image_to_mesh.py`` sends one picture and lets
the server's own default choose the shape model, this one sends a map of views and asks
for ``mv`` by name — the node *is* that model, so the request says so rather than relying
on how the server happened to be started, and the cache key carries it (§8.2).

``front`` is required and the other three are optional, which is the same rule the server
has (at least one view, never more than four) narrowed by one: the texture pass upstream is
conditioned on a single image, and a mesh textured from its left side is a surprise nobody
asked for. One picture and nothing else belongs on Hunyuan3D (Image → Mesh) instead, whose
single-view checkpoint is the better model for that input.
"""

from __future__ import annotations

import time

import _voxarium_studio as td
import hunyuan3d


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)
    settings = td.settings(payload)

    views = {
        view: td.require_file(values[view], f"{view} view")
        for view in hunyuan3d.VIEWS
        if values.get(view)
    }
    if "front" not in views:
        extra = f" (got {', '.join(views)})" if views else ""
        td.fail(
            "The multi-view model needs at least a front view: connect one to the Front "
            f"view port{extra}. The other three are optional — every view you do have is "
            "geometry the model builds instead of inventing."
        )
    seed = int(values.get("seed") or 0)

    output_format = str(values.get("output_format") or "stl").strip().lower()
    if output_format not in hunyuan3d.FORMATS:
        td.fail(f"Unknown output format {output_format!r}; expected one of {hunyuan3d.FORMATS}")

    td.log(f"views: {', '.join(f'{view}={path.name}' for view, path in views.items())}")

    stem = views["front"].stem.lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in stem)[:40].strip("-")
    # `-mv-` and not just the seed: the same front picture through the other node with the
    # same seed is a different mesh, and two of them in one assets directory must not be
    # one file.
    output = td.assets_dir(payload) / f"{slug or 'mesh'}-mv-{seed}.{output_format}"

    started = time.monotonic()
    options = hunyuan3d.options_from(
        values, settings, views=views, seed=seed, shape_model="mv"
    )
    result = hunyuan3d.generate(options, output)
    output, triangles, backend = result.path, result.triangle_count, result.backend

    elapsed = time.monotonic() - started
    counted = "unknown" if triangles is None else str(triangles)
    td.log(f"wrote {counted} triangles to {output} in {elapsed:.2f}s via {backend}")
    td.emit(
        {
            "mesh_file": str(output),
            "triangle_count": triangles,
            "views_used": len(views),
        }
    )


if __name__ == "__main__":
    main()
