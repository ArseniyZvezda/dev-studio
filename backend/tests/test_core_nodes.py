"""The built-in string and path nodes (§8.1).

Driven the way the engine drives them — the class the registry actually holds — rather than
by importing `plugins_builtin/core/plugin.py`, so a node that is written but never registered
fails here instead of in the palette.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voxarium_studio.plugins.api import NodeContext
from voxarium_studio.plugins.registry import Registry


def make_ctx(project: Path, params: dict, sink: list[str] | None = None) -> NodeContext:
    async def log(message: str, stream: str = "stdout") -> None:
        (sink if sink is not None else []).append(message)

    return NodeContext(
        inputs={},
        params=params,
        project_root=project,
        assets_dir=project / ".voxarium_studio" / "assets",
        work_dir=project / ".voxarium_studio" / "work",
        settings={},
        log_fn=log,
    )


async def run_node(
    registry: Registry, node_type: str, project: Path, params: dict, sink=None
) -> dict:
    node_class = registry.node_class(node_type)
    assert node_class is not None, f"{node_type} must register an implementation"
    return await node_class().run(make_ctx(project, params, sink))


# ------------------------------------------------------------------ core.append


async def test_append_joins_two_strings(project: Path, registry: Registry) -> None:
    outputs = await run_node(registry, "core.append", project, {"a": "hero", "b": ".png"})
    assert outputs["text"] == "hero.png"


async def test_append_puts_the_separator_only_between_two_values(
    project: Path, registry: Registry
) -> None:
    """A separator next to an empty side is a trailing comma, which is never the intent."""
    both = await run_node(registry, "core.append", project, {"a": "a", "b": "b", "separator": ", "})
    assert both["text"] == "a, b"

    for params in ({"a": "a", "b": "", "separator": ", "}, {"a": "", "b": "b", "separator": ", "}):
        one = await run_node(registry, "core.append", project, params)
        assert one["text"] in ("a", "b")
        assert ", " not in one["text"]


async def test_append_treats_a_missing_value_as_empty_rather_than_none(
    project: Path, registry: Registry
) -> None:
    """An unconnected optional port arrives as None; `"None"` in the output is a bug."""
    outputs = await run_node(registry, "core.append", project, {"a": None, "b": "tail"})
    assert outputs["text"] == "tail"


# ------------------------------------------------------------------ core.append_path


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("Assets", "Models", "Assets/Models"),
        # The whole point of the node: however many slashes the two pieces carried, one.
        ("Assets/", "/Models", "Assets/Models"),
        ("Assets//", "//Models", "Assets/Models"),
        ("Assets/Generated//Meshes", "hero.glb", "Assets/Generated/Meshes/hero.glb"),
        # Both ends are the caller's statement: "from the root", and "this is a directory".
        ("/Assets", "Models", "/Assets/Models"),
        ("Assets", "Models/", "Assets/Models/"),
        ("", "Models", "Models"),
        ("Assets", "", "Assets"),
        ("", "", ""),
        # Backslashes are left alone: a separator on one platform, a filename character on
        # the other, and this node is not the place to guess which.
        ("C:\\Assets", "Models", "C:\\Assets/Models"),
    ],
)
async def test_append_path_collapses_slashes(
    project: Path, registry: Registry, a: str, b: str, expected: str
) -> None:
    outputs = await run_node(registry, "core.append_path", project, {"a": a, "b": b})
    assert outputs["path"] == expected


async def test_append_path_needs_nothing_to_exist(project: Path, registry: Registry) -> None:
    """It is string work, so it can address a file a later node is going to write."""
    outputs = await run_node(
        registry, "core.append_path", project, {"a": "nowhere", "b": "not-yet.txt"}
    )
    assert outputs["path"] == "nowhere/not-yet.txt"


# ------------------------------------------------------------------ core.pathname


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Assets/Models/hero.png", "hero.png"),
        ("hero.png", "hero.png"),
        # A trailing separator says "this is a directory", not "this has no name".
        ("Assets/Models/", "Models"),
        ("Assets/Models", "Models"),
        ("Assets//Models//", "Models"),
        ("/Assets/hero.png", "hero.png"),
        # Read both separators, unlike `core.append_path`, which writes only one. A node
        # that resolves a file answers with `str(Path)`, so on Windows this is the shape
        # the port is actually handed.
        (r"D:\Workspace\Assets\hero.png", "hero.png"),
        ("Assets\\Models\\", "Models"),
        ("  Assets/Models/hero.png  ", "hero.png"),
    ],
)
async def test_pathname_takes_the_last_segment(
    project: Path, registry: Registry, path: str, expected: str
) -> None:
    outputs = await run_node(registry, "core.pathname", project, {"path": path})
    assert outputs["name"] == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Assets/Models/hero.png", "hero"),
        # Only the last suffix, exactly as `Path.stem` reads it.
        ("Assets/archive.tar.gz", "archive.tar"),
        # A leading dot is not a suffix, or a dotfile would come back as nothing at all.
        ("Assets/.gitignore", ".gitignore"),
        # Nothing to remove is not an error.
        ("Assets/Models", "Models"),
        ("Assets/Models/", "Models"),
    ],
)
async def test_pathname_can_drop_the_extension(
    project: Path, registry: Registry, path: str, expected: str
) -> None:
    outputs = await run_node(
        registry, "core.pathname", project, {"path": path, "remove_extension": True}
    )
    assert outputs["name"] == expected


async def test_pathname_decides_from_the_text_and_not_from_the_disk(
    project: Path, registry: Registry
) -> None:
    """It is string work: a folder whose name carries a dot loses the tail, and a name for
    a file nothing has written yet is answered just the same."""
    versioned = await run_node(
        registry, "core.pathname", project, {"path": "Assets/v1.2", "remove_extension": True}
    )
    assert versioned["name"] == "v1"

    unwritten = await run_node(
        registry, "core.pathname", project, {"path": "nowhere/not-yet.txt"}
    )
    assert unwritten["name"] == "not-yet.txt"


async def test_pathname_refuses_an_empty_value(project: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="No path provided"):
        await run_node(registry, "core.pathname", project, {"path": "   "})


@pytest.mark.parametrize("path", ["/", "//", "\\"])
async def test_pathname_says_so_when_there_is_no_final_segment(
    project: Path, registry: Registry, path: str
) -> None:
    """An empty name travelling downstream is worse than failing where it was asked for."""
    with pytest.raises(ValueError, match="no final segment"):
        await run_node(registry, "core.pathname", project, {"path": path})


# ------------------------------------------------------------------ core.parent_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The three cases the node was asked for: a file and a folder climb alike, and
        # `levels` says how far.
        ("/path/some/folder/file.txt", "/path/some/folder"),
        ("/path/some/folder/subfolder", "/path/some/folder"),
        ("Assets/Models/hero.png", "Assets/Models"),
        # A trailing separator says "this is a directory", not "climb one more".
        ("Assets/Models/lods/", "Assets/Models"),
        ("Assets/Models//lods//", "Assets/Models"),
        # Both separators are read, as in `core.pathname`: a node that resolves a file
        # answers with `str(Path)`, which on Windows is what this port is handed.
        (r"D:\Workspace\Assets\hero.png", r"D:\Workspace\Assets"),
        (r"Assets\Models\lods", r"Assets\Models"),
        ("  Assets/Models/hero.png  ", "Assets/Models"),
    ],
)
async def test_parent_path_climbs_one_level_by_default(
    project: Path, registry: Registry, path: str, expected: str
) -> None:
    outputs = await run_node(registry, "core.parent_path", project, {"path": path})
    assert outputs["path"] == expected


@pytest.mark.parametrize(
    ("path", "levels", "expected"),
    [
        ("/path/some/folder/file.txt", 2, "/path/some"),
        ("/path/some/folder/file.txt", 3, "/path"),
        ("/path/some/folder/file.txt", 4, "/"),
        ("Assets/Models/lods/hero.png", 2, "Assets/Models"),
        ("Assets/Models/lods/hero.png", 3, "Assets"),
        (r"D:\Assets\Models\hero.png", 2, r"D:\Assets"),
    ],
)
async def test_parent_path_climbs_as_many_levels_as_asked(
    project: Path, registry: Registry, path: str, levels: int, expected: str
) -> None:
    outputs = await run_node(
        registry, "core.parent_path", project, {"path": path, "levels": levels}
    )
    assert outputs["path"] == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # A root is spelled with its separator: bare `D:` means "wherever D: is currently
        # pointed", which is somewhere else entirely.
        ("/hero.png", "/"),
        (r"D:\hero.png", "D:\\"),
        ("D:/hero.png", "D:/"),
    ],
)
async def test_parent_path_stops_at_the_root_and_spells_it_with_its_separator(
    project: Path, registry: Registry, path: str, expected: str
) -> None:
    outputs = await run_node(registry, "core.parent_path", project, {"path": path})
    assert outputs["path"] == expected


async def test_parent_path_needs_nothing_to_exist(project: Path, registry: Registry) -> None:
    """String work, like its two siblings: it names the folder of a file nobody has
    written yet, and a folder whose name carries a dot is still just a segment."""
    unwritten = await run_node(
        registry, "core.parent_path", project, {"path": "nowhere/not-yet/output.txt"}
    )
    assert unwritten["path"] == "nowhere/not-yet"

    versioned = await run_node(registry, "core.parent_path", project, {"path": "Assets/v1.2/x"})
    assert versioned["path"] == "Assets/v1.2"


async def test_parent_path_refuses_an_empty_value(project: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="No path provided"):
        await run_node(registry, "core.parent_path", project, {"path": "   "})


@pytest.mark.parametrize(
    ("path", "levels"),
    [
        # Nothing above a single segment, and `.` would be a path nobody wrote.
        ("hero.png", 1),
        ("Assets/hero.png", 2),
        # Past the root of an absolute path.
        ("/hero.png", 2),
        ("/path/some", 3),
        (r"D:\hero.png", 2),
    ],
)
async def test_parent_path_says_so_when_asked_to_climb_past_the_top(
    project: Path, registry: Registry, path: str, levels: int
) -> None:
    """An empty path downstream reads as the project root — a real place, and not the one
    that was asked for."""
    with pytest.raises(ValueError, match="no parent"):
        await run_node(
            registry, "core.parent_path", project, {"path": path, "levels": levels}
        )


@pytest.mark.parametrize("levels", [0, -1])
async def test_parent_path_refuses_a_climb_of_nothing(
    project: Path, registry: Registry, levels: int
) -> None:
    """Zero levels is the path back unchanged, which is a wire, not a node."""
    with pytest.raises(ValueError, match="at least 1"):
        await run_node(
            registry, "core.parent_path", project, {"path": "Assets/hero.png", "levels": levels}
        )


async def test_parent_path_treats_a_missing_level_count_as_one(
    project: Path, registry: Registry
) -> None:
    """An unconnected optional port can arrive as None; `int(None)` is a crash."""
    outputs = await run_node(
        registry, "core.parent_path", project, {"path": "Assets/Models/hero.png", "levels": None}
    )
    assert outputs["path"] == "Assets/Models"


# ------------------------------------------------------------------ core.reparent_path


@pytest.mark.parametrize(
    ("path", "parents", "new_parent", "expected"),
    [
        # The two the node exists for: same tree, different top, whatever the depth.
        ("Assets/some/folder/file.txt", 1, "Source", "Source/some/folder/file.txt"),
        ("Assets/some/folder/file.txt", 2, "Export", "Export/folder/file.txt"),
        ("Assets/hero.png", 1, "Source", "Source/hero.png"),
        # Several folders go in as easily as one.
        ("Assets/hero.png", 1, "Build/Source", "Build/Source/hero.png"),
        # Nothing to put on top: the removed folders are simply gone.
        ("Assets/some/folder/file.txt", 3, "", "file.txt"),
        # Nothing to remove: the new parent goes above the whole path.
        ("Assets/hero.png", 0, "Top", "Top/Assets/hero.png"),
        # Read like `core.pathname` and `core.parent_path` — both separators — and written
        # like `core.append_path`, forward-slashed.
        (r"Assets\some\folder\file.txt", 1, "Source", "Source/some/folder/file.txt"),
        ("Assets//some///file.txt", 1, "Source", "Source/some/file.txt"),
        ("  Assets/some/file.txt  ", 1, "Source", "Source/some/file.txt"),
        ("Assets/some/file.txt", 1, "Source/", "Source/some/file.txt"),
        # An absolute path's root is its first parent, so one level replaces it.
        ("D:/Assets/some/file.txt", 1, "Source", "Source/Assets/some/file.txt"),
        (r"D:\Assets\some\file.txt", 2, "Source", "Source/some/file.txt"),
        ("/Assets/some/file.txt", 2, "Source", "Source/some/file.txt"),
        # And the new parent may itself be a root, which is how a path leaves the project.
        ("Assets/some/file.txt", 1, "D:/Export", "D:/Export/some/file.txt"),
    ],
)
async def test_reparent_path_swaps_the_top_of_a_path(
    project: Path, registry: Registry, path: str, parents: int, new_parent: str, expected: str
) -> None:
    outputs = await run_node(
        registry,
        "core.reparent_path",
        project,
        {"path": path, "parents": parents, "new_parent": new_parent},
    )
    assert outputs["path"] == expected


async def test_reparent_path_removes_one_parent_by_default(
    project: Path, registry: Registry
) -> None:
    outputs = await run_node(
        registry, "core.reparent_path", project, {"path": "Assets/a/b.txt", "new_parent": "Source"}
    )
    assert outputs["path"] == "Source/a/b.txt"


async def test_reparent_path_treats_a_missing_count_as_one(
    project: Path, registry: Registry
) -> None:
    """An unconnected optional port can arrive as None; `int(None)` is a crash."""
    outputs = await run_node(
        registry,
        "core.reparent_path",
        project,
        {"path": "Assets/a/b.txt", "parents": None, "new_parent": "Source"},
    )
    assert outputs["path"] == "Source/a/b.txt"


async def test_reparent_path_needs_nothing_to_exist(project: Path, registry: Registry) -> None:
    """String work like its siblings — it names where a later node will write."""
    outputs = await run_node(
        registry,
        "core.reparent_path",
        project,
        {"path": "Assets/not-yet/output.txt", "new_parent": "Export"},
    )
    assert outputs["path"] == "Export/not-yet/output.txt"
    assert not (project / "Export").exists()


@pytest.mark.parametrize(("path", "parents"), [("file.txt", 1), ("Assets/a/b.txt", 3), ("a", 4)])
async def test_reparent_path_refuses_to_leave_nothing(
    project: Path, registry: Registry, path: str, parents: int
) -> None:
    """Removing the file itself is not a reparent, and the empty path downstream reads as
    the project root — a real place, and not the one that was asked for."""
    with pytest.raises(ValueError, match="leaves nothing"):
        await run_node(
            registry,
            "core.reparent_path",
            project,
            {"path": path, "parents": parents, "new_parent": "Source"},
        )


async def test_reparent_path_refuses_to_bury_a_root(project: Path, registry: Registry) -> None:
    """`Source/D:/Assets/x` is not a path anything can open."""
    with pytest.raises(ValueError, match="root would end up in the middle"):
        await run_node(
            registry,
            "core.reparent_path",
            project,
            {"path": "D:/Assets/x.txt", "parents": 0, "new_parent": "Source"},
        )


async def test_reparent_path_refuses_an_empty_value(project: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="No path provided"):
        await run_node(registry, "core.reparent_path", project, {"path": "   "})


async def test_reparent_path_refuses_a_negative_count(project: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        await run_node(
            registry, "core.reparent_path", project, {"path": "Assets/x.txt", "parents": -1}
        )


# ------------------------------------------------------------------ core.for_each_file


def write(root: Path, relative: str, text: str = "x") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


async def test_for_each_file_lists_one_folder_by_default(
    project: Path, registry: Registry
) -> None:
    write(project, "Assets/b.txt")
    write(project, "Assets/a.txt")
    write(project, "Assets/nested/deep.txt")

    outputs = await run_node(registry, "core.for_each_file", project, {"directory": "Assets"})

    # Sorted, so a pipeline over the same folder does the same work in the same order.
    assert outputs["path"] == ["Assets/a.txt", "Assets/b.txt"]


async def test_for_each_file_walks_everything_below_when_asked(
    project: Path, registry: Registry
) -> None:
    write(project, "Assets/a.txt")
    write(project, "Assets/nested/deep.txt")
    write(project, "Assets/nested/deeper/deepest.txt")

    outputs = await run_node(
        registry, "core.for_each_file", project, {"directory": "Assets", "recursive": True}
    )

    assert outputs["path"] == [
        "Assets/a.txt",
        "Assets/nested/deep.txt",
        "Assets/nested/deeper/deepest.txt",
    ]


async def test_for_each_file_answers_project_relative_forward_slashed_paths(
    project: Path, registry: Registry
) -> None:
    """The spelling `core.path` gives, and what `core.reparent_path` downstream expects."""
    write(project, "Assets/models/hero.png")

    outputs = await run_node(
        registry, "core.for_each_file", project, {"directory": "Assets", "recursive": True}
    )

    assert outputs["path"] == ["Assets/models/hero.png"]
    assert "\\" not in outputs["path"][0]


async def test_for_each_file_does_not_descend_into_dotted_folders(
    project: Path, registry: Registry
) -> None:
    """A recursive walk of a game project that stepped into `.git` would iterate over tens
    of thousands of objects nobody meant to process."""
    write(project, "Assets/hero.png")
    write(project, "Assets/.git/objects/abcdef")
    write(project, "Assets/.venv/lib/thing.py")

    outputs = await run_node(
        registry, "core.for_each_file", project, {"directory": "Assets", "recursive": True}
    )

    assert outputs["path"] == ["Assets/hero.png"]


async def test_for_each_file_hides_no_file_and_walks_a_dotted_folder_it_is_pointed_at(
    project: Path, registry: Registry
) -> None:
    """Only the descent is skipped. A dotted *file* is a file, and a dotted folder chosen
    on purpose is the folder that was chosen."""
    write(project, "Assets/.gitignore")
    write(project, "Assets/.github/workflows/ci.yml")

    ordinary = await run_node(
        registry, "core.for_each_file", project, {"directory": "Assets"}
    )
    assert ordinary["path"] == ["Assets/.gitignore"]

    chosen = await run_node(
        registry,
        "core.for_each_file",
        project,
        {"directory": "Assets/.github", "recursive": True},
    )
    assert chosen["path"] == ["Assets/.github/workflows/ci.yml"]


@pytest.mark.parametrize(
    "extensions",
    [
        ".png",
        "png",
        "*.png",
        # Case is the file system's business, not the person typing the filter's.
        ".PNG",
        # Separated however the fingers land — the latitude `plastic`'s path field allows.
        ".png, .gif",
        ".png .gif",
        "png,gif",
        ".png\n.gif",
        # A repeat says nothing new, and neither does a stray separator.
        ".png, .png, ,",
    ],
)
async def test_for_each_file_filters_by_extension(
    project: Path, registry: Registry, extensions: str
) -> None:
    write(project, "Assets/hero.png")
    write(project, "Assets/hero.txt")
    write(project, "Assets/notes.md")

    outputs = await run_node(
        registry,
        "core.for_each_file",
        project,
        {"directory": "Assets", "extensions": extensions},
    )

    assert outputs["path"] == ["Assets/hero.png"]


async def test_for_each_file_takes_every_extension_it_was_given(
    project: Path, registry: Registry
) -> None:
    write(project, "Assets/hero.png")
    write(project, "Assets/hero.jpg")
    write(project, "Assets/hero.txt")

    outputs = await run_node(
        registry,
        "core.for_each_file",
        project,
        {"directory": "Assets", "extensions": ".png, .jpg"},
    )

    assert outputs["path"] == ["Assets/hero.jpg", "Assets/hero.png"]


async def test_for_each_file_matches_the_case_it_finds_on_disk(
    project: Path, registry: Registry
) -> None:
    write(project, "Assets/loud.PNG")
    write(project, "Assets/quiet.png")

    outputs = await run_node(
        registry, "core.for_each_file", project, {"directory": "Assets", "extensions": "png"}
    )

    assert outputs["path"] == ["Assets/loud.PNG", "Assets/quiet.png"]


async def test_for_each_file_reads_a_compound_extension_as_written(
    project: Path, registry: Registry
) -> None:
    """`Path.suffix` knows only the last one; a person typing `.tar.gz` means both."""
    write(project, "Assets/archive.tar.gz")
    write(project, "Assets/notes.gz")

    compound = await run_node(
        registry,
        "core.for_each_file",
        project,
        {"directory": "Assets", "extensions": ".tar.gz"},
    )
    assert compound["path"] == ["Assets/archive.tar.gz"]

    # And the last suffix alone still takes both, which is the other half of the same rule.
    last = await run_node(
        registry, "core.for_each_file", project, {"directory": "Assets", "extensions": ".gz"}
    )
    assert last["path"] == ["Assets/archive.tar.gz", "Assets/notes.gz"]


async def test_for_each_file_filters_the_recursive_walk_too(
    project: Path, registry: Registry
) -> None:
    write(project, "Assets/hero.png")
    write(project, "Assets/hero.txt")
    write(project, "Assets/nested/villain.png")
    write(project, "Assets/nested/villain.txt")

    outputs = await run_node(
        registry,
        "core.for_each_file",
        project,
        {"directory": "Assets", "recursive": True, "extensions": ".png"},
    )

    assert outputs["path"] == ["Assets/hero.png", "Assets/nested/villain.png"]


async def test_for_each_file_says_how_many_the_filter_left(
    project: Path, registry: Registry
) -> None:
    """A filter that matched nothing and an empty folder both skip the loop body, so the
    log line is the only thing that can tell them apart."""
    write(project, "Assets/hero.png")
    write(project, "Assets/hero.txt")
    sink: list[str] = []

    outputs = await run_node(
        registry,
        "core.for_each_file",
        project,
        {"directory": "Assets", "extensions": ".fbx"},
        sink,
    )

    assert outputs["path"] == []
    assert sink == ["0 of 2 file(s) in Assets match .fbx"]


async def test_for_each_file_treats_a_blank_filter_as_no_filter(
    project: Path, registry: Registry
) -> None:
    """An unconnected optional port can arrive as None, and a field cleared to spaces is
    the user turning the filter off, not asking for files ending in nothing."""
    write(project, "Assets/hero.png")
    write(project, "Assets/hero.txt")

    for blank in (None, "", "   ", ",", "*", "."):
        outputs = await run_node(
            registry,
            "core.for_each_file",
            project,
            {"directory": "Assets", "extensions": blank},
        )
        assert outputs["path"] == ["Assets/hero.png", "Assets/hero.txt"], blank


async def test_for_each_file_answers_an_empty_list_for_an_empty_folder(
    project: Path, registry: Registry
) -> None:
    (project / "Empty").mkdir()
    outputs = await run_node(registry, "core.for_each_file", project, {"directory": "Empty"})
    assert outputs["path"] == []


async def test_for_each_file_refuses_what_is_not_a_directory(
    project: Path, registry: Registry
) -> None:
    write(project, "Assets/hero.png")
    with pytest.raises(NotADirectoryError):
        await run_node(registry, "core.for_each_file", project, {"directory": "Assets/hero.png"})
    with pytest.raises(NotADirectoryError):
        await run_node(registry, "core.for_each_file", project, {"directory": "nowhere"})


async def test_for_each_file_stays_inside_the_project(
    project: Path, registry: Registry, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(registry, "core.for_each_file", project, {"directory": str(outside)})


async def test_for_each_file_refuses_an_empty_value(project: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="No directory provided"):
        await run_node(registry, "core.for_each_file", project, {"directory": "  "})


# ------------------------------------------------------------------ core.path


async def test_path_answers_project_relative_by_default(
    project: Path, registry: Registry
) -> None:
    """Relative is what keeps a saved pipeline working after the project moves."""
    target = project / "Assets" / "Models"
    target.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    outputs = await run_node(registry, "core.path", project, {"path": "Assets/Models"}, logs)

    assert outputs["path"] == "Assets/Models"
    assert any("directory" in line for line in logs)


async def test_path_answers_absolute_when_asked(project: Path, registry: Registry) -> None:
    target = project / "Assets" / "reference.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG")

    outputs = await run_node(
        registry, "core.path", project, {"path": "Assets/reference.png", "absolute": True}
    )

    assert Path(outputs["path"]) == target.resolve()
    assert Path(outputs["path"]).is_absolute()


async def test_path_forward_slashes_the_relative_form(project: Path, registry: Registry) -> None:
    """The Explorer reports paths this way, and so does every node that consumes one."""
    (project / "Assets" / "Models").mkdir(parents=True, exist_ok=True)
    outputs = await run_node(
        registry, "core.path", project, {"path": str(Path("Assets") / "Models")}
    )
    assert outputs["path"] == "Assets/Models"


async def test_path_refuses_to_leave_the_project(
    project: Path, tmp_path: Path, registry: Registry
) -> None:
    """The node runs with the server's rights; the guard is what keeps it in the project."""
    outsider = tmp_path / "elsewhere"
    outsider.mkdir()

    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(registry, "core.path", project, {"path": str(outsider)})

    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(registry, "core.path", project, {"path": "../elsewhere"})


