"""File browser over the managed project (§6.2).

Every path coming from the client is resolved and checked to be inside the managed
project before any I/O happens — the Studio must not become an arbitrary-file-read
endpoint, particularly in server mode. The same check gates the file *operations*
below: creating, renaming, moving, copying and deleting all go through ``resolve()``
before anything touches the disk.

None of the operations publish an event of their own. ``FileWatcher`` sees the change
and ``explorer.file_changed`` follows, which is the same path an external edit or a
pipeline node takes — one producer for that event, so a UI that is correct for a
teammate's edit is correct for its own click too.
"""

from __future__ import annotations

import base64
import codecs
import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..core.config import AppConfig
from ..core.paths import DATA_DIRNAME, ProjectPaths

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".css", ".scss", ".html", ".xml",
    ".cs", ".cpp", ".c", ".h", ".hpp", ".gd", ".shader", ".glsl", ".hlsl", ".log",
    ".gitignore", ".env", ".sh", ".ps1", ".bat", ".sql", ".rs", ".go", ".java", ".kt",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
MODEL_SUFFIXES = {".stl", ".obj", ".fbx", ".glb", ".gltf", ".vox", ".ply", ".dae"}
ARCHIVE_SUFFIXES = {
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".tar", ".tgz", ".jar",
    ".pak", ".whl", ".egg", ".cab", ".msi", ".iso", ".uasset", ".upk",
}
OPAQUE_SUFFIXES = (
    IMAGE_SUFFIXES
    | MODEL_SUFFIXES
    | ARCHIVE_SUFFIXES
    | {
        ".exe", ".dll", ".pdb", ".pdf", ".so", ".dylib", ".lib", ".pyc", ".pyd",
        ".class", ".wasm", ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".avi",
        ".webm", ".ttf", ".otf", ".woff", ".woff2",
    }
)
"""Suffixes ``preview()`` will not sniff. There is nothing to learn from reading the
first bytes of a `.exe`, and answering "text" for one would be worse than useless."""

PreviewKind = Literal["text", "image", "model", "binary", "directory"]
EntryKind = Literal["file", "directory"]

SNIFF_BYTES = 8192
"""How much of a file decides whether it is text. Enough to catch a binary header and
small enough that a preview of a huge file still starts with one read."""

MAX_PREVIEW_BYTES = 20 * 1024 * 1024
"""How much of a file the preview pane may be handed.

**The Studio's own number, not the project's.** It was an ``ExplorerConfig`` field until
2026-09-08, which put it in ``voxarium_studio/config.yaml`` — a file that is committed and shared
with everyone who opens the project (§5). What a browser and this machine can comfortably
hold down a pipe is a property of the *application*, and a game project has no opinion about
it; a per-project value also meant a new default never reached a project that already
existed, since ``save_config`` writes every field. ``show_data_dir`` and ``ignore`` stay in
the config because those really are decisions about a particular project's tree.

Three different ceilings hang off it, which is why it is generous: a text file is decoded up
to this and marked ``truncated`` past it, a hex dump is additionally capped at
``MAX_HEX_BYTES``, and an **image is inlined as a base64 data URL up to four times this** —
so 20 MiB here means an 80 MiB picture still arrives in the JSON, as ~107 MiB of base64.
Past that the payload says so and the client fetches the bytes from ``/api/explorer/raw``
instead, which is the path a game project's real textures take.
"""

MAX_HEX_BYTES = 64 * 1024
HEX_COLUMNS = 16

