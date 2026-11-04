"""Mesh -> MagicaVoxel .vox.

Surface-rasterises every triangle into a grid, then flood-fills from the grid boundary
to separate outside from enclosed space, producing a solid model rather than a shell.
Pure stdlib, so it runs whether or not the plugin environment has been provisioned.

Reading the mesh is :mod:`meshio`'s job, and that is why this node takes more than STL:
Hunyuan3D's server exports ``glb`` and ``obj`` and has no STL exporter at all, so a
pipeline whose voxelizer only spoke STL could not be fed by the real generator.

The two formats also disagree about which way is up, which is what ``up_axis`` is for —
see :func:`orient`.

**Colour rides the same rasterisation as the geometry.** Each surface sample already
knows its barycentric weights, so where the mesh carries UVs and a base colour texture
the sample looks up its own texel; where it carries only a flat material colour it takes
that; and where it carries neither, the old height ramp is written unchanged. What comes
out is a real .vox palette of up to 255 colours, median-cut from what was actually
sampled — which is the point: a textured Hunyuan3D mesh voxelised into a grey gradient
threw away the entire texture pass.
"""

from __future__ import annotations

import struct
from collections import Counter, deque
from pathlib import Path
from typing import NamedTuple

import _voxarium_studio as td
import imaging
import meshio

MAX_RESOLUTION = 200  # .vox tops out at 256 per axis

MAX_PALETTE = 255
"""Colour index 0 means "empty" in a .vox, so a model has 255 colours, not 256."""

UP_AXES = ("y", "z", "x", "-y", "-z", "-x")
"""Which axis of the source mesh points up, ordered with the default first.

Duplicated in ``plugin.py``, which declares the port: this script runs out of process in
the plugin's own venv, where the plugin package is not importable.
"""

_ROTATIONS = {
    # Each entry maps a source vertex onto one whose +Z is the named axis. They are written
    # out rather than derived by swapping components because every one of them has to be a
    # proper rotation: simply moving a component would also put the axis where it belongs
    # and would hand back a mirrored model, which reads as correct until you look at
    # anything with a left and a right.
    "z": lambda x, y, z: (x, y, z),      # identity
    "y": lambda x, y, z: (x, -z, y),     # +90 deg about X — glTF's Y-up into MagicaVoxel's
    "x": lambda x, y, z: (-z, y, x),     # -90 deg about Y
    "-z": lambda x, y, z: (x, -y, -z),   # 180 deg about X — the model arrived upside down
    "-y": lambda x, y, z: (x, z, -y),    # -90 deg about X
    "-x": lambda x, y, z: (z, y, -x),    # +90 deg about Y
}


def orient(triangles, up_axis: str):
    """Rotate a mesh so that ``up_axis`` becomes .vox's +Z.

    MagicaVoxel is Z-up; glTF says +Y is up in its own specification, and that is what
    Hunyuan3D's server exports — so are the STL and OBJ this plugin derives from it. Left
    alone, every mesh the generator produces voxelises lying on its back, and the height
    ramp :func:`write_vox` paints runs along the model's depth instead of its height. This
    is the whole reason the port exists, and why its default is ``y`` rather than the
    identity the node applied before.

    Nothing here is scale-aware: :func:`voxelise` re-derives the bounds afterwards, so a
    rotation costs one pass over the vertices and nothing else.
    """
    if up_axis == "z":
        return triangles  # the identity, spelled as "do not copy the mesh"
    rotate = _ROTATIONS[up_axis]
    return [tuple(rotate(*vertex) for vertex in triangle) for triangle in triangles]


def orient_parts(parts, up_axis: str):
    """:func:`orient`, per material. UVs are untouched — a rotation does not move them."""
    if up_axis == "z":
        return parts
    return [part._replace(triangles=orient(part.triangles, up_axis)) for part in parts]


# --------------------------------------------------------------------------- colour