async def test_path_refuses_a_missing_target_by_default(
    project: Path, registry: Registry
) -> None:
    with pytest.raises(FileNotFoundError):
        await run_node(registry, "core.path", project, {"path": "Assets/nothing.png"})


async def test_path_allows_a_missing_target_when_told_to(
    project: Path, registry: Registry
) -> None:
    """Naming somewhere a later node will create is a real use, and the guard still applies."""
    outputs = await run_node(
        registry,
        "core.path",
        project,
        {"path": "Assets/Generated", "must_exist": False},
    )
    assert outputs["path"] == "Assets/Generated"

    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(
            registry, "core.path", project, {"path": "../outside", "must_exist": False}
        )


async def test_path_refuses_an_empty_value(project: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="No path provided"):
        await run_node(registry, "core.path", project, {"path": "   "})


# ------------------------------------------------ core.write_text_file / core.read_text_file


async def test_text_survives_a_write_and_a_read(project: Path, registry: Registry) -> None:
    written = await run_node(
        registry,
        "core.write_text_file",
        project,
        {"text": "hero\nvillain\n", "path": "Assets/notes.txt"},
    )
    assert Path(written["file"]) == (project / "Assets" / "notes.txt").resolve()

    read = await run_node(registry, "core.read_text_file", project, {"path": "Assets/notes.txt"})
    assert read["text"] == "hero\nvillain\n"
    assert read["size"] == len("hero\nvillain\n")


