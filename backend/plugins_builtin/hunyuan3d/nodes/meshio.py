"""Reading and writing meshes with nothing but the stdlib.

Every node in this plugin runs out of process in an environment that may never have been
provisioned, so trimesh is not available and must not become a requirement. What is
needed is small and well specified: turn a file into a list of triangles, count the
triangles in one without decoding it, and write a binary STL.

The formats are not an arbitrary set. Hunyuan3D's server exports ``glb`` and ``obj`` and
nothing else (``server/schemas.py``: ``output_format: Literal["glb", "obj"]``), while
``hunyuan3d.voxelize`` was written for STL — so the two have to meet somewhere, and
this module is that place.

:func:`read_triangles` answers the question "what shape is this", and most of the plugin
only asks that. :func:`read_parts` answers "and what colour is it" — the same geometry
split by material, with UVs and the base colour texture's *bytes* attached. Decoding
those bytes is :mod:`imaging`'s job and happens in the one node that needs a colour, so
a conversion that only wants triangles never pays for a texture at all.
"""

from __future__ import annotations

import json
import posixpath
import struct
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple

import _voxarium_studio as td

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Triangle = tuple[Vec3, Vec3, Vec3]
TriangleUV = tuple[Vec2, Vec2, Vec2]


class Texture(NamedTuple):
    """One base colour image, still encoded. ``wrap_s``/``wrap_t`` are glTF's enums."""

    name: str
    data: bytes
    mime: str
    wrap_s: int = 10497
    wrap_t: int = 10497


class Part(NamedTuple):
    """One run of triangles sharing a material.

    ``uvs`` is parallel to ``triangles`` — one UV per corner — or None when the file
    carries none. ``color`` is glTF's ``baseColorFactor``, sRGB-encoded into 0…1, and
    multiplies the texture where there is one; ``texture`` indexes :attr:`Surface.textures`.
    """

    triangles: list[Triangle]
    uvs: list[TriangleUV | None] | None = None
    color: Vec3 | None = None
    texture: int | None = None


class Surface(NamedTuple):
    """A mesh split by material, and everything needed to colour it."""

    parts: list[Part]
    textures: list[Texture]
    warnings: list[str]

    def triangle_count(self) -> int:
        return sum(len(part.triangles) for part in self.parts)

READABLE = (".stl", ".obj", ".glb", ".gltf", ".zip")
"""Suffixes :func:`read_triangles` understands. ``.gltf`` only in its binary framing — a
JSON-with-external-.bin glTF is not something either generator here produces. ``.zip`` is
the textured-OBJ bundle the Hunyuan3D server returns for ``output_format=obj``."""


class MeshError(ValueError):
    """A file that cannot be read as geometry. Callers decide whether that is fatal."""


# --------------------------------------------------------------------------- dispatch


def read_triangles(path: Path) -> list[Triangle]:
    """Every triangle in ``path``, in world space, as flat vertex triples."""
    suffix = path.suffix.lower()
    if suffix == ".stl":
        return read_stl(path)
    if suffix == ".obj":
        return read_obj(path)
    if suffix in (".glb", ".gltf"):
        return read_glb(path)
    if suffix == ".zip":
        return read_obj_zip(path)
    raise MeshError(
        f"Cannot read {suffix or 'a suffixless file'} as a mesh; expected one of {READABLE}"
    )


def read_parts(path: Path) -> Surface:
    """Every triangle in ``path``, split by material, with UVs and textures attached.

    Never raises for a *missing* material: a file with no colour in it at all comes back
    as one untextured part, which is exactly what :func:`read_triangles` would have given
    and is what an STL always is. Only geometry that cannot be read is fatal.
    """
    suffix = path.suffix.lower()
    if suffix == ".obj":
        return _obj_surface(path.read_text(encoding="utf-8", errors="replace"), _sibling_opener(path))
    if suffix in (".glb", ".gltf"):
        return _glb_surface(path)
    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return _obj_surface(_obj_member(path), _archive_opener(archive))
    return Surface(parts=[Part(triangles=read_triangles(path))], textures=[], warnings=[])


