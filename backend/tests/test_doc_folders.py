"""Folders of the Documents tree: their look, renaming one, deleting one (block-editor spec §5.2).

The look is a small file inside the folder, and the tests are mostly about what that file is
allowed to become — refusals, keys nobody here wrote, a file that says nothing any more.

The rename is about links. A folder appears in a ``[[link]]`` only when the link spells out a
path, and the promise is not "the old name is replaced" but "every link means what it meant":
the tests assert the files byte for byte, because the one thing worse than a broken link is a
document the rename quietly reformatted.

The delete is about what is left: everything in the folder or nothing, never a folder that went
half way because something held a file in it.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core.context import AppContext
from voxarium_studio.explorer.service import PathOutsideProject
from voxarium_studio.teams import docs as docs_module
from voxarium_studio.teams.docs import FolderRename, FolderStyleUnreadable


@pytest.fixture
async def docs(ctx: AppContext) -> AppContext:
    await ctx.docs.write("features/dash.md", "# Dash ability\n\nSee [[test-plan]].\n")
    await ctx.docs.write("qa/test-plan.md", "# Test plan\n\nVerifies [[features/dash]] once.\n")
    await ctx.docs.write("notes/loose.md", "# Loose note\n")
    return ctx


@pytest.fixture
async def linked(ctx: AppContext) -> AppContext:
    """A corpus where every way of pointing into ``features`` is written down once."""
    await ctx.docs.write("features/dash.md", "# Dash ability\n")
    await ctx.docs.write("features/notes.md", "# Feature notes\n\nSee [[features/dash]].\n")
    await ctx.docs.write("qa/notes.md", "# QA notes\n")
    await ctx.docs.write(
        "qa/plan.md",
        "# Plan\n"
        "\n"
        "- [[features/dash]] and [[features/notes|the notes]]\n"
        "- [[ /Features/Dash.md ]] by its full path\n"
        "- [[dash]] and [[Dash ability]] by name\n"
        "- [[qa/notes]] stays, and so does [[features/ghost]]\n",
    )
    return ctx


async def _frames(operation: FolderRename) -> list[dict[str, object]]:
    return [frame async for frame in operation.frames()]


def _content(ctx: AppContext, path: str) -> str:
    return str(ctx.docs.read(path)["content"])


# ------------------------------------------------------------------ the look


async def test_a_folders_look_is_a_file_inside_the_folder(docs: AppContext) -> None:
    style = await docs.docs.set_folder_style("features", {"color": "green", "icon": "book-open"})

    assert style == {"path": "features", "color": "green", "icon": "book-open"}
    meta = docs.paths.docs / "features" / "vxs-folder.json"
    assert json.loads(meta.read_text(encoding="utf-8")) == {"color": "green", "icon": "book-open"}
    assert docs.docs.folders() == [style]

    published = [event for event in docs.bus.replay(0) if event.type == "team.doc.folder_changed"]
    assert [event.data for event in published] == [
        {"path": "features", "previous_path": None, "author": None}
    ]


async def test_a_look_is_patched_a_key_at_a_time_and_leaves_nothing_once_cleared(
    docs: AppContext,
) -> None:
    await docs.docs.set_folder_style("qa", {"color": "red", "icon": "bug"})

    changed = await docs.docs.set_folder_style("qa", {"color": "blue"})
    assert changed == {"path": "qa", "color": "blue", "icon": "bug"}, "the icon was not sent"

    await docs.docs.set_folder_style("qa", {"icon": None})
    assert docs.docs.folders() == [{"path": "qa", "color": "blue", "icon": None}]

    await docs.docs.set_folder_style("qa", {"color": None})
    assert not (docs.paths.docs / "qa" / "vxs-folder.json").exists(), "no `{}` left behind"
    assert docs.docs.folders() == []


async def test_a_look_keeps_the_keys_it_does_not_know(docs: AppContext) -> None:
    meta = docs.paths.docs / "notes" / "vxs-folder.json"
    meta.write_text('{"sort": "manual", "color": "pink"}', encoding="utf-8")

    await docs.docs.set_folder_style("notes", {"color": None})

    assert json.loads(meta.read_text(encoding="utf-8")) == {"sort": "manual"}, "still has content"
    assert docs.docs.folders() == [], "but nothing a folder can wear"


async def test_a_look_that_does_not_parse_is_refused_rather_than_replaced(docs: AppContext) -> None:
    meta = docs.paths.docs / "notes" / "vxs-folder.json"
    meta.write_text('{"color": "pink",', encoding="utf-8")

    with pytest.raises(FolderStyleUnreadable, match="does not parse"):
        await docs.docs.set_folder_style("notes", {"color": "green"})

    assert meta.read_text(encoding="utf-8") == '{"color": "pink",'
    assert docs.docs.folders() == [], "the listing draws that folder plainly instead of failing"


async def test_a_look_takes_only_the_text_colours_and_icon_shaped_names(docs: AppContext) -> None:
    for colour in ("#ff0000", "Green", "teal"):
        with pytest.raises(ValueError, match="not a folder colour"):
            await docs.docs.set_folder_style("features", {"color": colour})
    for icon in ("Book", "../x", "book open", "x" * 65):
        with pytest.raises(ValueError, match="not an icon name"):
            await docs.docs.set_folder_style("features", {"icon": icon})
    with pytest.raises(ValueError, match="no property"):
        await docs.docs.set_folder_style("features", {"font": "serif"})
    assert not (docs.paths.docs / "features" / "vxs-folder.json").exists()

    # A hand-written `grey` is the same colour, as it is in a document; a number is no icon.
    # Notepad's byte-order mark is tolerated too.
    (docs.paths.docs / "qa" / "vxs-folder.json").write_text(
        '\ufeff{"color": "grey", "icon": 7}', encoding="utf-8"
    )
    assert docs.docs.folders() == [{"path": "qa", "color": "gray", "icon": None}]


async def test_only_a_folder_of_the_tree_has_a_look(docs: AppContext) -> None:
    with pytest.raises(FileNotFoundError):
        await docs.docs.set_folder_style("ghost", {"color": "red"})
    with pytest.raises(FileNotFoundError, match="No folder"):
        await docs.docs.set_folder_style("features/dash.md", {"color": "red"})
    with pytest.raises(ValueError):
        await docs.docs.set_folder_style("/", {"color": "red"})
    with pytest.raises(PathOutsideProject):
        await docs.docs.set_folder_style("../..", {"color": "red"})


async def test_the_look_is_no_document(docs: AppContext) -> None:
    await docs.docs.set_folder_style("features", {"color": "green"})

    assert "features/vxs-folder.json" not in {entry.path for entry in docs.docs.list_docs()}
    assert docs.docs.search("green") == []


async def test_a_folder_left_holding_only_its_look_goes_with_its_last_document(
    docs: AppContext,
) -> None:
    await docs.docs.set_folder_style("notes", {"icon": "archive"})
    await docs.docs.delete("notes/loose.md")
    assert not (docs.paths.docs / "notes").exists()

    await docs.docs.set_folder_style("qa", {"icon": "bug"})
    (docs.paths.docs / "qa" / "shot.png").write_bytes(b"png")
    await docs.docs.move("qa/test-plan.md", "test-plan.md")
    assert (docs.paths.docs / "qa" / "vxs-folder.json").exists(), "a picture is still content"


# ------------------------------------------------------------------ renaming


async def test_renaming_a_folder_mends_every_link_that_spelled_it_out(linked: AppContext) -> None:
    frames = await _frames(await linked.docs.rename_folder("features", "mechanics"))

    assert not (linked.paths.docs / "features").exists()
    assert _content(linked, "mechanics/notes.md") == "# Feature notes\n\nSee [[mechanics/dash]].\n"
    assert _content(linked, "qa/plan.md") == (
        "# Plan\n"
        "\n"
        "- [[mechanics/dash]] and [[mechanics/notes|the notes]]\n"
        # The author's spelling of the rest, the spaces and the leading slash all survive.
        "- [[ /mechanics/Dash.md ]] by its full path\n"
        "- [[dash]] and [[Dash ability]] by name\n"
        "- [[qa/notes]] stays, and so does [[features/ghost]]\n"
    )
    assert {entry["path"] for entry in linked.docs.backlinks("mechanics/dash.md")} == {
        "mechanics/notes.md",
        "qa/plan.md",
    }

    assert frames == [
        {
            "type": "renamed",
            "path": "mechanics",
            "previous_path": "features",
            "moved": 2,
            "documents": [
                {
                    "path": "mechanics/notes.md",
                    "previous_path": "features/notes.md",
                    "title": "Feature notes",
                    "links": 1,
                },
                {"path": "qa/plan.md", "previous_path": None, "title": "Plan", "links": 3},
            ],
        },
        {
            "type": "document",
            "path": "mechanics/notes.md",
            "previous_path": "features/notes.md",
            "title": "Feature notes",
            "links": 1,
            "status": "updated",
            "error": None,
        },
        {
            "type": "document",
            "path": "qa/plan.md",
            "previous_path": None,
            "title": "Plan",
            "links": 3,
            "status": "updated",
            "error": None,
        },
        {
            "type": "done",
            "path": "mechanics",
            "previous_path": "features",
            "moved": 2,
            "updated": 2,
            "links": 4,
            "failed": 0,
        },
    ]


async def test_a_rename_tells_the_other_clients_once_and_names_each_rewritten_document(
    linked: AppContext,
) -> None:
    seq = linked.bus.current_seq
    await _frames(await linked.docs.rename_folder("features", "mechanics"))

    # The file watcher publishes on the same bus, so only the documents' own events are compared.
    published = [
        (event.type, event.data["path"])
        for event in linked.bus.replay(seq)
        if event.type.startswith("team.doc.")
    ]
    assert published == [
        ("team.doc.folder_changed", "mechanics"),
        ("team.doc.changed", "mechanics/notes.md"),
        ("team.doc.changed", "qa/plan.md"),
    ]


async def test_mending_a_link_changes_no_other_byte_of_the_file(ctx: AppContext) -> None:
    """CRLF, a byte-order mark, a byte that is not UTF-8 and no final newline all survive."""
    await ctx.docs.write("features/dash.md", "# Dash\n")
    plan = ctx.paths.docs / "plan.md"
    plan.write_bytes(
        b"\xef\xbb\xbf# Plan\r\n\r\nSee [[features/dash]]\r\nA stray \xff byte\r\nno final newline"
    )

    await _frames(await ctx.docs.rename_folder("features", "mechanics"))

    assert plan.read_bytes() == (
        b"\xef\xbb\xbf# Plan\r\n\r\nSee [[mechanics/dash]]\r\nA stray \xff byte\r\nno final newline"
    )


async def test_a_bare_name_the_rename_would_reorder_is_pinned_to_the_document_it_meant(
    ctx: AppContext,
) -> None:
    """Two ``notes.md``: the first in sorted order wins ``[[notes]]``, and a rename reorders."""
    await ctx.docs.write("mmm/notes.md", "# M notes\n")
    await ctx.docs.write("zzz/notes.md", "# Z notes\n")
    await ctx.docs.write("index.md", "# Index\n\nSee [[notes]].\n")
    assert ctx.docs.resolve_link("notes") == "mmm/notes.md"

    await _frames(await ctx.docs.rename_folder("zzz", "aaa"))

    assert ctx.docs.resolve_link("notes") == "aaa/notes.md", "the tie now falls the other way"
    assert _content(ctx, "index.md") == "# Index\n\nSee [[mmm/notes]].\n"


async def test_a_nested_folder_swaps_only_its_own_segment(ctx: AppContext) -> None:
    await ctx.docs.write("features/combat/dash.md", "# Dash\n")
    await ctx.docs.write("features/combat-notes.md", "# Notes\n\n[[features/combat/dash]]\n")

    frames = await _frames(await ctx.docs.rename_folder("features/combat", "fighting"))

    assert frames[0]["path"] == "features/fighting"
    assert _content(ctx, "features/combat-notes.md") == "# Notes\n\n[[features/fighting/dash]]\n"


async def test_a_rename_that_changes_only_case_goes_through(linked: AppContext) -> None:
    frames = await _frames(await linked.docs.rename_folder("features", "Features"))

    assert frames[-1]["type"] == "done"
    assert {entry.path for entry in linked.docs.list_docs()} >= {"Features/dash.md"}
    # The exact path is case-sensitive in resolution, so it had to change; the lowered one
    # with its suffix still resolved, so it was left alone.
    plan = _content(linked, "qa/plan.md")
    assert "- [[Features/dash]] and [[Features/notes|the notes]]\n" in plan
    assert "- [[ /Features/Dash.md ]] by its full path\n" in plan


async def test_a_folder_is_not_renamed_to_a_name_the_tree_cannot_hold(linked: AppContext) -> None:
    with pytest.raises(FileExistsError):
        await linked.docs.rename_folder("features", "qa")
    for name in ("", "  ", "a/b", "a\\b", "..", "a|b", "[x]", "what?", "con", "NUL.txt"):
        with pytest.raises(ValueError):
            await linked.docs.rename_folder("features", name)
    with pytest.raises(ValueError, match="end in a dot"):
        await linked.docs.rename_folder("features", "trailing.")
    with pytest.raises(ValueError, match="already called"):
        await linked.docs.rename_folder("features", "features")
    with pytest.raises(FileNotFoundError):
        await linked.docs.rename_folder("ghost", "x")
    with pytest.raises(PathOutsideProject):
        await linked.docs.rename_folder("../..", "x")

    assert (linked.paths.docs / "features" / "dash.md").exists(), "nothing moved"
    # Every refusal gave the lock back, or this would wait for ever.
    frames = await asyncio.wait_for(
        _frames(await linked.docs.rename_folder("features", "mechanics")), timeout=5
    )
    assert frames[-1]["type"] == "done"


async def test_a_document_that_cannot_be_written_fails_on_its_own(
    linked: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = docs_module._write_atomically

    def refuse_the_notes(path, data):  # type: ignore[no-untyped-def]
        if path.name == "notes.md":
            raise PermissionError(13, "Access is denied")
        written(path, data)

    monkeypatch.setattr(docs_module, "_write_atomically", refuse_the_notes)

    frames = await _frames(await linked.docs.rename_folder("features", "mechanics"))

    documents = {frame["path"]: frame for frame in frames if frame["type"] == "document"}
    assert documents["mechanics/notes.md"]["status"] == "failed"
    assert "read-only" in str(documents["mechanics/notes.md"]["error"])
    assert documents["qa/plan.md"]["status"] == "updated", "the next document is still mended"
    assert frames[-1] == {
        "type": "done",
        "path": "mechanics",
        "previous_path": "features",
        "moved": 2,
        "updated": 1,
        "links": 3,
        "failed": 1,
    }
    assert "[[features/dash]]" in _content(linked, "mechanics/notes.md"), "left as it was"


async def test_a_folder_the_system_will_not_let_go_of_is_reported_as_busy(
    linked: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def busy(self, target):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(docs_module, "RENAME_RETRY_SECONDS", 0)
    monkeypatch.setattr(type(linked.paths.docs), "rename", busy)

    with pytest.raises(docs_module.FolderInUse, match="has the folder or a file inside it open"):
        await linked.docs.rename_folder("features", "mechanics")
    assert attempts == docs_module.RENAME_ATTEMPTS


async def test_renames_wait_for_each_other(linked: AppContext) -> None:
    """The second plans against a tree the first has finished mending, not a half-written one."""
    first, second = await asyncio.gather(
        linked.docs.rename_folder("features", "mechanics"),
        linked.docs.rename_folder("qa", "tests"),
    )
    await _frames(first)
    await _frames(second)

    assert _content(linked, "tests/plan.md") == (
        "# Plan\n"
        "\n"
        "- [[mechanics/dash]] and [[mechanics/notes|the notes]]\n"
        "- [[ /mechanics/Dash.md ]] by its full path\n"
        "- [[dash]] and [[Dash ability]] by name\n"
        "- [[tests/notes]] stays, and so does [[features/ghost]]\n"
    )


# ------------------------------------------------------------------ deleting


def _hidden_leftovers(ctx: AppContext) -> list[str]:
    """What a delete moved aside and did not clean up — `.features.deleting-…` in the root."""
    return [path.name for path in ctx.paths.docs.iterdir() if ".deleting-" in path.name]


async def test_deleting_a_folder_takes_everything_in_it(docs: AppContext) -> None:
    await docs.docs.write("features/combat/hit.md", "# Hit\n")
    await docs.docs.set_folder_style("features", {"color": "green"})
    (docs.paths.docs / "features" / "shot.png").write_bytes(b"png")
    seq = docs.bus.current_seq

    deleted = await docs.docs.delete_folder("features")

    assert deleted == {
        "path": "features",
        "documents": ["features/combat/hit.md", "features/dash.md"],
    }
    assert not (docs.paths.docs / "features").exists(), "the picture and the look went too"
    assert _hidden_leftovers(docs) == []
    assert {entry.path for entry in docs.docs.list_docs()} == {"qa/test-plan.md", "notes/loose.md"}
    assert docs.docs.folders() == []
    # A link into it is left as written, and now means nothing — as after deleting a document.
    assert _content(docs, "qa/test-plan.md") == "# Test plan\n\nVerifies [[features/dash]] once.\n"
    assert docs.docs.resolve_link("features/dash") is None

    published = [
        (event.type, event.data)
        for event in docs.bus.replay(seq)
        if event.type.startswith("team.doc.")
    ]
    assert published == [
        (
            "team.doc.folder_changed",
            {"path": "features", "previous_path": None, "author": None, "deleted": True},
        )
    ], "once for the folder, not once per document"


async def test_a_folder_a_delete_empties_goes_with_it(ctx: AppContext) -> None:
    await ctx.docs.write("features/combat/hit.md", "# Hit\n")
    await ctx.docs.set_folder_style("features", {"icon": "swords"})
    await ctx.docs.write("qa/combat/plan.md", "# Plan\n")
    await ctx.docs.write("qa/notes.md", "# Notes\n")

    await ctx.docs.delete_folder("features/combat")
    await ctx.docs.delete_folder("qa/combat")

    assert not (ctx.paths.docs / "features").exists(), "left holding nothing but its look"
    assert (ctx.paths.docs / "qa" / "notes.md").exists()


async def test_only_a_folder_of_the_tree_is_deleted(docs: AppContext) -> None:
    with pytest.raises(FileNotFoundError):
        await docs.docs.delete_folder("ghost")
    with pytest.raises(FileNotFoundError, match="No folder"):
        await docs.docs.delete_folder("features/dash.md")
    with pytest.raises(ValueError):
        await docs.docs.delete_folder("/")
    with pytest.raises(PathOutsideProject):
        await docs.docs.delete_folder("../..")

    assert len(docs.docs.list_docs()) == 3


async def test_a_folder_the_system_will_not_let_go_of_is_not_deleted(
    docs: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(self, target):  # type: ignore[no-untyped-def]
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(docs_module, "RENAME_RETRY_SECONDS", 0)
    monkeypatch.setattr(type(docs.paths.docs), "rename", busy)

    with pytest.raises(docs_module.FolderInUse, match="Could not delete 'features'"):
        await docs.docs.delete_folder("features")
    assert (docs.paths.docs / "features" / "dash.md").exists()


@pytest.mark.skipif(os.name != "nt", reason="elsewhere a folder renames with a file open inside it")
async def test_a_folder_with_a_file_held_open_loses_nothing(
    docs: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real thing, not a patched rename: ``rmtree`` on this folder deletes what it reaches
    before the open file and then fails, which is what moving the folder aside first prevents."""
    monkeypatch.setattr(docs_module, "RENAME_RETRY_SECONDS", 0)
    await docs.docs.write("features/combat/hit.md", "# Hit\n")
    (docs.paths.docs / "features" / "shot.png").write_bytes(b"png")
    folder = docs.paths.docs / "features"
    before = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*"))

    with open(folder / "dash.md", "rb"):
        with pytest.raises(docs_module.FolderInUse):
            await docs.docs.delete_folder("features")

    assert sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")) == before
    assert _hidden_leftovers(docs) == []