async def test_write_text_falls_back_to_the_assets_directory(
    project: Path, registry: Registry
) -> None:
    """No path means the pipeline's own output, which belongs in .voxarium_studio/assets."""
    outputs = await run_node(registry, "core.write_text_file", project, {"text": "hello"})
    assert Path(outputs["file"]) == (project / ".voxarium_studio" / "assets" / "output.txt").resolve()
    assert Path(outputs["file"]).read_text(encoding="utf-8") == "hello"


async def test_write_text_treats_a_directory_as_one(project: Path, registry: Registry) -> None:
    """A folder in that field means put it in here, not become this folder."""
    (project / "Assets").mkdir(parents=True, exist_ok=True)
    outputs = await run_node(
        registry, "core.write_text_file", project, {"text": "hello", "path": "Assets"}
    )
    assert Path(outputs["file"]) == (project / "Assets" / "output.txt").resolve()
    assert (project / "Assets").is_dir()


async def test_the_file_nodes_stay_inside_the_project(
    project: Path, tmp_path: Path, registry: Registry
) -> None:
    """They run with the server's rights; the boundary check is what keeps them in."""
    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(
            registry, "core.write_text_file", project, {"text": "x", "path": "../escaped.txt"}
        )

    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(
            registry, "core.read_text_file", project, {"path": str(tmp_path / "elsewhere.txt")}
        )