def count_triangles(path: Path) -> int | None:
    """Triangle count without decoding the geometry, or None when it cannot be read.

    Deliberately cheap and deliberately forgiving: the count is informational, and a node
    that generated a mesh must not fail because it could not describe it.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".stl":
            return _count_stl(path)
        if suffix == ".obj":
            return _count_obj_lines(path.read_text(encoding="utf-8", errors="replace"))
        if suffix in (".glb", ".gltf"):
            return _count_glb(path)
        if suffix == ".zip":
            return _count_obj_lines(_obj_member(path))
    except (OSError, ValueError, struct.error, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        td.log(f"  could not count triangles in {path.name}: {exc}")
    return None


def write_binary_stl(path: Path, triangles: list[Triangle], header: str) -> None:
    """Binary STL. Normals are recomputed rather than carried over — a mesh converted
    from glTF has none, and every consumer that cares recomputes them anyway."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(header.encode("utf-8", errors="replace")[:80].ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            handle.write(struct.pack("<3f", *_face_normal(a, b, c)))
            for vertex in (a, b, c):
                handle.write(struct.pack("<3f", *vertex))
            handle.write(struct.pack("<H", 0))


def _face_normal(a: Vec3, b: Vec3, c: Vec3) -> Vec3:
    ux, uy, uz = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    wx, wy, wz = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    nx, ny, nz = (uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if not length:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


# -------------------------------------------------------------------------------- stl


def read_stl(path: Path) -> list[Triangle]:
    data = path.read_bytes()
    if len(data) < 84:
        raise MeshError(f"{path.name} is too small to be an STL")
    count = struct.unpack("<I", data[80:84])[0]
    if len(data) < 84 + count * 50:
        # A valid ASCII STL fails the binary size check; parse it rather than refuse it.
        return _read_ascii_stl(data)

    triangles: list[Triangle] = []
    offset = 84
    for _ in range(count):
        values = struct.unpack("<12fH", data[offset : offset + 50])
        triangles.append(
            (
                (values[3], values[4], values[5]),
                (values[6], values[7], values[8]),
                (values[9], values[10], values[11]),
            )
        )
        offset += 50
    return triangles


def _read_ascii_stl(data: bytes) -> list[Triangle]:
    vertices: list[Vec3] = []
    triangles: list[Triangle] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "vertex":
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(vertices) == 3:
                triangles.append((vertices[0], vertices[1], vertices[2]))
                vertices = []
    if not triangles:
        raise MeshError("Could not parse the file as a binary or ASCII STL")
    return triangles


def _count_stl(path: Path) -> int | None:
    data = path.read_bytes()
    if len(data) < 84:
        return None
    count = struct.unpack("<I", data[80:84])[0]
    if len(data) >= 84 + count * 50:
        return count
    return data.decode("utf-8", errors="replace").count("facet normal") or None


# -------------------------------------------------------------------------------- obj


def read_obj(path: Path) -> list[Triangle]:
    return _parse_obj(path.read_text(encoding="utf-8", errors="replace"))


def read_obj_zip(path: Path) -> list[Triangle]:
    return _parse_obj(_obj_member(path))


def _obj_member(path: Path) -> str:
    """The .obj inside a textured-OBJ zip, which is how the server bundles that format."""
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".obj")]
        if not names:
            raise MeshError(f"{path.name} contains no .obj member")
        return archive.read(names[0]).decode("utf-8", errors="replace")


def _parse_obj(text: str) -> list[Triangle]:
    return [triangle for part in _obj_surface(text, None).parts for triangle in part.triangles]