BOM_CODECS: tuple[tuple[bytes, str], ...] = (
    # UTF-32's BOMs start with UTF-16's, so the wider ones have to be tested first.
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

SINGLE_BYTE_CODECS = ("cp1251", "cp1252", "latin-1")
"""Last-resort codecs for a file that is not UTF-8. Ordered by how much they refuse:
`latin-1` decodes *anything*, so it may only win once the printability test below has
established that the result reads like text."""

PRINTABLE_RATIO = 0.95

DUPLICATE_SUFFIX = "copy"
"""What a duplicate's name gains — see ``_duplicate_target`` for the scheme."""


class PathOutsideProject(PermissionError):
    pass


class OperationRefused(Exception):
    """Well-formed, but not allowed against the project as it stands — HTTP 409.

    Distinct from ``ValueError`` (the request itself is malformed, 400) and from
    ``FileExistsError`` (which the API maps to 409 as well, but which ``overwrite``
    can legitimately turn into a success).
    """


class PartialOperation(Exception):
    """A multi-path operation that stopped partway through.

    ``done`` is what actually landed before ``cause`` stopped it. The client has to be
    told: without it, its tree and its selection both describe a state that never
    existed — some of the files moved and the error implies none did.
    """

    def __init__(self, cause: Exception, done: list[str]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.done = done


@dataclass(slots=True)
class FileEntry:
    name: str
    path: str
    """Always relative to the project root, POSIX-style, so it is safe to echo back."""

    is_dir: bool
    size: int
    modified: float
    kind: PreviewKind
    has_children: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "modified": datetime.fromtimestamp(self.modified, tz=timezone.utc).isoformat(),
            "kind": self.kind,
            "has_children": self.has_children,
        }


@dataclass(slots=True)
class PreviewResult:
    kind: PreviewKind
    path: str
    size: int
    modified: float
    mime: str | None = None
    text: str | None = None
    data_url: str | None = None
    truncated: bool = False
    message: str | None = None
    encoding: str | None = None
    """The codec ``text`` was decoded with — ``utf-8``, ``utf-16-le``, ``cp1251``…
    ``None`` whenever the file is not text, which is how the client tells the two
    apart without re-deriving the rule."""

    hex: str | None = None
    """A formatted hex dump of the leading bytes of a binary file. Produced whole
    server-side; the client prints the string and does no layout of its own."""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "size": self.size,
            "modified": datetime.fromtimestamp(self.modified, tz=timezone.utc).isoformat(),
            "mime": self.mime,
            "text": self.text,
            "data_url": self.data_url,
            "truncated": self.truncated,
            "message": self.message,
            "encoding": self.encoding,
            "hex": self.hex,
        }


def classify(path: Path) -> PreviewKind:
    """Guess a file's kind from its **suffix alone**.

    This runs for every entry of every directory the tree lists, so it must never read
    a file: a folder of ten thousand assets would otherwise cost ten thousand opens.
    What it produces is therefore a *hint* — ``FileEntry.kind`` is what the UI paints an
    icon from. ``preview()`` is the authority, and it sniffs content: a `LICENSE`, a
    `Makefile` or a `.lua` is listed as ``binary`` here and previewed as text there.
    """
    if path.is_dir():
        return "directory"
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in MODEL_SUFFIXES:
        return "model"
    if suffix in TEXT_SUFFIXES or path.name.startswith("."):
        return "text"
    return "binary"


def sniff_encoding(sample: bytes) -> str | None:
    """The codec these leading bytes are text in, or ``None`` if they are not text.

    The order of the tests is the whole design:

    1. A **BOM settles it.** It is a positive declaration by whoever wrote the file, and
       it has to come first — UTF-16 text is half NUL bytes, so the NUL test below would
       otherwise call every UTF-16 document binary.
    2. A **NUL byte** in a file with no BOM means binary. Nothing that is meant to be
       read as text contains one.
    3. **Strict UTF-8**, which is what almost every text file in a game project is.
    4. A **single-byte codec**, but only if the result reads like text. Random bytes
       decode perfectly happily as latin-1, and answering with mojibake is a worse
       failure than answering "binary" — the user at least gets a hex dump for that.
    """
    for bom, codec in BOM_CODECS:
        if sample.startswith(bom):
            return codec
    if not sample:
        return "utf-8"  # An empty file is an empty text file.
    if b"\x00" in sample:
        return None
    try:
        # `final=False`: an 8 KiB sample can cut a multi-byte sequence in half, and that
        # is evidence about where we stopped reading, not about the file.
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        pass
    else:
        return "utf-8"
    for codec in SINGLE_BYTE_CODECS:
        try:
            decoded = sample.decode(codec)
        except UnicodeDecodeError:
            continue
        if _mostly_printable(decoded):
            return codec
    return None