async def test_read_text_refuses_a_missing_file_and_an_empty_path(
    project: Path, registry: Registry
) -> None:
    with pytest.raises(FileNotFoundError):
        await run_node(registry, "core.read_text_file", project, {"path": "nothing.txt"})

    with pytest.raises(ValueError, match="No path provided"):
        await run_node(registry, "core.read_text_file", project, {"path": "   "})


# -------------------------------------------------- core.write_bin_file / core.read_bin_file


BINARY = b"\x89PNG\r\n\x1a\n\x00\xff\xfe" * 8
"""Bytes no text codec would survive — the whole point of the binary pair."""


async def test_binary_survives_a_read_and_a_write_byte_for_byte(
    project: Path, registry: Registry
) -> None:
    source = project / "Assets" / "hero.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(BINARY)

    read = await run_node(registry, "core.read_bin_file", project, {"path": "Assets/hero.png"})
    assert Path(read["data"]) == source.resolve()
    assert read["size"] == len(BINARY)

    written = await run_node(
        registry,
        "core.write_bin_file",
        project,
        {"data": read["data"], "path": "Assets/Generated/copy.png"},
    )
    assert Path(written["file"]).read_bytes() == BINARY


async def test_write_bin_keeps_the_source_name_when_given_a_folder(
    project: Path, registry: Registry
) -> None:
    """Nothing else can name the file: the bytes are opaque and the extension matters."""
    source = project / "Assets" / "hero.vox"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(BINARY)

    into_assets = await run_node(registry, "core.write_bin_file", project, {"data": str(source)})
    assert Path(into_assets["file"]) == (project / ".voxarium_studio" / "assets" / "hero.vox").resolve()

    into_folder = await run_node(
        registry, "core.write_bin_file", project, {"data": str(source), "path": "Assets/Meshes/"}
    )
    assert Path(into_folder["file"]) == (project / "Assets" / "Meshes" / "hero.vox").resolve()


