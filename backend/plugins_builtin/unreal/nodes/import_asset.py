"""Import a file into an Unreal project through the editor's Python API.

Runs in the Studio's interpreter. The half that touches ``unreal`` is generated as a
script and executed *inside the editor* by ``_ue.run_script`` — see that module for why.
"""

from __future__ import annotations

import json
from pathlib import Path

import _voxarium_studio as td
import _ue

# Rendered into the editor. Placeholders are substituted rather than %-formatted or
# str.format-ed, because the body is full of dict literals and % operators of its own.
# Parameters arrive as a JSON literal, so a Windows path or a quote in an asset name
# cannot break out of the script.
EDITOR_SCRIPT = '''\
import json

import unreal

PARAMS = json.loads(__VOXARIUM_STUDIO_PARAMS__)
MARKER = __VOXARIUM_STUDIO_MARKER__


def build_options(filename, params):
    """FBX import options. Version-tolerant: fall back to the factory defaults."""
    if not filename.lower().endswith(".fbx"):
        return None
    try:
        options = unreal.FbxImportUI()
        options.set_editor_property("import_mesh", True)
        options.set_editor_property("import_as_skeletal", params["import_as_skeletal"])
        options.set_editor_property("import_materials", params["import_materials"])
        options.set_editor_property("import_textures", params["import_materials"])
        options.set_editor_property("import_animations", False)
        options.set_editor_property(
            "mesh_type_to_import",
            unreal.FBXImportType.FBXIT_SKELETAL_MESH
            if params["import_as_skeletal"]
            else unreal.FBXImportType.FBXIT_STATIC_MESH,
        )
        return options
    except Exception as exc:
        unreal.log_warning("voxarium_studio: using default FBX options (%s)" % exc)
        return None


def run():
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", PARAMS["filename"])
    task.set_editor_property("destination_path", PARAMS["destination_path"])
    if PARAMS["destination_name"]:
        task.set_editor_property("destination_name", PARAMS["destination_name"])
    # Without this the editor puts up modal dialogs, which never come back headless.
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", PARAMS["replace_existing"])
    task.set_editor_property("save", PARAMS["save"])

    options = build_options(PARAMS["filename"], PARAMS)
    if options is not None:
        task.set_editor_property("options", options)

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    paths = [str(path) for path in (task.get_editor_property("imported_object_paths") or [])]
    if not paths:
        # Some factories leave imported_object_paths empty; ask the asset registry instead.
        guess = "%s/%s" % (
            PARAMS["destination_path"].rstrip("/"),
            PARAMS["destination_name"] or PARAMS["stem"],
        )
        if unreal.EditorAssetLibrary.does_asset_exist(guess):
            paths = [guess]
    return paths


try:
    imported = run()
except Exception as exc:
    unreal.log_error("voxarium_studio: import failed: %s" % exc)
    print(MARKER + json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
else:
    print(MARKER + json.dumps({"asset_paths": imported, "count": len(imported)}))
'''


def build_editor_script(params: dict) -> str:
    """Exposed so tests can compile it without an editor anywhere in sight."""
    return EDITOR_SCRIPT.replace(
        "__VOXARIUM_STUDIO_PARAMS__", repr(json.dumps(params))
    ).replace("__VOXARIUM_STUDIO_MARKER__", repr(_ue.RESULT_MARKER))


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)
    settings = _ue.UnrealSettings.from_payload(td.settings(payload), values)

    source = td.require_file(values.get("file"), "file").resolve()

    destination = str(values.get("destination_path") or "/Game/VoxariumStudio").strip()
    if not destination.startswith("/"):
        td.fail(
            "destination_path must be an Unreal content path such as /Game/Meshes, "
            f"got {destination!r}"
        )
    destination = destination.rstrip("/") or "/Game"

    params = {
        "filename": str(source),
        "destination_path": destination,
        "destination_name": str(values.get("asset_name") or "").strip(),
        "stem": source.stem,
        "replace_existing": bool(values.get("replace_existing", True)),
        "save": bool(values.get("save", True)),
        "import_as_skeletal": bool(values.get("import_as_skeletal", False)),
        "import_materials": bool(values.get("import_materials", True)),
    }

    td.log(f"importing {source.name} -> {destination}")
    td.log(f"transport: {settings.transport}")

    work_dir = Path((payload.get("context") or {}).get("work_dir") or ".")
    try:
        result, _lines = _ue.run_script(
            settings,
            build_editor_script(params),
            work_dir=work_dir,
            timeout=1500,
            log=td.log,
        )
    except _ue.UnrealError as exc:
        td.fail(str(exc))
        return

    if result is None:
        td.fail(
            "The editor ran the script but reported nothing back. Its log is above — the "
            "usual cause is the Python plugin failing to load."
        )
        return
    if result.get("error"):
        td.fail(f"Unreal refused the import: {result['error']}")
        return

    paths = [str(path) for path in (result.get("asset_paths") or [])]
    if not paths:
        td.fail(
            f"Unreal imported nothing from {source.name}. Check that the editor supports "
            "this file type and that the content path exists."
        )
        return

    for path in paths:
        td.log(f"imported {path}")

    td.emit({"asset_paths": paths, "asset_path": paths[0], "asset_count": len(paths)})


if __name__ == "__main__":
    main()