def _obj_surface(text: str, opener: Callable[[str], bytes | None] | None) -> Surface:
    """An OBJ split by ``usemtl``, with whatever its material library points at.

    ``opener`` is how a named sibling is fetched — off the disk beside the .obj, or out
    of the zip the server bundles a textured OBJ as. It is None when only the geometry
    was asked for, and then no material file is read at all.
    """
    vertices: list[Vec3] = []
    coords: list[Vec2] = []
    # Keyed by material name, in the order the file first mentions each: the OBJ decides
    # what is drawn over what, and re-ordering here would change a coplanar overlap.
    groups: dict[str | None, tuple[list[Triangle], list[TriangleUV | None]]] = {}
    libraries: list[str] = []
    current: str | None = None

    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        keyword = parts[0]
        if keyword == "v" and len(parts) >= 4:
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif keyword == "vt" and len(parts) >= 3:
            # OBJ's V runs upward from the bottom-left; every sampler here indexes rows
            # from the top, so the flip belongs at the one place the format is read.
            coords.append((float(parts[1]), 1.0 - float(parts[2])))
        elif keyword == "usemtl" and len(parts) >= 2:
            current = parts[1]
        elif keyword == "mtllib" and len(parts) >= 2:
            libraries.extend(parts[1:])
        elif keyword == "f" and len(parts) >= 4:
            # `f v`, `f v/vt`, `f v//vn`, `f v/vt/vn`.
            corners = [_obj_corner(token, len(vertices), len(coords)) for token in parts[1:]]
            triangles, uvs = groups.setdefault(current, ([], []))
            # An n-gon fans out into n-2 triangles, which is what every consumer expects.
            for i in range(1, len(corners) - 1):
                trio = (corners[0], corners[i], corners[i + 1])
                triangles.append(tuple(vertices[corner[0]] for corner in trio))
                uvs.append(
                    tuple(coords[corner[1]] for corner in trio)
                    if all(corner[1] >= 0 for corner in trio)
                    else None
                )

    if not any(triangles for triangles, _ in groups.values()):
        raise MeshError("The OBJ file declares no faces")

    textures: list[Texture] = []
    warnings: list[str] = []
    materials = _obj_materials(libraries, opener, textures, warnings) if opener else {}

    parts_out: list[Part] = []
    for name, (triangles, uvs) in groups.items():
        if not triangles:
            continue
        color, texture = materials.get(name, (None, None))
        carries_uv = any(uv is not None for uv in uvs)
        if texture is not None and not carries_uv:
            warnings.append(f"{name or 'the mesh'} has a texture but no vt coordinates to place it")
            texture = None
        parts_out.append(
            Part(
                triangles=triangles,
                uvs=uvs if carries_uv else None,
                color=color,
                texture=texture,
            )
        )
    return Surface(parts=parts_out, textures=textures, warnings=warnings)


def _obj_corner(token: str, vertices: int, coords: int) -> tuple[int, int]:
    fields = token.split("/")
    position = _obj_index(fields[0], vertices)
    if position is None:
        raise MeshError(f"OBJ face references vertex {fields[0]}, which is out of range")
    # A missing or out-of-range vt is not fatal — it means this corner is untextured, and
    # a face is only textured when all three of its corners are.
    texture = _obj_index(fields[1], coords) if len(fields) >= 2 and fields[1] else None
    return position, -1 if texture is None else texture


def _obj_index(token: str, total: int) -> int | None:
    try:
        raw = int(token)
    except ValueError:
        return None
    # OBJ indices are 1-based, and negative ones count back from the vertices seen so far.
    index = raw - 1 if raw > 0 else total + raw
    return index if 0 <= index < total else None