async def test_write_bin_onto_itself_leaves_the_file_alone(
    project: Path, registry: Registry
) -> None:
    """`copyfile` would truncate it to nothing, which is a data loss for a no-op."""
    source = project / "Assets" / "hero.vox"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(BINARY)

    outputs = await run_node(
        registry, "core.write_bin_file", project, {"data": str(source), "path": "Assets/hero.vox"}
    )
    assert Path(outputs["file"]) == source.resolve()
    assert source.read_bytes() == BINARY


async def test_the_binary_nodes_refuse_what_is_not_there(
    project: Path, registry: Registry
) -> None:
    with pytest.raises(FileNotFoundError):
        await run_node(registry, "core.read_bin_file", project, {"path": "Assets/nothing.png"})

    with pytest.raises(FileNotFoundError):
        await run_node(registry, "core.write_bin_file", project, {"data": "Assets/nothing.png"})

    with pytest.raises(ValueError, match="No data provided"):
        await run_node(registry, "core.write_bin_file", project, {"data": "  "})


# ------------------------------------------------------------------ the schemas


def test_pathname_takes_its_path_on_a_pin(registry: Registry) -> None:
    """It exists to be fed by an upstream node — `core.path`, or anything answering a file
    — so unlike the picker-driven `core.path` its input is a pin out of the box."""
    schema = registry.node_type("core.pathname")
    assert schema is not None
    assert schema.input("path").connectable is True
    assert schema.input("path").required is True
    # The option is a setting, not a wire.
    assert schema.input("remove_extension").connectable is False
    assert schema.input("remove_extension").default is False
    assert schema.output("name").type == "string"