def hex_dump(data: bytes) -> str:
    """``00000000  4d 5a 90 00 …  |MZ..|`` — the classic ``hexdump -C`` layout.

    Formatted here rather than in the browser because the client renders the string
    as-is: alignment is a property of the dump, not of the viewer's font stack.
    """
    half = HEX_COLUMNS // 2
    width = half * 3 - 1
    lines: list[str] = []
    for offset in range(0, len(data), HEX_COLUMNS):
        chunk = data[offset : offset + HEX_COLUMNS]
        left = " ".join(f"{byte:02x}" for byte in chunk[:half])
        right = " ".join(f"{byte:02x}" for byte in chunk[half:])
        printable = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{offset:08x}  {left:<{width}}  {right:<{width}}  |{printable}|")
    return "\n".join(lines)


class ExplorerService:
    def __init__(self, paths: ProjectPaths, config: AppConfig) -> None:
        self.paths = paths
        self.config = config

    # ---- path safety ----

    def resolve(self, relative: str | None) -> Path:
        """Map a client-supplied relative path to an absolute one inside the project."""
        candidate = (self.paths.root / (relative or "")).resolve()
        if not self.paths.is_inside_project(candidate):
            raise PathOutsideProject(f"Path escapes the managed project: {relative!r}")
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.paths.root).as_posix()
        except ValueError:
            return path.name

    def _is_hidden(self, path: Path) -> bool:
        if path.name == DATA_DIRNAME and path.parent == self.paths.root:
            return not self.config.explorer.show_data_dir
        return path.name in set(self.config.explorer.ignore)

    # ---- listing ----

    def list_dir(self, relative: str | None = None) -> list[FileEntry]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {relative!r}")

        entries: list[FileEntry] = []
        try:
            children = list(directory.iterdir())
        except PermissionError:
            log.warning("Permission denied listing %s", directory)
            return []

        for child in children:
            if self._is_hidden(child):
                continue
            try:
                stat = child.stat()
                is_dir = child.is_dir()
            except OSError:
                continue
            entries.append(
                FileEntry(
                    name=child.name,
                    path=self.relative(child),
                    is_dir=is_dir,
                    size=0 if is_dir else stat.st_size,
                    modified=stat.st_mtime,
                    # A hint only, and deliberately cheap — see `classify`.
                    kind=classify(child),
                    has_children=is_dir and _has_children(child),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def tree(self, relative: str | None = None, *, depth: int = 1) -> dict[str, Any]:
        """One level by default; the UI expands lazily so huge projects stay responsive."""
        root = self.resolve(relative)
        return {
            "path": self.relative(root) if root != self.paths.root else "",
            "name": root.name,
            "entries": [entry.to_json() for entry in self.list_dir(relative)],
            "children": (
                {
                    entry.path: self.tree(entry.path, depth=depth - 1)
                    for entry in self.list_dir(relative)
                    if entry.is_dir
                }
                if depth > 1
                else {}
            ),
        }

    # ---- preview ----

    def preview(self, relative: str) -> PreviewResult:
        path = self.resolve(relative)
        if not path.exists():
            raise FileNotFoundError(relative)
        stat = path.stat()
        kind = classify(path)
        rel = self.relative(path)
        mime = mimetypes.guess_type(path.name)[0]
        limit = MAX_PREVIEW_BYTES

        if kind == "directory":
            return PreviewResult(
                kind="directory", path=rel, size=0, modified=stat.st_mtime
            )

        if kind == "image":
            if stat.st_size > limit * 4:
                return PreviewResult(
                    kind="image",
                    path=rel,
                    size=stat.st_size,
                    modified=stat.st_mtime,
                    mime=mime,
                    message="Image is too large to inline; open it from disk.",
                )
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return PreviewResult(
                kind="image",
                path=rel,
                size=stat.st_size,
                modified=stat.st_mtime,
                mime=mime or "application/octet-stream",
                data_url=f"data:{mime or 'application/octet-stream'};base64,{encoded}",
            )

        if kind == "model":
            # Metadata only. 3D preview is not built in (§14) — it arrives as a
            # plugin-registered previewer, which the API layer attaches to this payload
            # when one claims the suffix.
            return PreviewResult(
                kind="model",
                path=rel,
                size=stat.st_size,
                modified=stat.st_mtime,
                mime=mime,
                message="No plugin is installed that previews this 3D format.",
            )

        # From here the suffix has had its say and it is not enough: `classify` cannot
        # know that `LICENSE`, `Makefile` and `combat.lua` are text. Content decides —
        # except for the suffixes that are opaque by construction, which are skipped so
        # a 300 MB archive is not opened to be told what its extension already said.
        encoding = (
            None
            if path.suffix.lower() in OPAQUE_SUFFIXES
            else sniff_encoding(_read_head(path, SNIFF_BYTES))
        )
        if encoding is not None:
            return self._text_preview(path, rel, stat, mime, encoding, limit)
        return self._binary_preview(path, rel, stat, mime, limit)

    def _text_preview(
        self,
        path: Path,
        rel: str,
        stat: os.stat_result,
        mime: str | None,
        encoding: str,
        limit: int,
    ) -> PreviewResult:
        # `limit + 1` rather than the stat size: the file is the authority on its own
        # length, and reading one byte past the cap is what makes `truncated` honest
        # without loading a gigabyte to find out.
        data = _read_head(path, limit + 1)
        truncated = len(data) > limit
        # Line endings are normalised, which is the *opposite* of the rule the Documents
        # layer follows — and for the opposite reason. This is a read-only view: nothing
        # here is ever written back, so there is no round trip to preserve, and a stray
        # CR reaching the browser is a rendering artefact rather than information.
        # `size` still reports the real bytes on disk.
        text = _decode(data[:limit], encoding).replace("\r\n", "\n").replace("\r", "\n")
        return PreviewResult(
            kind="text",
            path=rel,
            size=stat.st_size,
            modified=stat.st_mtime,
            mime=mime or "text/plain",
            text=text,
            truncated=truncated,
            encoding=encoding,
        )

    def _binary_preview(
        self, path: Path, rel: str, stat: os.stat_result, mime: str | None, limit: int
    ) -> PreviewResult:
        cap = max(0, min(limit, MAX_HEX_BYTES))
        data = _read_head(path, cap + 1)
        truncated = len(data) > cap
        return PreviewResult(
            kind="binary",
            path=rel,
            size=stat.st_size,
            modified=stat.st_mtime,
            mime=mime,
            # No `message`: there *is* a preview now. The client falls back to its own
            # wording if it has nothing to render the dump with.
            hex=hex_dump(data[:cap]),
            truncated=truncated,
        )

    # ---- operations ----
    #
    # Each one answers with the project-relative paths it produced, so the client can
    # select or reveal them. None of them publishes an event: the watcher does that.

    def create(self, parent: str, name: str, kind: EntryKind) -> list[str]:
        if kind not in ("file", "directory"):
            raise ValueError(f"Unknown entry kind: {kind!r}")
        directory = self._resolve_directory(parent)
        target = self._child(directory, name)
        self._guard_operand(target)
        if target.exists():
            raise FileExistsError(f"{self.relative(target)} already exists.")
        if kind == "directory":
            target.mkdir()
        else:
            # "x" rather than "w": a file that appeared between the check and here is
            # someone else's, and truncating it is not what "create" was asked for.
            with target.open("xb"):
                pass
        return [self.relative(target)]

    def rename(self, path: str, name: str) -> list[str]:
        source = self._resolve_existing(path)
        self._guard_operand(source)
        target = self._child(source.parent, name)
        self._guard_operand(target)
        if target == source:
            return [self.relative(source)]
        # `_same_entry` is the case-only rename on a case-insensitive filesystem:
        # `Readme.md` → `README.md` has a target that "exists" and is the source.
        if target.exists() and not _same_entry(source, target):
            raise FileExistsError(f"{self.relative(target)} already exists.")
        source.rename(target)
        return [self.relative(target)]

    def move(self, paths: list[str], destination: str, *, overwrite: bool = False) -> list[str]:
        return self._transfer(paths, destination, overwrite=overwrite, copy=False)

    def copy(self, paths: list[str], destination: str, *, overwrite: bool = False) -> list[str]:
        return self._transfer(paths, destination, overwrite=overwrite, copy=True)

    def delete(self, paths: list[str]) -> list[str]:
        done: list[str] = []
        for relative in paths:
            try:
                path = self._resolve_existing(relative)
                self._guard_operand(path)
                landed = self.relative(path)  # Before it goes: `relative` re-resolves.
                remove_path(path)
            except Exception as exc:
                raise PartialOperation(exc, done) from exc
            done.append(landed)
        return done

    def _transfer(
        self, paths: list[str], destination: str, *, overwrite: bool, copy: bool
    ) -> list[str]:
        target_dir = self._resolve_directory(destination)
        done: list[str] = []
        for relative in paths:
            try:
                done.append(self._transfer_one(relative, target_dir, overwrite=overwrite, copy=copy))
            except Exception as exc:
                raise PartialOperation(exc, done) from exc
        return done

    def _transfer_one(
        self, relative: str, target_dir: Path, *, overwrite: bool, copy: bool
    ) -> str:
        verb = "copy" if copy else "move"
        source = self._resolve_existing(relative)
        self._guard_operand(source)
        if source == target_dir or source in target_dir.parents:
            raise OperationRefused(
                f"Cannot {verb} {self.relative(source)} into itself or one of its own subfolders."
            )

        if copy and source.parent == target_dir:
            # This is **Duplicate**. The frozen client API has no call of its own for it,
            # so a copy whose destination is the source's own folder is the only way to
            # say it — which makes the name collision here the *request*, not a conflict.
            # `overwrite` is meaningless in this case (it would replace the original with
            # itself), so the name is stepped instead. A copy into a *different* folder
            # keeps its 409: that one is a real collision and the UI offers to overwrite.
            target = _duplicate_target(source)
            self._guard_operand(target)
            _perform(source, target, copy=True)
            return self.relative(target)

        target = self._child(target_dir, source.name)
        self._guard_operand(target)
        if source == target:
            return self.relative(source)  # A move to where it already is.
        if target.exists():
            if not overwrite:
                raise FileExistsError(f"{self.relative(target)} already exists.")
            remove_path(target)
        _perform(source, target, copy=copy)
        return self.relative(target)

    # ---- operation helpers ----

    def _resolve_existing(self, relative: str) -> Path:
        path = self.resolve(relative)
        if not path.exists():
            raise FileNotFoundError(f"Not found: {relative}")
        return path

    def _resolve_directory(self, relative: str) -> Path:
        path = self.resolve(relative)
        if not path.exists():
            raise FileNotFoundError(f"Not found: {relative}")
        if not path.is_dir():
            raise OperationRefused(f"Not a directory: {self.relative(path)}")
        return path

    def _child(self, directory: Path, name: str) -> Path:
        """``directory/name`` where ``name`` is a **basename**, checked as one.

        A separator here is the difference between renaming a file and writing it
        somewhere else entirely, so it is refused rather than normalised away.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("A name is required.")
        if cleaned in {".", ".."} or ".." in cleaned:
            raise ValueError(f"{name!r} is not a valid name.")
        if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
            raise ValueError(f"{name!r} is a name, not a path — it may not contain a separator.")
        target = (directory / cleaned).resolve()
        # Belt and braces: a drive-qualified name (`C:`) is neither a separator nor a
        # `..`, and pathlib will happily let it re-root the path.
        if not self.paths.is_inside_project(target) or target.parent != directory:
            raise PathOutsideProject(f"{name!r} does not name an entry of this folder.")
        return target

    def _guard_operand(self, path: Path) -> None:
        """The two directories no operation may create, rename, overwrite or delete.

        The project root is the user's project; ``voxarium_studio/`` is the Studio's own state,
        including the database it is at that moment writing to. Either one going missing
        is not an undoable file operation, it is the end of the session.
        """
        if path == self.paths.root:
            raise OperationRefused("The project root itself cannot be changed.")
        if path == self.paths.data:
            raise OperationRefused(
                f"{DATA_DIRNAME}/ holds the Studio's own state and cannot be changed here."
            )


def _perform(source: Path, target: Path, *, copy: bool) -> None:
    if not copy:
        shutil.move(str(source), str(target))
    elif source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _duplicate_target(source: Path) -> Path:
    """The name a duplicate takes: ``hero.png`` → ``hero copy.png`` → ``hero copy 2.png``.

    macOS's scheme rather than ``hero (2).png``, and the choice is worth stating because
    both are defensible: the first duplicate reads as what it is without a number, and a
    number only appears once there is something to distinguish. A directory takes the
    suffix at the **end** — it has no extension for one to sit in front of, and
    ``Assets.v2`` is a folder called `Assets.v2`, not one with a `.v2` extension.

    Stepping until the name is free is also what makes a multi-path duplicate safe: each
    copy is on disk before the next is named, so two duplicates of the same file in one
    request cannot land on each other.
    """
    directory = source.parent
    stem, extension = (source.name, "") if source.is_dir() else (source.stem, source.suffix)
    candidate = directory / f"{stem} {DUPLICATE_SUFFIX}{extension}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem} {DUPLICATE_SUFFIX} {counter}{extension}"
        counter += 1
    return candidate


def _read_head(path: Path, count: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(max(0, count))


def _decode(data: bytes, encoding: str) -> str:
    """Decode with the sniffed codec, dropping a BOM the codec does not eat itself.

    Only ``utf-8-sig`` strips its own mark; ``utf-16-le`` and friends name an endianness
    and leave the U+FEFF in the text, where it shows up as a stray glyph in the preview.
    """
    for bom, codec in BOM_CODECS:
        if codec == encoding and codec != "utf-8-sig" and data.startswith(bom):
            data = data[len(bom) :]
            break
    return data.decode(encoding, errors="replace")


def _mostly_printable(text: str) -> bool:
    if not text:
        return True
    printable = sum(1 for char in text if char.isprintable() or char in "\t\n\r\f\v")
    return printable / len(text) >= PRINTABLE_RATIO


def _same_entry(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return False


def remove_path(path: Path) -> None:
    """Delete a file, or a directory with everything in it — read-only files included.

    Public because the Documents tree deletes a folder the same way (``DocStore.delete_folder``).
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=_clear_read_only)
    else:
        path.unlink()


def _clear_read_only(func: Any, path: Any, error: BaseException) -> None:
    """Retry a failed removal once with the read-only bit off.

    Windows refuses to unlink a read-only file, and a `.tmp` left by a failed write or a
    checked-in asset marked read-only by a VCS is exactly what a user tries to delete.
    """
    import stat as stat_flags

    try:
        os.chmod(path, stat_flags.S_IWRITE)
        func(path)
    except OSError:
        raise error from None


def _has_children(directory: Path) -> bool:
    """Cheap check so the tree can show an expander without listing the whole subtree."""
    try:
        next(directory.iterdir())
    except (StopIteration, OSError):
        return False
    return True