def _obj_materials(
    libraries: list[str],
    opener: Callable[[str], bytes | None],
    textures: list[Texture],
    warnings: list[str],
) -> dict[str, tuple[Vec3 | None, int | None]]:
    """``newmtl`` names mapped to their diffuse colour and ``map_Kd`` image.

    ``Kd`` is taken as it is written. Unlike glTF's linear ``baseColorFactor`` an MTL
    diffuse colour is already display-referred, so encoding it again would wash the
    model out.
    """
    materials: dict[str, tuple[Vec3 | None, int | None]] = {}
    current: str | None = None
    for library in libraries:
        data = opener(library)
        if data is None:
            warnings.append(f"{library} is not beside the mesh, so its materials were not read")
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            fields = line.split()
            if not fields:
                continue
            keyword = fields[0].lower()
            if keyword == "newmtl" and len(fields) >= 2:
                current = fields[1]
                materials.setdefault(current, (None, None))
            elif current is None:
                continue
            elif keyword == "kd" and len(fields) >= 4:
                try:
                    colour = tuple(min(max(float(field), 0.0), 1.0) for field in fields[1:4])
                except ValueError:
                    continue
                materials[current] = (colour, materials[current][1])
            elif keyword == "map_kd" and len(fields) >= 2:
                # `map_Kd` may carry options ahead of the name (`-s 1 1 1 albedo.png`),
                # and a bare filename is the last field either way.
                materials[current] = (
                    materials[current][0],
                    _obj_texture(fields[-1], opener, textures, warnings),
                )
    return materials


def _obj_texture(
    name: str,
    opener: Callable[[str], bytes | None],
    textures: list[Texture],
    warnings: list[str],
) -> int | None:
    for index, texture in enumerate(textures):
        if texture.name == name:
            return index
    data = opener(name)
    if not data:
        warnings.append(f"{name} is not beside the mesh, so the model was left untextured")
        return None
    textures.append(Texture(name=name, data=data, mime=_mime_for(name)))
    return len(textures) - 1


def _sibling_opener(path: Path) -> Callable[[str], bytes | None]:
    """Files an OBJ names, read from beside it — and never from outside it.

    A mesh is often generated rather than authored, and `mtllib ../../../etc/passwd` is a
    line anyone can put in one. The node already runs with the server's rights, so the
    directory the .obj sits in is the whole of what it may reach.
    """
    root = path.parent.resolve()

    def open_named(name: str) -> bytes | None:
        for candidate in (name.replace("\\", "/"), posixpath.basename(name.replace("\\", "/"))):
            resolved = (root / candidate).resolve()
            if resolved != root and root not in resolved.parents:
                continue
            if resolved.is_file():
                return resolved.read_bytes()
        return None

    return open_named


def _archive_opener(archive: zipfile.ZipFile) -> Callable[[str], bytes | None]:
    """The same lookup inside a zip, by base name: the bundle is flat in practice, and
    an MTL written next to the OBJ may still name its map with a path from elsewhere."""
    members = {posixpath.basename(name).lower(): name for name in archive.namelist()}

    def open_named(name: str) -> bytes | None:
        member = members.get(posixpath.basename(name.replace("\\", "/")).lower())
        return archive.read(member) if member else None

    return open_named


_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tga": "image/x-tga",
}


def _mime_for(name: str) -> str:
    suffix = posixpath.splitext(name.replace("\\", "/"))[1].lower()
    return _MIME.get(suffix, "")


def _count_obj_lines(text: str) -> int:
    total = 0
    for line in text.splitlines():
        if line.startswith("f "):
            total += max(0, len(line.split()) - 3)
    return total


# -------------------------------------------------------------------------------- glb

_GLTF_JSON_CHUNK = 0x4E4F534A
_GLTF_BIN_CHUNK = 0x004E4942
_GLTF_TRIANGLE_MODES = (4, 5, 6)  # TRIANGLES, TRIANGLE_STRIP, TRIANGLE_FAN

_COMPONENT = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_COMPONENTS_PER_ELEMENT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_NORMALIZED = {5120: 1 / 127, 5121: 1 / 255, 5122: 1 / 32767, 5123: 1 / 65535}
"""What one unit of a normalized integer attribute is worth, per component type."""

_IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def read_glb(path: Path) -> list[Triangle]:
    """Every triangle in a GLB, with each node's transform applied.

    Hunyuan3D exports one mesh at the identity, but a GLB that has been through any other
    tool need not — and geometry read in local space would voxelise at the wrong scale
    without ever looking wrong enough to notice.
    """
    return [triangle for part in _glb_surface(path).parts for triangle in part.triangles]