async def test_what_a_removal_could_not_take_goes_back_under_its_name(
    docs: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    await docs.docs.write("features/combat/hit.md", "# Hit\n")

    def stops_part_way(path):  # type: ignore[no-untyped-def]
        (path / "dash.md").unlink()
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(docs_module, "remove_path", stops_part_way)
    seq = docs.bus.current_seq

    with pytest.raises(OSError, match="Access is denied. What is left is back where it was"):
        await docs.docs.delete_folder("features")

    listed = {entry.path for entry in docs.docs.list_docs()}
    assert "features/combat/hit.md" in listed
    assert "features/dash.md" not in listed
    assert _hidden_leftovers(docs) == []
    # Something did go, so the other clients are still told to look again.
    assert [
        event.data["path"] for event in docs.bus.replay(seq) if event.type == "team.doc.folder_changed"
    ] == ["features"]


async def test_a_delete_waits_for_a_rename_still_mending_its_links(linked: AppContext) -> None:
    """Renamed to `zzz`, the folder's documents sort after `qa/plan.md`, so a document outside it
    is mended first — and a delete that did not wait for the lock would take `zzz/notes.md` from
    under the mending, which would report it as a document it failed to write."""
    operation = await linked.docs.rename_folder("features", "zzz")
    deleting = asyncio.create_task(linked.docs.delete_folder("zzz"))

    frames = await _frames(operation)
    deleted = await deleting

    assert frames[-1]["failed"] == 0
    assert deleted["documents"] == ["zzz/dash.md", "zzz/notes.md"]
    assert _content(linked, "qa/plan.md").startswith("# Plan\n\n- [[zzz/dash]]")


# ------------------------------------------------------------------ over HTTP


def test_a_folders_look_over_http(client: TestClient) -> None:
    client.put("/api/docs/features/dash.md", json={"content": "# Dash\n"})
    client.put("/api/docs/folders.md", json={"content": "# Named like the route\n"})

    styled = client.post(
        "/api/docs/folders/style", json={"path": "features", "color": "green", "icon": "swords"}
    )
    assert styled.status_code == 200
    assert client.get("/api/docs/folders").json() == [
        {"path": "features", "color": "green", "icon": "swords"}
    ]
    # A key left out is left alone; a key sent as null goes back to the default.
    cleared = client.post("/api/docs/folders/style", json={"path": "features", "icon": None})
    assert cleared.json() == {"path": "features", "color": "green", "icon": None}
    # The literal route does not hide a document of that name, which is read by its suffix.
    assert client.get("/api/docs/folders.md").status_code == 200

    style = "/api/docs/folders/style"
    assert client.post(style, json={"path": "ghost", "color": "red"}).status_code == 404
    assert client.post(style, json={"path": "features", "color": "#fff"}).status_code == 400


def test_renaming_a_folder_over_http_streams_a_frame_per_document(client: TestClient) -> None:
    client.put("/api/docs/features/dash.md", json={"content": "# Dash\n"})
    client.put("/api/docs/qa/plan.md", json={"content": "# Plan\n\n[[features/dash]]\n"})

    rename = "/api/docs/folders/rename"
    response = client.post(rename, json={"path": "features", "name": "mechanics"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    frames = [json.loads(line) for line in response.text.splitlines()]
    assert [frame["type"] for frame in frames] == ["renamed", "document", "done"]
    assert frames[1]["status"] == "updated"
    assert client.get("/api/docs/qa/plan.md").json()["content"] == "# Plan\n\n[[mechanics/dash]]\n"

    assert client.post(rename, json={"path": "ghost", "name": "x"}).status_code == 404
    assert client.post(rename, json={"path": "mechanics", "name": "qa"}).status_code == 409
    assert client.post(rename, json={"path": "mechanics", "name": "a|b"}).status_code == 400
    assert client.post(rename, json={"path": "../..", "name": "x"}).status_code == 403


def test_deleting_a_folder_over_http_names_the_documents_that_went(client: TestClient) -> None:
    client.put("/api/docs/features/dash.md", json={"content": "# Dash\n"})
    client.put("/api/docs/features/combat/hit.md", json={"content": "# Hit\n"})
    client.put("/api/docs/qa/plan.md", json={"content": "# Plan\n"})

    delete = "/api/docs/folders/delete"
    response = client.post(delete, json={"path": "features"})

    assert response.status_code == 200
    assert response.json() == {
        "path": "features",
        "documents": ["features/combat/hit.md", "features/dash.md"],
    }
    assert [entry["path"] for entry in client.get("/api/docs").json()] == ["qa/plan.md"]

    assert client.post(delete, json={"path": "features"}).status_code == 404
    assert client.post(delete, json={"path": "../.."}).status_code == 403
    assert client.post(delete, json={"path": ""}).status_code == 400