class Paint:
    """Where one part's colour comes from: a texture, a flat value, or nowhere."""

    __slots__ = ("image", "wrap_s", "wrap_t", "tint", "flat")

    def __init__(self, image, wrap_s: int, wrap_t: int, tint) -> None:
        self.image = image
        self.wrap_s = wrap_s
        self.wrap_t = wrap_t
        self.tint = tint
        # Precomputed because a flat part asks for this once per surface sample, and the
        # multiply below is the whole of what the tint ever does.
        self.flat = None if tint is None else tuple(_byte(channel) for channel in tint)

    def at(self, u: float, v: float):
        """The colour under a UV, or the flat colour when there is no image."""
        if self.image is None:
            return self.flat
        r, g, b = self.image.texel(u, v, self.wrap_s, self.wrap_t)
        if self.tint is None:
            return (r, g, b)
        # glTF's base colour is factor x texel; `meshio` hands the factor over already
        # sRGB-encoded, so this is a plain multiply in the space the palette is in.
        return (
            _clamp_byte(r * self.tint[0]),
            _clamp_byte(g * self.tint[1]),
            _clamp_byte(b * self.tint[2]),
        )


def _byte(value: float) -> int:
    return _clamp_byte(value * 255.0)


def _clamp_byte(value: float) -> int:
    return 0 if value < 0 else 255 if value > 255 else int(value)


def paints(surface: meshio.Surface, override, warn) -> list[Paint]:
    """One colour source per part, with every image decoded exactly once.

    ``override`` is the node's Texture port: an image supplied from outside wins over
    whatever the file carries, which is what makes the port useful for an OBJ whose maps
    were never bundled and for re-skinning a mesh without re-exporting it. It still needs
    UVs to land on — a texture with nowhere to go is reported, not guessed at.
    """
    decoded: dict[int, imaging.Image | None] = {}
    out: list[Paint] = []
    for part in surface.parts:
        image = override
        wrap_s = wrap_t = imaging.REPEAT
        if image is None and part.texture is not None:
            texture = surface.textures[part.texture]
            wrap_s, wrap_t = texture.wrap_s, texture.wrap_t
            if part.texture not in decoded:
                try:
                    decoded[part.texture] = imaging.decode(
                        texture.data, mime=texture.mime, name=texture.name
                    )
                except imaging.ImageError as exc:
                    warn(str(exc))
                    decoded[part.texture] = None
            image = decoded[part.texture]
        if image is not None and not part.uvs:
            warn("part has no UV coordinates, so the texture was not applied to it")
            image = None
        tint = part.color
        if image is not None and tint is None:
            # glTF's own default factor. Anything darker would tint every textured mesh.
            tint = (1.0, 1.0, 1.0)
        out.append(Paint(image, wrap_s, wrap_t, tint))
    return out