def _glb_surface(path: Path) -> Surface:
    """The same walk, keeping each primitive apart with its material."""
    document, binary = _split_glb(path.read_bytes())
    meshes = document.get("meshes") or []
    nodes = document.get("nodes") or []
    textures: list[Texture] = []
    warnings: list[str] = []
    # glTF texture index -> our own, or None for one that could not be read. Memoised
    # because a file naming an image it does not carry should say so once, not per
    # primitive — and because the bytes are shared rather than copied per material.
    resolved: dict[int, int | None] = {}
    parts: list[Part] = []

    def collect(mesh_index: int, transform: tuple[float, ...]) -> None:
        if mesh_index >= len(meshes):
            return
        for primitive in meshes[mesh_index].get("primitives") or []:
            part = _primitive_part(
                document, binary, primitive, transform, textures, resolved, warnings
            )
            if part is not None:
                parts.append(part)

    for node_index, transform in _node_transforms(document, nodes):
        mesh_index = nodes[node_index].get("mesh")
        if mesh_index is not None:
            collect(mesh_index, transform)

    if not parts and meshes:
        # No scene graph reaching them: some exporters write meshes no node references.
        for index in range(len(meshes)):
            collect(index, _IDENTITY)
    if not parts:
        raise MeshError("The glTF file contains no triangles")
    return Surface(parts=parts, textures=textures, warnings=warnings)


def _split_glb(data: bytes) -> tuple[dict, bytes]:
    """The JSON document and the binary buffer of a GLB container."""
    if len(data) < 20 or data[:4] != b"glTF":
        raise MeshError("Not a binary glTF (no glTF magic)")
    document: dict | None = None
    binary = b""
    offset = 12
    while offset + 8 <= len(data):
        length, kind = struct.unpack("<II", data[offset : offset + 8])
        body = data[offset + 8 : offset + 8 + length]
        if len(body) < length:
            raise MeshError("Truncated glTF chunk")
        if kind == _GLTF_JSON_CHUNK and document is None:
            document = json.loads(body.decode("utf-8", errors="replace"))
        elif kind == _GLTF_BIN_CHUNK and not binary:
            binary = body
        offset += 8 + length + (-length % 4)
    if document is None:
        raise MeshError("Binary glTF has no JSON chunk")
    return document, binary


def _node_transforms(document: dict, nodes: list[dict]):
    """Walk the scene graph, yielding ``(node index, world matrix)`` for every node."""
    scenes = document.get("scenes") or []
    scene = document.get("scene", 0)
    roots = list(scenes[scene].get("nodes", [])) if scene < len(scenes) else []
    if not roots:
        roots = list(range(len(nodes)))

    stack = [(index, _IDENTITY) for index in reversed(roots)]
    seen: set[int] = set()
    while stack:
        index, parent = stack.pop()
        # A malformed file can name a node twice, or point a child back at an ancestor;
        # visiting each once keeps that from looping forever inside a node script.
        if index in seen or index >= len(nodes):
            continue
        seen.add(index)
        world = _multiply(parent, _local_matrix(nodes[index]))
        yield index, world
        for child in reversed(nodes[index].get("children") or []):
            stack.append((child, world))


def _local_matrix(node: dict) -> tuple[float, ...]:
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        return tuple(float(value) for value in matrix)

    tx, ty, tz = node.get("translation") or (0.0, 0.0, 0.0)
    qx, qy, qz, qw = node.get("rotation") or (0.0, 0.0, 0.0, 1.0)
    sx, sy, sz = node.get("scale") or (1.0, 1.0, 1.0)
    # Column-major, as glTF stores matrices: R*S fills the upper 3x3, T the last column.
    rotation = (
        1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy + qz * qw), 2 * (qx * qz - qy * qw),
        2 * (qx * qy - qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz + qx * qw),
        2 * (qx * qz + qy * qw), 2 * (qy * qz - qx * qw), 1 - 2 * (qx * qx + qy * qy),
    )
    scale = (sx, sx, sx, sy, sy, sy, sz, sz, sz)
    scaled = tuple(r * s for r, s in zip(rotation, scale))
    return (
        scaled[0], scaled[1], scaled[2], 0.0,
        scaled[3], scaled[4], scaled[5], 0.0,
        scaled[6], scaled[7], scaled[8], 0.0,
        float(tx), float(ty), float(tz), 1.0,
    )