def test_parent_path_is_the_other_half_of_pathname(registry: Registry) -> None:
    """Same input port, opposite answer: one keeps the last segment and the other drops it,
    so anything wired into one can be wired into the other."""
    schema = registry.node_type("core.parent_path")
    assert schema is not None
    assert schema.input("path").connectable is True
    assert schema.input("path").required is True
    assert schema.input("path").type == registry.node_type("core.pathname").input("path").type
    # How far to climb is a setting, not a wire — and one folder up is the common case.
    assert schema.input("levels").connectable is False
    assert schema.input("levels").type == "integer"
    assert schema.input("levels").default == 1
    assert schema.input("levels").minimum == 1
    assert schema.output("path").type == "string"


def test_the_path_port_is_the_type_the_picker_reads(registry: Registry) -> None:
    """`path` rather than `file` or `directory` is what lets the picker offer both."""
    schema = registry.node_type("core.path")
    assert schema is not None
    assert schema.input("path").type == "path"
    assert schema.output("path").type == "string"
    # Its answer depends on what is on disk, which a memoised one would stop re-checking.
    assert schema.execution.cacheable is False


def test_the_string_nodes_are_pure_and_cacheable(registry: Registry) -> None:
    for node_type in ("core.append", "core.append_path", "core.pathname", "core.parent_path"):
        execution = registry.node_type(node_type).execution
        assert execution.kind == "python_function"
        assert execution.cacheable is True


