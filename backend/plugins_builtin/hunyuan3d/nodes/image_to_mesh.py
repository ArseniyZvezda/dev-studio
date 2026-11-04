"""One reference image -> mesh, written as an STL, OBJ or GLB artifact.

The single-view half of the plugin: ``hunyuan3d-dit-v2-1`` conditioned on one picture. Its
sibling ``multiview_to_mesh.py`` runs the multi-view checkpoint on up to four views of the
same object; the two share everything below the input, which is :mod:`hunyuan3d`.

This script is a thin shell: read the payload, require a picture, flatten node params and
plugin settings into one options object, and hand it over.

The path that comes back is not always the one handed over — a textured OBJ from the
server is a zip — so the emitted ``mesh_file`` is whatever the backend actually wrote.
"""

from __future__ import annotations

import time

import _voxarium_studio as td
import hunyuan3d


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)
    settings = td.settings(payload)

    image = values.get("image")
    if not image:
        td.fail("No reference image provided — Hunyuan3D generates from a picture")
    image_path = td.require_file(image, "image")
    seed = int(values.get("seed") or 0)

    output_format = str(values.get("output_format") or "stl").strip().lower()
    if output_format not in hunyuan3d.FORMATS:
        td.fail(f"Unknown output format {output_format!r}; expected one of {hunyuan3d.FORMATS}")

    td.log(f"image: {image}")

    slug = "".join(ch if ch.isalnum() else "-" for ch in image_path.stem.lower())[:40].strip("-")
    output = td.assets_dir(payload) / f"{slug or 'mesh'}-{seed}.{output_format}"

    started = time.monotonic()
    options = hunyuan3d.options_from(values, settings, image=image_path, seed=seed)
    result = hunyuan3d.generate(options, output)
    output, triangles, backend = result.path, result.triangle_count, result.backend

    elapsed = time.monotonic() - started
    counted = "unknown" if triangles is None else str(triangles)
    td.log(f"wrote {counted} triangles to {output} in {elapsed:.2f}s via {backend}")
    td.emit({"mesh_file": str(output), "triangle_count": triangles})


if __name__ == "__main__":
    main()