def _multiply(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """``a * b`` for column-major 4x4s: apply b first, then a."""
    out: list[float] = []
    for column in range(4):
        for row in range(4):
            out.append(sum(a[k * 4 + row] * b[column * 4 + k] for k in range(4)))
    return tuple(out)


def _apply(matrix: tuple[float, ...], v: Vec3) -> Vec3:
    x, y, z = v
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )


def _primitive_part(
    document: dict,
    binary: bytes,
    primitive: dict,
    transform: tuple[float, ...],
    textures: list[Texture],
    resolved: dict[int, int | None],
    warnings: list[str],
) -> Part | None:
    mode = primitive.get("mode", 4)
    if mode not in _GLTF_TRIANGLE_MODES:
        return None  # points and lines carry no surface to voxelise

    attributes = primitive.get("attributes") or {}
    position = attributes.get("POSITION")
    if position is None:
        return None
    points = _read_accessor(document, binary, position)
    if not points:
        return None
    vertices = [_apply(transform, (p[0], p[1], p[2])) for p in points]

    indices_accessor = primitive.get("indices")
    if indices_accessor is None:
        order = list(range(len(vertices)))
    else:
        order = [int(value[0]) for value in _read_accessor(document, binary, indices_accessor)]
    if any(index >= len(vertices) for index in order):
        raise MeshError("glTF primitive indexes a vertex its POSITION accessor does not have")

    faces = _faces(order, mode)
    if not faces:
        return None

    material_index = primitive.get("material")
    texture, tex_coord = _base_texture(
        document, binary, material_index, textures, resolved, warnings
    )
    # UVs are never transformed — they index the image, not the world — so they are the
    # one attribute copied straight through.
    coords: list[Vec2] | None = None
    if texture is not None:
        accessor = attributes.get(f"TEXCOORD_{tex_coord}")
        if accessor is None:
            warnings.append(
                f"a textured primitive carries no TEXCOORD_{tex_coord}, so it was left flat"
            )
            texture = None
        else:
            supplied = _read_accessor(document, binary, accessor)
            if len(supplied) < len(vertices):
                warnings.append("a textured primitive has fewer UVs than vertices")
                texture = None
            else:
                coords = [(float(value[0]), float(value[1])) for value in supplied]

    return Part(
        triangles=[tuple(vertices[i] for i in face) for face in faces],
        uvs=[tuple(coords[i] for i in face) for face in faces] if coords else None,
        color=_base_color(document, material_index),
        texture=texture,
    )


def _faces(order: list[int], mode: int) -> list[tuple[int, int, int]]:
    """Every triangle mode collapsed to a plain list of corner index triples."""
    faces: list[tuple[int, int, int]] = []
    if mode == 4:
        for i in range(0, len(order) - 2, 3):
            faces.append((order[i], order[i + 1], order[i + 2]))
    elif mode == 5:  # strip: each new vertex closes a triangle, and the winding alternates
        for i in range(len(order) - 2):
            a, b, c = order[i], order[i + 1], order[i + 2]
            if i % 2:
                a, b = b, a
            faces.append((a, b, c))
    else:  # fan: every triangle shares the first vertex
        for i in range(1, len(order) - 1):
            faces.append((order[0], order[i], order[i + 1]))
    return faces


def _base_color(document: dict, material_index: int | None) -> Vec3 | None:
    """``baseColorFactor``, sRGB-encoded.

    glTF stores it in linear light and every consumer here — a MagicaVoxel palette, a
    canvas — is sRGB. Skip the encode and every mid-tone comes out visibly darker than
    the file meant, which on a flat-coloured mesh reads as bad lighting.
    """
    materials = document.get("materials") or []
    if material_index is None or not 0 <= material_index < len(materials):
        return None
    factor = (materials[material_index].get("pbrMetallicRoughness") or {}).get("baseColorFactor")
    if not isinstance(factor, list) or len(factor) < 3:
        return None
    encoded = []
    for value in factor[:3]:
        linear = min(max(float(value), 0.0), 1.0)
        encoded.append(linear * 12.92 if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055)
    return (encoded[0], encoded[1], encoded[2])


