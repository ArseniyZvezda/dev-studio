"""Explorer file browsing, previewing and file operations (§6.2).

The Studio must not become an arbitrary-file-read — or arbitrary-file-*write* — endpoint,
particularly in server mode, so every client-supplied path is resolved and checked before
any I/O. That boundary is the first thing tested here and the thing every operation below
is re-tested against.
"""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core import reveal as reveal_module
from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.main import create_app
from voxarium_studio.explorer import service
from voxarium_studio.explorer.service import (
    ExplorerService,
    OperationRefused,
    PartialOperation,
    PathOutsideProject,
    classify,
    hex_dump,
    sniff_encoding,
)
from voxarium_studio.ws.bus import Subscription
from voxarium_studio.ws.events import EventType

RUSSIAN = "Привет, мир"


@pytest.fixture
def explorer(project: Path) -> ExplorerService:
    (project / "Assets").mkdir()
    (project / "Assets" / "notes.md").write_text("# hello\n", encoding="utf-8")
    (project / "Assets" / "model.vox").write_bytes(b"VOX \x96\x00\x00\x00")
    (project / "Assets" / "blob.bin").write_bytes(bytes(range(256)))
    return ExplorerService(ProjectPaths.for_project(project), AppConfig())


# --------------------------------------------------------------- classification


def test_classify_by_suffix(tmp_path: Path) -> None:
    assert classify(tmp_path / "a.md") == "text"
    assert classify(tmp_path / "a.png") == "image"
    assert classify(tmp_path / "a.vox") == "model"
    assert classify(tmp_path / "a.fbx") == "model"
    assert classify(tmp_path / "a.exe") == "binary"
    assert classify(tmp_path) == "directory"


def test_classify_never_reads_the_file(tmp_path: Path) -> None:
    """It runs once per entry of every listed directory; a read there is a per-file open.

    The consequence is that `kind` in a listing is a hint: an extensionless text file is
    listed as binary and *previewed* as text, which is what the next section pins.
    """
    missing = tmp_path / "LICENSE"
    assert classify(missing) == "binary", "no extension, and nothing read to find out"
    assert not missing.exists()


# ------------------------------------------------------------------ path safety


@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "../../etc/hosts", "C:/Windows/win.ini", "/etc/passwd", "Assets/../.."],
)
def test_paths_that_leave_the_project_are_refused(explorer: ExplorerService, escape: str) -> None:
    with pytest.raises(PathOutsideProject):
        explorer.resolve(escape)


def test_paths_inside_the_project_resolve(explorer: ExplorerService) -> None:
    assert explorer.resolve("Assets/notes.md").name == "notes.md"
    assert explorer.resolve("") == explorer.paths.root
    assert explorer.resolve("Assets/../Assets/notes.md").name == "notes.md"


# ---------------------------------------------------------------------- listing


def test_listing_hides_the_data_directory_by_default(explorer: ExplorerService) -> None:
    names = {entry.name for entry in explorer.list_dir("")}
    assert "Assets" in names
    assert ".voxarium_studio" not in names, "the Studio's own directory is off by default (§6.2)"

    explorer.config.explorer.show_data_dir = True
    assert ".voxarium_studio" in {entry.name for entry in explorer.list_dir("")}


def test_listing_honours_the_ignore_list(explorer: ExplorerService) -> None:
    (explorer.paths.root / "node_modules").mkdir()
    assert "node_modules" not in {entry.name for entry in explorer.list_dir("")}


def test_directories_sort_first(explorer: ExplorerService) -> None:
    (explorer.paths.root / "zzz").mkdir()
    (explorer.paths.root / "aaa.txt").write_text("x", encoding="utf-8")
    entries = explorer.list_dir("")
    assert entries[0].is_dir


def test_tree_reports_expandable_directories(explorer: ExplorerService) -> None:
    tree = explorer.tree("")
    assets = next(entry for entry in tree["entries"] if entry["name"] == "Assets")
    assert assets["is_dir"] and assets["has_children"]


# ---------------------------------------------------------------- text preview


def test_text_preview_returns_content(explorer: ExplorerService) -> None:
    preview = explorer.preview("Assets/notes.md")
    assert preview.kind == "text"
    assert preview.text == "# hello\n"
    assert preview.encoding == "utf-8"
    assert preview.truncated is False
    assert preview.hex is None