def test_the_file_nodes_come_in_a_text_pair_and_a_binary_pair(registry: Registry) -> None:
    """Four nodes, one axis: text is decoded and binary is not, and both read and write."""
    for node_type, port in (("core.read_text_file", "text"), ("core.read_bin_file", "data")):
        schema = registry.node_type(node_type)
        assert schema is not None, f"{node_type} must be registered"
        assert schema.input("path").type == "file", "the picker has to offer a file"
        assert schema.input("path").required is True
        assert schema.output(port) is not None
        assert schema.output("size").type == "integer"
        # Reading is pure given the file, and the cache key already covers its contents.
        assert schema.execution.cacheable is True

    for node_type, port in (("core.write_text_file", "text"), ("core.write_bin_file", "data")):
        schema = registry.node_type(node_type)
        assert schema is not None, f"{node_type} must be registered"
        assert schema.input(port).required is True
        # The destination may not exist yet, so it is typed to be written rather than picked.
        assert schema.input("path").type == "string"
        assert schema.input("path").required is False
        assert schema.output("file").type == "file"
        # It touches the disk every time; a memoised write is a write that did not happen.
        assert schema.execution.cacheable is False

    assert registry.node_type("core.read_bin_file").output("data").type == "file", (
        "binary data travels as the file that holds it, which is what every binary node reads"
    )


def test_the_old_undifferentiated_file_nodes_are_gone(registry: Registry) -> None:
    """Replaced by the four above — a pipeline holding one has to be repointed."""
    assert registry.node_type("core.read_file") is None
    assert registry.node_type("core.write_file") is None