def _base_texture(
    document: dict,
    binary: bytes,
    material_index: int | None,
    textures: list[Texture],
    resolved: dict[int, int | None],
    warnings: list[str],
) -> tuple[int | None, int]:
    """A material's ``baseColorTexture``, and which TEXCOORD set places it.

    Only that one channel, and deliberately: it is what the surface looks like, and it is
    what Hunyuan3D's texture pass bakes. Normal, metallic/roughness, occlusion and
    emissive maps are ignored — a voxel has one colour, and evaluating a PBR material to
    pick it would be inventing detail the .vox cannot hold.
    """
    materials = document.get("materials") or []
    if material_index is None or not 0 <= material_index < len(materials):
        return None, 0
    material = materials[material_index]
    info = (material.get("pbrMetallicRoughness") or {}).get("baseColorTexture")
    if not isinstance(info, dict) or info.get("index") is None:
        return None, 0

    key = int(info["index"])
    tex_coord = int(info.get("texCoord") or 0)
    if key in resolved:
        return resolved[key], tex_coord

    index: int | None = None
    try:
        index = _read_gltf_texture(document, binary, key, info, material, textures)
    except MeshError as exc:
        warnings.append(str(exc))
    resolved[key] = index
    return index, tex_coord


def _read_gltf_texture(
    document: dict,
    binary: bytes,
    key: int,
    info: dict,
    material: dict,
    textures: list[Texture],
) -> int:
    named = f'"{material["name"]}"' if material.get("name") else "a material"
    entries = document.get("textures") or []
    if key >= len(entries):
        raise MeshError(f"{named} names texture {key}, which this file does not carry")
    entry = entries[key]
    if (info.get("extensions") or {}).get("KHR_texture_transform"):
        # Applying it means composing a UV matrix per texture; nothing that reaches this
        # plugin writes one, and a silently untransformed texture reads as a broken unwrap.
        raise MeshError(f"{named} uses KHR_texture_transform, which is not applied here")

    extensions = entry.get("extensions") or {}
    source = entry.get("source")
    if source is None:
        source = (extensions.get("EXT_texture_webp") or {}).get("source")
    if source is None:
        raise MeshError(
            f"{named} stores its texture as Basis/KTX2, which cannot be decoded here"
            if extensions.get("KHR_texture_basisu")
            else f"{named} names a texture with no image"
        )

    images = document.get("images") or []
    if source >= len(images):
        raise MeshError(f"{named} names image {source}, which this file does not carry")
    image = images[source]

    if image.get("uri"):
        uri = str(image["uri"])
        if not uri.lower().startswith("data:"):
            # Unlike the browser previewer, this side *could* open the sibling file — but
            # the GLB is a single self-contained file everywhere this plugin produces one,
            # and reaching outside it for a path the mesh names is a wider door than the
            # feature is worth.
            raise MeshError(f"{named} keeps its texture in a separate file ({uri})")
        data = _decode_data_uri(uri)
        mime = uri[5 : uri.find(",")].split(";")[0]
    elif image.get("bufferView") is not None:
        views = document.get("bufferViews") or []
        view_index = int(image["bufferView"])
        if view_index >= len(views):
            raise MeshError(f"{named} names bufferView {view_index}, which does not exist")
        view = views[view_index]
        if view.get("buffer", 0) != 0 or not binary:
            raise MeshError(f"{named} keeps its texture outside the GLB's own buffer")
        start = int(view.get("byteOffset", 0))
        end = start + int(view.get("byteLength", 0))
        if end > len(binary):
            raise MeshError(f"{named} stores its texture past the end of the buffer")
        data = binary[start:end]
        mime = str(image.get("mimeType") or "")
    else:
        raise MeshError(f"{named} names an image with neither a URI nor a bufferView")

    if not data:
        raise MeshError(f"{named} names an empty image")

    samplers = document.get("samplers") or []
    sampler = entry.get("sampler")
    wrap = samplers[sampler] if isinstance(sampler, int) and 0 <= sampler < len(samplers) else {}
    textures.append(
        Texture(
            name=str(image.get("name") or entry.get("name") or material.get("name") or "base colour"),
            data=data,
            mime=mime,
            wrap_s=int(wrap.get("wrapS") or 10497),
            wrap_t=int(wrap.get("wrapT") or 10497),
        )
    )
    return len(textures) - 1