def test_text_preview_truncates_past_the_limit(
    explorer: ExplorerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_PREVIEW_BYTES", 8)
    (explorer.paths.root / "big.txt").write_text("x" * 100, encoding="utf-8")
    preview = explorer.preview("big.txt")
    assert preview.truncated is True
    assert len(preview.text or "") == 8


@pytest.mark.parametrize("name", ["LICENSE", "Makefile", "combat.lua", "player.as", "old.bak"])
def test_any_text_file_previews_whatever_its_extension(
    explorer: ExplorerService, name: str
) -> None:
    """`classify` says binary for all of these; content is what actually decides."""
    (explorer.paths.root / name).write_text("plain words\n", encoding="utf-8")
    preview = explorer.preview(name)
    assert preview.kind == "text"
    assert preview.text == "plain words\n"
    assert preview.encoding == "utf-8"


def test_a_crlf_file_previews_with_plain_newlines(explorer: ExplorerService) -> None:
    """The inverse of the Documents rule, and deliberate: nothing writes this back, so
    there is no round trip to protect — only a viewer that must not double-space."""
    (explorer.paths.root / "windows.notes").write_bytes(b"one\r\ntwo\r\n")
    preview = explorer.preview("windows.notes")
    assert preview.text == "one\ntwo\n"
    assert preview.size == 10, "the reported size is still the bytes on disk"


def test_an_empty_file_is_empty_text_not_binary(explorer: ExplorerService) -> None:
    (explorer.paths.root / "empty").write_bytes(b"")
    preview = explorer.preview("empty")
    assert preview.kind == "text"
    assert preview.text == ""
    assert preview.encoding == "utf-8"


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        ("utf-8-sig", "utf-8-sig"),
        ("utf-16-le", "utf-16-le"),
        ("utf-16-be", "utf-16-be"),
        ("utf-32-le", "utf-32-le"),
        ("utf-32-be", "utf-32-be"),
    ],
)
def test_a_bom_settles_the_encoding(
    explorer: ExplorerService, encoding: str, expected: str
) -> None:
    """A BOM is a declaration, and it is tested before the NUL rule — UTF-16 text is
    half NUL bytes, so any other order calls every UTF-16 document binary."""
    body = f"{RUSSIAN}\n"
    payload = body.encode(encoding)
    if encoding != "utf-8-sig":  # only utf-8-sig writes its own mark
        payload = _bom_for(encoding) + payload
    (explorer.paths.root / "bom.dump").write_bytes(payload)

    preview = explorer.preview("bom.dump")
    assert preview.kind == "text"
    assert preview.encoding == expected
    assert preview.text == body, "the mark itself must not survive into the preview"


def test_a_non_utf8_single_byte_file_falls_back(explorer: ExplorerService) -> None:
    (explorer.paths.root / "readme.txt").write_bytes(RUSSIAN.encode("cp1251"))
    preview = explorer.preview("readme.txt")
    assert preview.kind == "text"
    assert preview.encoding == "cp1251"
    assert preview.text == RUSSIAN


def test_utf8_wins_over_the_single_byte_fallback(explorer: ExplorerService) -> None:
    (explorer.paths.root / "readme.txt").write_text(RUSSIAN, encoding="utf-8")
    preview = explorer.preview("readme.txt")
    assert preview.encoding == "utf-8"
    assert preview.text == RUSSIAN


def test_a_truncated_multibyte_sequence_at_the_sample_edge_is_not_binary(
    explorer: ExplorerService,
) -> None:
    """The 8 KiB sniff can cut a character in half; that says where we stopped reading,
    not what the file is."""
    padding = "a" * (8192 - 1)
    (explorer.paths.root / "long.notes").write_text(padding + RUSSIAN, encoding="utf-8")
    preview = explorer.preview("long.notes")
    assert preview.kind == "text"
    assert (preview.text or "").endswith(RUSSIAN)


# -------------------------------------------------------------- binary preview


def test_random_bytes_come_back_binary_with_a_hex_dump(explorer: ExplorerService) -> None:
    preview = explorer.preview("Assets/blob.bin")
    assert preview.kind == "binary"
    assert preview.encoding is None
    assert preview.text is None
    assert (preview.hex or "").startswith("00000000  00 01 02 03 04 05 06 07  08 09 0a 0b 0c 0d 0e 0f")


def test_mojibake_is_not_offered_as_text(explorer: ExplorerService) -> None:
    """latin-1 decodes *anything*. Without the printability gate a file of random bytes
    with no NUL in it would come back as text full of control characters."""
    noisy = bytes(byte for byte in range(1, 256) if byte != 0x00) * 40
    (explorer.paths.root / "noise.dat").write_bytes(noisy)
    assert b"\x00" not in noisy
    preview = explorer.preview("noise.dat")
    assert preview.kind == "binary"
    assert preview.encoding is None
    assert preview.hex


def test_a_binary_file_with_a_text_suffix_is_reported_not_mangled(
    explorer: ExplorerService,
) -> None:
    (explorer.paths.root / "broken.md").write_bytes(b"# title\x00\x01\x02 not really markdown")
    preview = explorer.preview("broken.md")
    assert preview.kind == "binary"
    assert preview.encoding is None
    assert preview.hex