def voxelise(parts, resolution: int, colours: list[Paint] | None = None):
    """Rasterise every triangle into the grid, sampling its colour as it goes.

    Returns the surface cells, the colour of each one that had a colour to take, and the
    grid dimensions. Colours are averaged rather than last-write-wins: a voxel that
    straddles a seam in the texture should read as the two sides of it, not as whichever
    triangle happened to be rasterised second.
    """
    triangles = [triangle for part in parts for triangle in part.triangles]
    if not triangles:
        raise meshio.MeshError("The mesh has no triangles to voxelise")
    xs = [v[0] for tri in triangles for v in tri]
    ys = [v[1] for tri in triangles for v in tri]
    zs = [v[2] for tri in triangles for v in tri]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    extent = max(max_x - min_x, max_y - min_y, max_z - min_z) or 1.0
    scale = (resolution - 1) / extent

    def to_grid(v):
        return (
            (v[0] - min_x) * scale,
            (v[1] - min_y) * scale,
            (v[2] - min_z) * scale,
        )

    surface: set[tuple[int, int, int]] = set()
    # cell -> [red, green, blue, samples], averaged once the whole surface is in.
    totals: dict[tuple[int, int, int], list[int]] = {}

    for index, part in enumerate(parts):
        paint = colours[index] if colours else None
        # Also the fallback for a *textured* part: an OBJ can carry a face with no vt,
        # and the material's tint is a better answer there than no colour at all.
        flat = paint.flat if paint else None
        uvs = part.uvs if paint and paint.image is not None else None
        for face, triangle in enumerate(part.triangles):
            a, b, c = (to_grid(v) for v in triangle)
            # Sample density follows the longest edge so no voxel-sized gaps appear.
            edge = max(
                _distance(a, b), _distance(b, c), _distance(c, a)
            )
            steps = max(2, int(edge * 2) + 2)
            corners = uvs[face] if uvs else None
            for i in range(steps + 1):
                for j in range(steps + 1 - i):
                    u = i / steps
                    v = j / steps
                    w = 1.0 - u - v
                    point = (
                        a[0] * w + b[0] * u + c[0] * v,
                        a[1] * w + b[1] * u + c[1] * v,
                        a[2] * w + b[2] * u + c[2] * v,
                    )
                    cell = (int(point[0]), int(point[1]), int(point[2]))
                    surface.add(cell)
                    if corners is not None:
                        colour = paint.at(
                            corners[0][0] * w + corners[1][0] * u + corners[2][0] * v,
                            corners[0][1] * w + corners[1][1] * u + corners[2][1] * v,
                        )
                    else:
                        colour = flat
                    if colour is None:
                        continue
                    running = totals.get(cell)
                    if running is None:
                        totals[cell] = [colour[0], colour[1], colour[2], 1]
                    else:
                        running[0] += colour[0]
                        running[1] += colour[1]
                        running[2] += colour[2]
                        running[3] += 1

    dims = (
        max(1, int((max_x - min_x) * scale) + 1),
        max(1, int((max_y - min_y) * scale) + 1),
        max(1, int((max_z - min_z) * scale) + 1),
    )
    averaged = {
        cell: (total[0] // total[3], total[1] // total[3], total[2] // total[3])
        for cell, total in totals.items()
    }
    return surface, averaged, dims


def _distance(a, b) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def fill_solid(
    surface: set[tuple[int, int, int]], dims: tuple[int, int, int]
) -> set[tuple[int, int, int]]:
    """Flood-fill from the boundary; anything unreached and unfilled is interior."""
    width, height, depth = dims
    outside: set[tuple[int, int, int]] = set()
    queue: deque[tuple[int, int, int]] = deque()

    for x in range(width):
        for y in range(height):
            for z in (0, depth - 1):
                cell = (x, y, z)
                if cell not in surface and cell not in outside:
                    outside.add(cell)
                    queue.append(cell)
    for x in range(width):
        for z in range(depth):
            for y in (0, height - 1):
                cell = (x, y, z)
                if cell not in surface and cell not in outside:
                    outside.add(cell)
                    queue.append(cell)
    for y in range(height):
        for z in range(depth):
            for x in (0, width - 1):
                cell = (x, y, z)
                if cell not in surface and cell not in outside:
                    outside.add(cell)
                    queue.append(cell)

    while queue:
        x, y, z = queue.popleft()
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            neighbour = (x + dx, y + dy, z + dz)
            if not (0 <= neighbour[0] < width and 0 <= neighbour[1] < height and 0 <= neighbour[2] < depth):
                continue
            if neighbour in surface or neighbour in outside:
                continue
            outside.add(neighbour)
            queue.append(neighbour)

    solid = set(surface)
    for x in range(width):
        for y in range(height):
            for z in range(depth):
                cell = (x, y, z)
                if cell not in outside:
                    solid.add(cell)
    return solid


def spread_colours(colours: dict, solid: set) -> dict:
    """Give the interior the colour of the nearest surface it sits behind.

    The inside of a solid model is never seen, so this is not about accuracy — it is
    about every voxel having *a* palette index. Doing it by flooding outward from the
    coloured shell means a model sliced open in MagicaVoxel still reads as itself
    instead of as a block of whatever colour happened to be first in the palette.
    """
    if not colours:
        return colours
    filled = dict(colours)
    queue = deque(cell for cell in colours if cell in solid)
    while queue:
        x, y, z = queue.popleft()
        colour = filled[(x, y, z)]
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            neighbour = (x + dx, y + dy, z + dz)
            if neighbour in filled or neighbour not in solid:
                continue
            filled[neighbour] = colour
            queue.append(neighbour)
    return filled


def palette_for(colours: dict, limit: int = MAX_PALETTE):
    """Median-cut the sampled colours down to what a .vox can hold.

    Nearest-neighbour matching afterwards would cost a scan per voxel; splitting buckets
    and remembering which one every colour landed in gives the mapping for free.
    """
    counts = Counter(colours.values())
    buckets = _median_cut(list(counts.items()), limit)

    entries: list[tuple[int, int, int]] = []
    index_of: dict[tuple[int, int, int], int] = {}
    for bucket in buckets:
        if not bucket:
            continue
        weight = sum(count for _, count in bucket)
        entries.append(
            (
                sum(colour[0] * count for colour, count in bucket) // weight,
                sum(colour[1] * count for colour, count in bucket) // weight,
                sum(colour[2] * count for colour, count in bucket) // weight,
            )
        )
        for colour, _ in bucket:
            index_of[colour] = len(entries)  # 1-based: 0 is "empty"
    return entries, index_of


class Shading(NamedTuple):
    """How the model gets painted: its own palette, or the height ramp.

    ``entries`` empty means the ramp, and ``tint`` is the one colour to run it through —
    both None-tint and a colour are ordinary. A palette and a ramp are the two cases;
    there is no third, and keeping them in one value is what stops the writer guessing.
    """

    entries: list
    index_of: dict
    tint: tuple[int, int, int] | None


def shading(colours: dict | None) -> Shading:
    """Decide between the mesh's own palette and the ramp.

    **A model that resolved to a single colour is written as the ramp in that colour, not
    as a flat block.** This is the case a `baseColorFactor` with no texture produces —
    which is most of what Hunyuan3D exports untextured, and what a texture that failed to
    decode falls back to — and painting every voxel one value throws away the whole shape:
    a `.vox` is a thing someone opens and looks at, and a flat block has no silhouette,
    no depth and no readable surface. The ramp goes back underneath, in the material's
    colour rather than the default blue-grey, so nothing about the material is lost.
    """
    distinct = set(colours.values()) if colours else set()
    if len(distinct) >= 2:
        entries, index_of = palette_for(colours)
        return Shading(entries, index_of, None)
    return Shading([], {}, next(iter(distinct)) if distinct else None)


def _median_cut(items: list, limit: int) -> list[list]:
    """Split the widest bucket along its widest channel until there are ``limit`` of them.

    The spread of each bucket is cached and recomputed only for the two halves a split
    produces — without that, this is a full scan of every colour per split, which on a
    baked texture is tens of millions of comparisons for a 255-entry palette.
    """
    if not items:
        return []
    buckets = [items]
    spreads = [_widest(items)]
    while len(buckets) < limit:
        target = max(range(len(buckets)), key=lambda i: spreads[i][0])
        if spreads[target][0] <= 0:
            break  # every remaining bucket is one colour repeated
        axis = spreads[target][1]
        ordered = sorted(buckets[target], key=lambda item: item[0][axis])
        half = len(ordered) // 2
        buckets[target], spreads[target] = ordered[:half], _widest(ordered[:half])
        buckets.append(ordered[half:])
        spreads.append(_widest(ordered[half:]))
    return buckets


def _widest(bucket: list) -> tuple[int, int]:
    """``(spread, channel)`` for the channel this bucket varies most in."""
    if len(bucket) < 2:
        return (0, 0)
    best = (0, 0)
    for axis in range(3):
        low = min(colour[axis] for colour, _ in bucket)
        high = max(colour[axis] for colour, _ in bucket)
        if high - low > best[0]:
            best = (high - low, axis)
    return best


def _chunk(chunk_id: bytes, content: bytes, children: bytes = b"") -> bytes:
    return chunk_id + struct.pack("<II", len(content), len(children)) + content + children


def write_vox(
    path: Path,
    voxels: set[tuple[int, int, int]],
    dims: tuple[int, int, int],
    colours: dict | None = None,
    shade: Shading | None = None,
) -> None:
    """Write a MagicaVoxel .vox file (version 150).

    The palette is the model's own where it has more than one colour to give, and the
    height ramp where it does not — see :func:`shading`, which is where that is decided
    and why. ``shade`` is only ever passed to save recomputing the median cut; it is
    derived from ``colours`` otherwise.
    """
    width, height, depth = dims
    ordered = sorted(voxels)
    shade = shade or shading(colours)

    size_chunk = _chunk(b"SIZE", struct.pack("<3i", width, height, depth))

    body = bytearray(struct.pack("<I", len(ordered)))
    for x, y, z in ordered:
        if shade.entries:
            colour = colours.get((x, y, z)) if colours else None
            # A voxel the shell never reached takes the first entry rather than index 0,
            # which would delete it: 0 is "empty", not "unpainted".
            index = index_of_or_first(shade, colour)
        else:
            # Colour index gradients over height so the result reads as a model, not a blob.
            index = 1 + int((z / max(1, depth - 1)) * 20)
        body += struct.pack("<4B", x & 0xFF, y & 0xFF, z & 0xFF, index)
    xyzi_chunk = _chunk(b"XYZI", bytes(body))

    palette = bytearray()
    for i in range(256):
        palette += struct.pack("<4B", *_palette_entry(shade, i), 255)
    rgba_chunk = _chunk(b"RGBA", bytes(palette))

    children = size_chunk + xyzi_chunk + rgba_chunk
    main_chunk = _chunk(b"MAIN", b"", children)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"VOX " + struct.pack("<i", 150) + main_chunk)