def _decode_data_uri(uri: str) -> bytes:
    import base64  # noqa: PLC0415 — only a self-contained .gltf ever needs it

    comma = uri.find(",")
    if comma < 0:
        raise MeshError("Malformed data: URI in this glTF")
    payload = uri[comma + 1 :]
    if ";base64" not in uri[:comma].lower():
        from urllib.parse import unquote_to_bytes  # noqa: PLC0415

        return unquote_to_bytes(payload)
    return base64.b64decode(payload + "=" * (-len(payload) % 4))


def _read_accessor(document: dict, binary: bytes, index: int) -> list[tuple[float, ...]]:
    accessors = document.get("accessors") or []
    if index >= len(accessors):
        raise MeshError(f"glTF accessor {index} does not exist")
    accessor = accessors[index]

    component = _COMPONENT.get(accessor.get("componentType"))
    per_element = _COMPONENTS_PER_ELEMENT.get(accessor.get("type"))
    if component is None or per_element is None:
        raise MeshError(
            f"Unsupported glTF accessor {accessor.get('type')}/{accessor.get('componentType')}"
        )
    code, size = component
    count = int(accessor.get("count", 0))
    if count <= 0:
        return []

    view_index = accessor.get("bufferView")
    if view_index is None:
        # A sparse-only accessor is legal and means "zeros, plus the sparse entries";
        # nothing this plugin talks to emits one, and guessing is worse than saying so.
        raise MeshError("glTF accessor without a bufferView is not supported")
    views = document.get("bufferViews") or []
    if view_index >= len(views):
        raise MeshError(f"glTF bufferView {view_index} does not exist")
    view = views[view_index]
    if view.get("buffer", 0) != 0 or not binary:
        raise MeshError("glTF geometry lives outside the GLB's own buffer")

    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride") or 0) or size * per_element
    if start < 0 or start + (count - 1) * stride + size * per_element > len(binary):
        raise MeshError("glTF accessor reads past the end of the buffer")

    unpack = struct.Struct("<" + code * per_element).unpack_from
    values = [unpack(binary, start + i * stride) for i in range(count)]
    # A normalized integer attribute is ordinary — UVs are routinely unsigned shorts — so
    # it is scaled back into 0…1 rather than read as a raw count. The clamp is glTF's own:
    # both -128/127 and -32768/32767 mean -1.
    scale = _NORMALIZED.get(accessor.get("componentType")) if accessor.get("normalized") else None
    if scale:
        return [tuple(max(value * scale, -1.0) for value in element) for element in values]
    return values


def _count_glb(path: Path) -> int | None:
    """Sum the triangles across every primitive, from the JSON chunk alone."""
    try:
        document, _ = _split_glb(path.read_bytes())
    except MeshError:
        return None

    accessors = document.get("accessors") or []
    total = 0
    for mesh in document.get("meshes") or []:
        for primitive in mesh.get("primitives") or []:
            mode = primitive.get("mode", 4)
            if mode not in _GLTF_TRIANGLE_MODES:
                continue
            index = primitive.get("indices")
            if index is None:
                index = (primitive.get("attributes") or {}).get("POSITION")
            if index is None or index >= len(accessors):
                continue
            elements = int(accessors[index].get("count", 0))
            total += elements // 3 if mode == 4 else max(0, elements - 2)
    return total