def test_the_hex_dump_is_capped_and_says_so(
    explorer: ExplorerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_PREVIEW_BYTES", 32)
    (explorer.paths.root / "big.bin").write_bytes(bytes(64) + b"\x01" * 64)
    preview = explorer.preview("big.bin")
    assert preview.kind == "binary"
    assert preview.truncated is True
    assert len((preview.hex or "").splitlines()) == 2, "32 bytes at 16 to a row"


def test_hex_dump_format() -> None:
    """`hexdump -C`, to the column: a short last row still lines its `|` up with the rest."""
    dump = hex_dump(bytes.fromhex("4d5a900003000000") + bytes.fromhex("04000000ffff0000") + b"hi")
    first, second = dump.splitlines()
    assert first == (
        "00000000  4d 5a 90 00 03 00 00 00  04 00 00 00 ff ff 00 00  |MZ..............|"
    )
    assert second == "00000010  68 69" + " " * 45 + "|hi|"
    assert first.index("|") == second.index("|") == 60


def test_hex_dump_of_nothing_is_nothing() -> None:
    assert hex_dump(b"") == ""


# ------------------------------------------------------- suffixes never sniffed


def test_a_png_is_never_sniffed(explorer: ExplorerService) -> None:
    """An image goes down the image branch whatever its bytes say — reading it to ask
    whether it is text would be a per-preview cost for an answer already known."""
    (explorer.paths.root / "icon.png").write_bytes(b"this is plainly ascii text, not a png")
    preview = explorer.preview("icon.png")
    assert preview.kind == "image"
    assert preview.encoding is None
    assert preview.hex is None
    assert (preview.data_url or "").startswith("data:image/png;base64,")


@pytest.mark.parametrize("name", ["game.exe", "core.dll", "core.pdb", "manual.pdf", "pack.zip"])
def test_known_binary_suffixes_skip_the_sniff(explorer: ExplorerService, name: str) -> None:
    (explorer.paths.root / name).write_bytes(b"ascii text that is nonetheless not a preview")
    preview = explorer.preview(name)
    assert preview.kind == "binary"
    assert preview.encoding is None
    assert preview.hex, "still inspectable — just as bytes"


def test_image_preview_inlines_small_files(explorer: ExplorerService) -> None:
    png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32
    (explorer.paths.root / "icon.png").write_bytes(png)
    preview = explorer.preview("icon.png")
    assert preview.kind == "image"
    assert (preview.data_url or "").startswith("data:image/png;base64,")


def test_model_files_get_metadata_and_a_pointer_to_plugins(explorer: ExplorerService) -> None:
    """3D preview is not built in; a plugin claims the type (§6.2)."""
    preview = explorer.preview("Assets/model.vox")
    assert preview.kind == "model"
    assert preview.text is None and preview.data_url is None, "the bytes are not inlined"
    assert preview.hex is None and preview.encoding is None
    assert "plugin" in (preview.message or "").lower()


def test_missing_file_raises(explorer: ExplorerService) -> None:
    with pytest.raises(FileNotFoundError):
        explorer.preview("nope.txt")


def test_sniff_encoding_directly() -> None:
    assert sniff_encoding(b"") == "utf-8"
    assert sniff_encoding(b"hello") == "utf-8"
    assert sniff_encoding(b"\x00\x01\x02") is None
    assert sniff_encoding(b"\xff\xfe" + "ok".encode("utf-16-le")) == "utf-16-le"
    assert sniff_encoding(b"\xff\xfe\x00\x00" + "ok".encode("utf-32-le")) == "utf-32-le"
    assert sniff_encoding(RUSSIAN.encode("cp1251")) == "cp1251"


def test_the_preview_payload_carries_the_new_fields(explorer: ExplorerService) -> None:
    payload = explorer.preview("Assets/notes.md").to_json()
    assert payload["encoding"] == "utf-8"
    assert payload["hex"] is None


# ------------------------------------------------------------- create / rename


def test_create_makes_a_file_and_a_directory(explorer: ExplorerService) -> None:
    assert explorer.create("Assets", "Textures", "directory") == ["Assets/Textures"]
    assert (explorer.paths.root / "Assets" / "Textures").is_dir()

    assert explorer.create("Assets/Textures", "grass.mat", "file") == [
        "Assets/Textures/grass.mat"
    ]
    assert (explorer.paths.root / "Assets" / "Textures" / "grass.mat").read_bytes() == b""


def test_create_at_the_project_root(explorer: ExplorerService) -> None:
    assert explorer.create("", "TODO", "file") == ["TODO"]


def test_create_refuses_an_existing_name(explorer: ExplorerService) -> None:
    with pytest.raises(FileExistsError):
        explorer.create("Assets", "notes.md", "file")


@pytest.mark.parametrize("name", ["sub/file.txt", "sub\\file.txt", "..", "../file.txt", "", "  "])
def test_create_refuses_a_name_that_is_really_a_path(
    explorer: ExplorerService, name: str
) -> None:
    with pytest.raises(ValueError):
        explorer.create("Assets", name, "file")


def test_create_refuses_a_missing_parent(explorer: ExplorerService) -> None:
    with pytest.raises(FileNotFoundError):
        explorer.create("Nowhere", "a.txt", "file")


def test_create_refuses_a_parent_outside_the_project(explorer: ExplorerService) -> None:
    with pytest.raises(PathOutsideProject):
        explorer.create("../elsewhere", "a.txt", "file")


def test_rename_moves_a_file_within_its_folder(explorer: ExplorerService) -> None:
    assert explorer.rename("Assets/notes.md", "design.md") == ["Assets/design.md"]
    assert (explorer.paths.root / "Assets" / "design.md").exists()
    assert not (explorer.paths.root / "Assets" / "notes.md").exists()


def test_rename_refuses_a_separator_in_the_new_name(explorer: ExplorerService) -> None:
    with pytest.raises(ValueError):
        explorer.rename("Assets/notes.md", "docs/design.md")
    with pytest.raises(ValueError):
        explorer.rename("Assets/notes.md", "..")
    assert (explorer.paths.root / "Assets" / "notes.md").exists()


def test_rename_refuses_an_existing_sibling(explorer: ExplorerService) -> None:
    (explorer.paths.root / "Assets" / "design.md").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        explorer.rename("Assets/notes.md", "design.md")


def test_rename_refuses_a_missing_file(explorer: ExplorerService) -> None:
    with pytest.raises(FileNotFoundError):
        explorer.rename("Assets/nope.md", "yes.md")


# --------------------------------------------------------------- move and copy


def test_move_relocates_files_and_folders(explorer: ExplorerService) -> None:
    (explorer.paths.root / "Build").mkdir()
    assert explorer.move(["Assets/notes.md"], "Build") == ["Build/notes.md"]
    assert (explorer.paths.root / "Build" / "notes.md").exists()

    assert explorer.move(["Build"], "Assets") == ["Assets/Build"]
    assert (explorer.paths.root / "Assets" / "Build" / "notes.md").exists()


def test_copy_leaves_the_source_and_recurses(explorer: ExplorerService) -> None:
    (explorer.paths.root / "Build").mkdir()
    assert explorer.copy(["Assets"], "Build") == ["Build/Assets"]
    assert (explorer.paths.root / "Assets" / "notes.md").exists()
    assert (explorer.paths.root / "Build" / "Assets" / "notes.md").read_text(
        encoding="utf-8"
    ) == "# hello\n"


def test_an_occupied_destination_needs_overwrite(explorer: ExplorerService) -> None:
    (explorer.paths.root / "Build").mkdir()
    (explorer.paths.root / "Build" / "notes.md").write_text("older\n", encoding="utf-8")

    with pytest.raises(PartialOperation) as refused:
        explorer.move(["Assets/notes.md"], "Build")
    assert isinstance(refused.value.cause, FileExistsError)
    assert (explorer.paths.root / "Assets" / "notes.md").exists(), "nothing moved"

    assert explorer.move(["Assets/notes.md"], "Build", overwrite=True) == ["Build/notes.md"]
    assert (explorer.paths.root / "Build" / "notes.md").read_text(encoding="utf-8") == "# hello\n"


def test_overwriting_the_folder_that_holds_the_source_is_refused(
    explorer: ExplorerService,
) -> None:
    """`Assets/Inner/Assets` sent to the root collides with the `Assets` it lives in.

    Overwrite removes what is at the destination first — here that is the folder holding
    the source, so the source would be gone before anything was moved or copied.
    """
    nested = explorer.paths.root / "Assets" / "Inner" / "Assets"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("keep", encoding="utf-8")

    for operation in (explorer.move, explorer.copy):
        with pytest.raises(PartialOperation) as refused:
            operation(["Assets/Inner/Assets"], "", overwrite=True)
        assert isinstance(refused.value.cause, OperationRefused)

    assert (nested / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (explorer.paths.root / "Assets" / "notes.md").is_file(), "nothing was removed"


@pytest.mark.parametrize("destination", ["Assets", "Assets/Textures/High"])
def test_a_directory_cannot_be_moved_into_itself(
    explorer: ExplorerService, destination: str
) -> None:
    (explorer.paths.root / "Assets" / "Textures" / "High").mkdir(parents=True)
    with pytest.raises(PartialOperation) as refused:
        explorer.move(["Assets"], destination)
    assert isinstance(refused.value.cause, OperationRefused)
    assert (explorer.paths.root / "Assets" / "notes.md").exists()


def test_copy_into_a_descendant_is_refused_too(explorer: ExplorerService) -> None:
    """Same rule, sharper consequence: `copytree` into its own subtree does not stop."""
    (explorer.paths.root / "Assets" / "Textures").mkdir()
    with pytest.raises(PartialOperation) as refused:
        explorer.copy(["Assets"], "Assets/Textures")
    assert isinstance(refused.value.cause, OperationRefused)


def test_a_copy_into_the_source_own_folder_duplicates(explorer: ExplorerService) -> None:
    """"Duplicate" has no call of its own in the frozen client API, so it arrives as a
    copy whose destination is the source's own folder. The collision *is* the request."""
    assert explorer.copy(["Assets/notes.md"], "Assets") == ["Assets/notes copy.md"]
    assert (explorer.paths.root / "Assets" / "notes.md").exists(), "the original stays"
    assert (explorer.paths.root / "Assets" / "notes copy.md").read_text(
        encoding="utf-8"
    ) == "# hello\n"


def test_duplicating_twice_steps_the_name(explorer: ExplorerService) -> None:
    assert explorer.copy(["Assets/notes.md"], "Assets") == ["Assets/notes copy.md"]
    assert explorer.copy(["Assets/notes.md"], "Assets") == ["Assets/notes copy 2.md"]
    assert explorer.copy(["Assets/notes.md"], "Assets") == ["Assets/notes copy 3.md"]
    assert explorer.copy(["Assets/notes copy.md"], "Assets") == ["Assets/notes copy copy.md"]


def test_duplicating_a_directory_suffixes_the_whole_name(explorer: ExplorerService) -> None:
    """A folder has no extension for the suffix to sit in front of — `Assets.v2` is a
    folder called `Assets.v2`, not one with a `.v2` extension."""
    (explorer.paths.root / "Assets.v2").mkdir()
    (explorer.paths.root / "Assets.v2" / "inner.txt").write_text("in", encoding="utf-8")

    assert explorer.copy(["Assets"], "") == ["Assets copy"]
    assert (explorer.paths.root / "Assets copy" / "notes.md").exists(), "recursive"
    assert explorer.copy(["Assets.v2"], "") == ["Assets.v2 copy"]
    assert (explorer.paths.root / "Assets.v2 copy" / "inner.txt").exists()


def test_duplicating_several_paths_at_once(explorer: ExplorerService) -> None:
    """Each is suffixed independently, and none may land on a name the same request
    just created — which the step loop gets for free, since each copy is on disk before
    the next is named."""
    assert explorer.copy(
        ["Assets/notes.md", "Assets/model.vox", "Assets/notes.md"], "Assets"
    ) == ["Assets/notes copy.md", "Assets/model copy.vox", "Assets/notes copy 2.md"]
    assert (explorer.paths.root / "Assets" / "notes copy.md").exists()
    assert (explorer.paths.root / "Assets" / "notes copy 2.md").exists()
    assert (explorer.paths.root / "Assets" / "model copy.vox").exists()


def test_a_copy_to_another_folder_still_conflicts(explorer: ExplorerService) -> None:
    """The rule is narrow on purpose: only the source's *own* parent means "duplicate".
    Anywhere else a taken name is a real conflict, and the UI offers overwrite for it."""
    (explorer.paths.root / "Build").mkdir()
    (explorer.paths.root / "Build" / "notes.md").write_text("older\n", encoding="utf-8")

    with pytest.raises(PartialOperation) as refused:
        explorer.copy(["Assets/notes.md"], "Build")
    assert isinstance(refused.value.cause, FileExistsError)
    assert not (explorer.paths.root / "Build" / "notes copy.md").exists(), "no suffixing here"

    assert explorer.copy(["Assets/notes.md"], "Build", overwrite=True) == ["Build/notes.md"]
    assert (explorer.paths.root / "Build" / "notes.md").read_text(encoding="utf-8") == "# hello\n"


def test_duplicate_is_a_copy_rule_only(explorer: ExplorerService) -> None:
    """Moving something onto itself is still the no-op it was — nothing is duplicated."""
    assert explorer.move(["Assets/notes.md"], "Assets") == ["Assets/notes.md"]
    assert not (explorer.paths.root / "Assets" / "notes copy.md").exists()


def test_duplicate_over_http(client: TestClient, project: Path) -> None:
    (project / "Assets").mkdir()
    (project / "Assets" / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    first = client.post(
        "/api/explorer/copy", json={"paths": ["Assets/hero.png"], "destination": "Assets"}
    )
    assert first.status_code == 200
    assert first.json() == {"paths": ["Assets/hero copy.png"]}

    second = client.post(
        "/api/explorer/copy", json={"paths": ["Assets/hero.png"], "destination": "Assets"}
    )
    assert second.json() == {"paths": ["Assets/hero copy 2.png"]}


def test_moving_a_file_where_it_already_is_is_a_no_op(explorer: ExplorerService) -> None:
    assert explorer.move(["Assets/notes.md"], "Assets") == ["Assets/notes.md"]
    assert (explorer.paths.root / "Assets" / "notes.md").exists()


def test_a_missing_destination_is_not_created(explorer: ExplorerService) -> None:
    with pytest.raises(FileNotFoundError):
        explorer.move(["Assets/notes.md"], "Nowhere")


def test_a_file_is_not_a_destination(explorer: ExplorerService) -> None:
    with pytest.raises(OperationRefused):
        explorer.move(["Assets/model.vox"], "Assets/notes.md")


# ---------------------------------------------------------------------- delete


def test_delete_is_recursive(explorer: ExplorerService) -> None:
    assert explorer.delete(["Assets"]) == ["Assets"]
    assert not (explorer.paths.root / "Assets").exists()


def test_delete_reports_every_path_it_removed(explorer: ExplorerService) -> None:
    (explorer.paths.root / "a.txt").write_text("a", encoding="utf-8")
    assert explorer.delete(["a.txt", "Assets/notes.md"]) == ["a.txt", "Assets/notes.md"]


# ------------------------------------------------------------- what is refused


@pytest.mark.parametrize("protected", ["", ".", ".voxarium_studio", ".voxarium_studio/"])
def test_the_root_and_the_data_directory_are_untouchable(
    explorer: ExplorerService, protected: str
) -> None:
    """Neither is an undoable file operation: one is the user's project, the other is
    the database the Studio is writing to right now."""
    with pytest.raises(PartialOperation) as deleted:
        explorer.delete([protected])
    assert isinstance(deleted.value.cause, OperationRefused)

    with pytest.raises(OperationRefused):
        explorer.rename(protected, "elsewhere")

    (explorer.paths.root / "Build").mkdir()
    with pytest.raises(PartialOperation) as moved:
        explorer.move([protected], "Build")
    assert isinstance(moved.value.cause, OperationRefused)


def test_the_data_directory_cannot_be_overwritten_by_a_move(explorer: ExplorerService) -> None:
    """The target of the move is `<root>/.voxarium_studio` — guarding only the *sources* would
    let `overwrite: true` delete the Studio's state on the way past."""
    (explorer.paths.root / "Assets" / ".voxarium_studio").mkdir()
    with pytest.raises(PartialOperation) as refused:
        explorer.move(["Assets/.voxarium_studio"], "", overwrite=True)
    assert isinstance(refused.value.cause, OperationRefused)
    assert explorer.paths.data.is_dir()


@pytest.mark.parametrize("escape", ["../outside.txt", "/etc/passwd", "C:/Windows/win.ini"])
def test_operations_refuse_paths_outside_the_project(
    explorer: ExplorerService, escape: str
) -> None:
    with pytest.raises(PartialOperation) as deleted:
        explorer.delete([escape])
    assert isinstance(deleted.value.cause, PathOutsideProject)

    with pytest.raises(PathOutsideProject):
        explorer.rename(escape, "mine.txt")

    with pytest.raises(PathOutsideProject):
        explorer.move(["Assets/notes.md"], escape)


def test_a_half_finished_operation_reports_what_landed(explorer: ExplorerService) -> None:
    """The client's tree and its selection both have to describe what is now on disk."""
    (explorer.paths.root / "first.txt").write_text("1", encoding="utf-8")
    (explorer.paths.root / "third.txt").write_text("3", encoding="utf-8")

    with pytest.raises(PartialOperation) as stopped:
        explorer.delete(["first.txt", "missing.txt", "third.txt"])

    assert stopped.value.done == ["first.txt"]
    assert isinstance(stopped.value.cause, FileNotFoundError)
    assert not (explorer.paths.root / "first.txt").exists()
    assert (explorer.paths.root / "third.txt").exists(), "the operation stopped, it did not skip"


# ------------------------------------------------------------------- the wire


def test_preview_over_http_carries_encoding_and_hex(client: TestClient, project: Path) -> None:
    (project / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (project / "blob.bin").write_bytes(bytes(range(64)))

    text = client.get("/api/explorer/file", params={"path": "LICENSE"}).json()
    assert text["kind"] == "text" and text["encoding"] == "utf-8" and text["hex"] is None

    binary = client.get("/api/explorer/file", params={"path": "blob.bin"}).json()
    assert binary["kind"] == "binary" and binary["encoding"] is None
    assert binary["hex"].startswith("00000000  00 01 02 03")


def test_the_five_operations_over_http(client: TestClient, project: Path) -> None:
    created = client.post(
        "/api/explorer/create", json={"parent": "", "name": "Assets", "kind": "directory"}
    )
    assert created.status_code == 200 and created.json() == {"paths": ["Assets"]}

    created_file = client.post(
        "/api/explorer/create", json={"parent": "Assets", "name": "notes.md", "kind": "file"}
    )
    assert created_file.json() == {"paths": ["Assets/notes.md"]}

    renamed = client.post(
        "/api/explorer/rename", json={"path": "Assets/notes.md", "name": "design.md"}
    )
    assert renamed.json() == {"paths": ["Assets/design.md"]}

    client.post("/api/explorer/create", json={"parent": "", "name": "Build", "kind": "directory"})
    copied = client.post(
        "/api/explorer/copy", json={"paths": ["Assets/design.md"], "destination": "Build"}
    )
    assert copied.json() == {"paths": ["Build/design.md"]}

    moved = client.post(
        "/api/explorer/move",
        json={"paths": ["Assets/design.md"], "destination": "Build", "overwrite": True},
    )
    assert moved.json() == {"paths": ["Build/design.md"]}

    deleted = client.post("/api/explorer/delete", json={"paths": ["Build", "Assets"]})
    assert deleted.json() == {"paths": ["Build", "Assets"]}
    assert not (project / "Build").exists() and not (project / "Assets").exists()


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        # Outside the project — 403, and the body carries the path so `httpx` cannot
        # normalise the `..` away the way it does in a URL.
        ("create", {"parent": "../elsewhere", "name": "a.txt", "kind": "file"}, 403),
        ("rename", {"path": "../outside.txt", "name": "mine.txt"}, 403),
        ("move", {"paths": ["notes.md"], "destination": "/etc"}, 403),
        ("delete", {"paths": ["../outside.txt"]}, 403),
        # A name that is really a path — 400.
        ("create", {"parent": "", "name": "sub/a.txt", "kind": "file"}, 400),
        ("rename", {"path": "notes.md", "name": "sub/a.txt"}, 400),
        ("rename", {"path": "notes.md", "name": ".."}, 400),
        # The two directories nothing may touch — 409.
        ("delete", {"paths": [""]}, 409),
        ("delete", {"paths": [".voxarium_studio"]}, 409),
        ("rename", {"path": ".voxarium_studio", "name": "elsewhere"}, 409),
        # Missing sources — 404.
        ("rename", {"path": "nope.md", "name": "yes.md"}, 404),
        ("delete", {"paths": ["nope.md"]}, 404),
        ("move", {"paths": ["nope.md"], "destination": ""}, 404),
    ],
)
def test_refusals_over_http(
    client: TestClient, project: Path, path: str, body: dict, status: int
) -> None:
    (project / "notes.md").write_text("# hello\n", encoding="utf-8")
    response = client.post(f"/api/explorer/{path}", json=body)
    assert response.status_code == status, response.text
    assert isinstance(response.json()["detail"], str)


def test_an_occupied_destination_is_409_until_overwrite(
    client: TestClient, project: Path
) -> None:
    (project / "Build").mkdir()
    (project / "notes.md").write_text("new\n", encoding="utf-8")
    (project / "Build" / "notes.md").write_text("old\n", encoding="utf-8")

    refused = client.post(
        "/api/explorer/move", json={"paths": ["notes.md"], "destination": "Build"}
    )
    assert refused.status_code == 409

    allowed = client.post(
        "/api/explorer/move",
        json={"paths": ["notes.md"], "destination": "Build", "overwrite": True},
    )
    assert allowed.status_code == 200
    assert (project / "Build" / "notes.md").read_text(encoding="utf-8") == "new\n"


def test_a_directory_into_its_own_descendant_is_409(client: TestClient, project: Path) -> None:
    (project / "Assets" / "Textures").mkdir(parents=True)
    response = client.post(
        "/api/explorer/move", json={"paths": ["Assets"], "destination": "Assets/Textures"}
    )
    assert response.status_code == 409


def test_a_partial_failure_names_the_paths_that_landed(
    client: TestClient, project: Path
) -> None:
    (project / "first.txt").write_text("1", encoding="utf-8")
    response = client.post(
        "/api/explorer/delete", json={"paths": ["first.txt", "missing.txt"]}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["paths"] == ["first.txt"]
    assert not (project / "first.txt").exists()


# ---------------------------------------------------------------- the watcher


async def test_a_rename_and_a_delete_reach_the_watcher(ctx: AppContext) -> None:
    """No operation publishes `explorer.file_changed` itself, so this is the only path.

    One producer for that event is the point: whatever the UI does with a rename it did
    itself, it also does with a rename an agent or a pipeline node made.
    """
    root = ctx.explorer.paths.root
    subscription = ctx.bus.subscribe()
    try:
        (root / "watched.txt").write_text("x", encoding="utf-8")
        await _await_change(subscription, "watched.txt")

        ctx.explorer.rename("watched.txt", "renamed.txt")
        assert await _await_change(subscription, "renamed.txt") == "added"

        ctx.explorer.delete(["renamed.txt"])
        assert await _await_change(subscription, "renamed.txt") == "deleted"

        # A deleted *directory* is reported with `is_dir: false` — the watcher stats a
        # path that no longer exists to answer that field, and there is nothing left to
        # stat. The client cannot use it to decide what to prune, so it refetches.
        ctx.explorer.create("", "gone", "directory")
        await _await_change(subscription, "gone")
        ctx.explorer.delete(["gone"])
        change, payload = await _await_change(subscription, "gone", with_payload=True)
        assert change == "deleted"
        assert payload["is_dir"] is False
    finally:
        subscription.close()


async def _await_change(
    subscription: Subscription,
    path: str,
    timeout: float = 20.0,
    *,
    with_payload: bool = False,
) -> Any:
    """Wait for the watcher to report `path`, and answer with the change it reported."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: list[str] = []
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"the watcher never reported {path!r}; it saw {seen}")
        try:
            event = await asyncio.wait_for(subscription.get(), timeout=remaining)
        except TimeoutError as exc:
            raise AssertionError(f"the watcher never reported {path!r}; it saw {seen}") from exc
        if event.type != EventType.EXPLORER_FILE_CHANGED.value:
            continue
        for change in event.data["changes"]:
            seen.append(f"{change['change']} {change['path']}")
            if change["path"] == path:
                name = str(change["change"])
                return (name, change) if with_payload else name


def _bom_for(encoding: str) -> bytes:
    import codecs

    return {
        "utf-16-le": codecs.BOM_UTF16_LE,
        "utf-16-be": codecs.BOM_UTF16_BE,
        "utf-32-le": codecs.BOM_UTF32_LE,
        "utf-32-be": codecs.BOM_UTF32_BE,
    }[encoding]


# --------------------------------------------------------------------------- reveal
#
# "Open in system explorer" spawns a program on the machine running the backend, so what
# is tested here is the boundary rather than the window: which argument vector is built,
# and every case in which no process should be started at all.


def test_reveal_selects_a_file_inside_its_folder_on_windows() -> None:
    command, opened, selected = reveal_module._command(
        PureWindowsPath(r"C:\Games\Hero\Assets\hero.png"), is_dir=False, platform="windows"
    )

    # One token: `explorer.exe` ignores `/select,` followed by a space and silently opens
    # Documents instead, which looks like the feature choosing the wrong folder.
    assert command == ["explorer.exe", r"/select,C:\Games\Hero\Assets\hero.png"]
    assert opened == PureWindowsPath(r"C:\Games\Hero\Assets")
    assert selected is True


def test_reveal_opens_a_directory_as_itself_on_windows() -> None:
    command, opened, selected = reveal_module._command(
        PureWindowsPath(r"C:\Games\Hero\Assets"), is_dir=True, platform="windows"
    )

    assert command == ["explorer.exe", r"C:\Games\Hero\Assets"]
    assert opened == PureWindowsPath(r"C:\Games\Hero\Assets")
    assert selected is False


def test_reveal_falls_back_to_the_folder_where_selection_is_unavailable() -> None:
    """`xdg-open` takes a location and not a selection, and the payload must say so."""
    command, opened, selected = reveal_module._command(
        PurePosixPath("/games/hero/hero.png"), is_dir=False, platform="freedesktop"
    )

    assert command == ["xdg-open", "/games/hero"]
    assert opened == PurePosixPath("/games/hero")
    assert selected is False


def test_reveal_uses_open_dash_r_on_macos() -> None:
    command, _, selected = reveal_module._command(
        PurePosixPath("/games/hero/hero.png"), is_dir=False, platform="macos"
    )

    assert command == ["open", "-R", "/games/hero/hero.png"]
    assert selected is True


def test_reveal_passes_the_resolved_path_and_never_a_shell_string(
    client: TestClient, project: Path, monkeypatch
) -> None:
    (project / "Assets").mkdir(exist_ok=True)
    (project / "Assets" / "a & b.png").write_bytes(b"\x89PNG")
    seen: list[Path] = []

    async def fake_reveal(target: Path) -> reveal_module.RevealResult:
        seen.append(target)
        return reveal_module.RevealResult(path=target.parent, selected=True)

    monkeypatch.setattr("voxarium_studio.api.explorer.reveal", fake_reveal)

    response = client.post("/api/explorer/reveal", json={"path": "Assets/a & b.png"})

    assert response.status_code == 200
    assert response.json()["selected"] is True
    # An absolute path on this machine, not the project-relative one the client sent.
    assert seen == [project / "Assets" / "a & b.png"]


def test_reveal_accepts_the_project_root(client: TestClient, project: Path, monkeypatch) -> None:
    """"Show me where this project lives" is the one case with no entry to click."""
    seen: list[Path] = []

    async def fake_reveal(target: Path) -> reveal_module.RevealResult:
        seen.append(target)
        return reveal_module.RevealResult(path=target, selected=False)

    monkeypatch.setattr("voxarium_studio.api.explorer.reveal", fake_reveal)

    response = client.post("/api/explorer/reveal", json={"path": ""})

    assert response.status_code == 200
    assert seen == [project.resolve()]


@pytest.mark.parametrize("path", ["%2e%2e/outside.txt", "../outside.txt", "C:/Windows"])
def test_reveal_refuses_a_path_outside_the_project(
    client: TestClient, path: str, monkeypatch
) -> None:
    async def fail(target: Path) -> reveal_module.RevealResult:  # pragma: no cover
        raise AssertionError(f"a file manager was started for {target}")

    monkeypatch.setattr("voxarium_studio.api.explorer.reveal", fail)

    response = client.post("/api/explorer/reveal", json={"path": path})

    assert response.status_code in (403, 404)


def test_reveal_reports_a_missing_entry_rather_than_opening_its_parent(
    client: TestClient, monkeypatch
) -> None:
    async def fail(target: Path) -> reveal_module.RevealResult:  # pragma: no cover
        raise AssertionError(f"a file manager was started for {target}")

    monkeypatch.setattr("voxarium_studio.api.explorer.reveal", fail)

    response = client.post("/api/explorer/reveal", json={"path": "Assets/gone.png"})

    assert response.status_code == 404


def test_reveal_declines_when_the_studio_is_not_on_this_machine(
    project: Path, monkeypatch
) -> None:
    """The window would open on the server's desktop, where the caller cannot see it."""

    async def fail(target: Path) -> reveal_module.RevealResult:  # pragma: no cover
        raise AssertionError(f"a file manager was started for {target}")

    monkeypatch.setattr("voxarium_studio.api.explorer.reveal", fail)
    app = create_app(str(project), auth=AuthSettings(token="t0ken", host="0.0.0.0"))

    with TestClient(app, base_url="http://127.0.0.1") as remote:
        response = remote.post(
            "/api/explorer/reveal",
            json={"path": ""},
            headers={"Authorization": "Bearer t0ken"},
        )

    assert response.status_code == 409
    assert "not running on your machine" in response.json()["detail"]


async def test_reveal_turns_a_missing_file_manager_into_something_readable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(reveal_module, "file_manager_available", lambda: (False, "xdg-open was not found on PATH."))

    with pytest.raises(reveal_module.RevealError, match="xdg-open"):
        await reveal_module.reveal(Path("/games/hero"))


def test_the_window_search_quotes_the_folder_it_looks_for() -> None:
    """The path reaches PowerShell as a literal, apostrophes and all.

    Asset trees carry apostrophes and ampersands routinely; a path spliced in raw would
    end the string and turn the rest of the folder name into code.
    """
    script = reveal_module._find_script(PureWindowsPath(r"D:\Games\Bob's Stuff & Co"))

    assert r"$target = 'D:\Games\Bob''s Stuff & Co'" in script
    # It waits for the window rather than looking once: Explorer takes a moment to appear.
    assert "while ((Get-Date) -lt $deadline)" in script
    assert "$window.Document.Folder.Self.Path -eq $target" in script


async def test_the_window_search_gives_up_without_powershell(monkeypatch) -> None:
    monkeypatch.setattr(reveal_module, "_powershell", lambda: None)

    assert await reveal_module._find_window(PureWindowsPath(r"D:\Games")) is None


@pytest.mark.parametrize("printed", [b"", b"   ", b"0", b"not-a-handle"])
async def test_the_window_search_rejects_an_unusable_handle(monkeypatch, printed: bytes) -> None:
    """A timed-out search prints nothing, and 0 is not a window — neither may be raised."""
    monkeypatch.setattr(reveal_module, "_powershell", lambda: "powershell")

    class FakeProcess:
        async def communicate(self) -> tuple[bytes, bytes]:
            return printed, b""

    async def fake_exec(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(reveal_module.asyncio, "create_subprocess_exec", fake_exec)

    assert await reveal_module._find_window(PureWindowsPath(r"D:\Games")) is None


async def test_only_windows_goes_looking_for_a_window_to_raise(monkeypatch, tmp_path: Path) -> None:
    """`open` activates Finder itself and a freedesktop file manager raises its own window.

    Doing it anyway would mean a PowerShell subprocess on a machine that has no PowerShell.
    """
    monkeypatch.setattr(reveal_module, "current_platform", lambda: "macos")
    monkeypatch.setattr(reveal_module, "file_manager_available", lambda: (True, ""))

    async def fake_exec(*_args: object, **_kwargs: object):
        class Immediate:
            returncode = 0
            stderr = None

            async def wait(self) -> int:
                return 0

        return Immediate()

    async def unexpected(folder: object) -> None:  # pragma: no cover
        raise AssertionError(f"went looking for a window on macOS: {folder}")

    monkeypatch.setattr(reveal_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(reveal_module, "_focus_window", unexpected)

    result = await reveal_module.reveal(tmp_path)

    assert result.path == tmp_path