def index_of_or_first(shade: Shading, colour) -> int:
    return shade.index_of.get(colour, 1) if colour is not None else 1


def _palette_entry(shade: Shading, i: int) -> tuple[int, int, int]:
    """Palette entry ``i``, which is colour index ``i + 1``."""
    if shade.entries:
        # Past the end, repeat the last rather than leaving black, so a stray index can
        # never punch a hole in the model.
        return shade.entries[i] if i < len(shade.entries) else shade.entries[-1]
    t = min(1.0, i / 21.0)
    if shade.tint:
        # The same 22 steps, as a light-to-dark run of the material's own colour. 0.55 is
        # the floor because a ramp that reached black would read as shadow, not as shape.
        level = 0.55 + 0.45 * t
        return tuple(_clamp_byte(channel * level) for channel in shade.tint)
    return (int(90 + 120 * t), int(120 + 90 * (1 - t)), int(140 + 60 * t))


def _override_texture(raw) -> imaging.Image | None:
    """The Texture port, read eagerly.

    Fatal when it cannot be read, unlike a texture found inside the mesh: someone
    connected this on purpose, and quietly voxelising grey would look like the node
    ignoring the port.
    """
    path = str(raw or "").strip()
    if not path:
        return None
    file = td.require_file(path, "texture")
    try:
        image = imaging.read(file)
    except imaging.ImageError as exc:
        td.fail(str(exc))
    td.log(f"texture: {file.name} ({image.width}x{image.height})")
    return image


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)

    mesh_path = td.require_file(values.get("mesh_file"), "mesh_file")
    resolution = max(8, min(MAX_RESOLUTION, int(values.get("resolution") or 64)))
    # Validated here rather than trusted: the port schema bounds it, and a pipeline file
    # edited by hand does not go through the port schema.
    up_axis = str(values.get("up_axis") or "y").strip().lower()
    if up_axis not in UP_AXES:
        td.fail(f"Unknown up axis {up_axis!r}; expected one of {', '.join(UP_AXES)}")

    override = _override_texture(values.get("texture"))

    td.log(f"reading {mesh_path}")
    try:
        surface = meshio.read_parts(mesh_path)
    except meshio.MeshError as exc:
        td.fail(str(exc))
    for warning in surface.warnings:
        td.log(f"  {warning}")

    palette = paints(surface, override, lambda message: td.log(f"  {message}"))
    textured = sum(1 for paint in palette if paint.image is not None)
    if textured:
        td.log(f"colour: sampling {textured} of {len(palette)} parts from a texture")
    elif any(paint.flat for paint in palette):
        td.log("colour: flat material colours; no texture in this mesh")
    else:
        td.log("colour: the mesh names none, so the height ramp is used")

    parts = orient_parts(surface.parts, up_axis)
    if up_axis == "z":
        td.log(f"up axis: {up_axis} — already MagicaVoxel's way up, rotating nothing")
    else:
        td.log(f"up axis: {up_axis} — rotating it onto the .vox Z axis")
    td.log(f"{surface.triangle_count()} triangles; voxelising at resolution {resolution}")

    try:
        shell, colours, dims = voxelise(parts, resolution, palette)
    except meshio.MeshError as exc:
        td.fail(str(exc))
    td.log(f"surface voxels: {len(shell)}; grid {dims[0]}x{dims[1]}x{dims[2]}")

    solid = fill_solid(shell, dims)
    td.log(f"solid voxels: {len(solid)}")

    colours = spread_colours(colours, solid)
    shade = shading(colours)
    if shade.entries:
        td.log(f"palette: {len(shade.entries)} colours from the mesh")
    elif shade.tint:
        td.log(
            f"palette: the mesh is one flat colour rgb{shade.tint}, which would write a "
            "block with no shape — shading it by height instead"
        )
    else:
        td.log("palette: the mesh names no colour, so the height ramp is used")

    output = mesh_path.with_suffix(".vox")
    if output == mesh_path:
        output = mesh_path.parent / f"{mesh_path.stem}-voxels.vox"
    write_vox(output, solid, dims, colours, shade)

    td.log(f"wrote {output}")
    td.emit({"vox_file": str(output), "voxel_count": len(solid)})


if __name__ == "__main__":
    main()
