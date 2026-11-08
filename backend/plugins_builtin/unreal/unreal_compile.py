"""Export the project-local products UBT listed for this compile (§9.5)."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


def export_compile_products(project: Path, manifest: Path, destination: Path) -> None:
    """Copy this target's products into a fresh staging folder in project-relative layout."""
    try:
        products = ET.parse(manifest).getroot().find("BuildProducts")
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"Could not read the UnrealBuildTool manifest {manifest}: {exc}") from exc
    if products is None:
        raise ValueError(f"No BuildProducts section in UnrealBuildTool manifest {manifest}")

    root = project.resolve().parent
    destination = destination.resolve()
    copied = False
    for entry in products.findall("string"):
        source = Path(entry.text or "")
        if not source.is_absolute():
            raise ValueError(f"Expected an absolute build product path in {manifest}: {source}")
        # Editor receipts include the engine's own binaries. Export only files
        # owned by this project and retain the relative layout of modules/plugins.
        if not source.is_relative_to(root) or not source.resolve().is_relative_to(root):
            continue
        output = destination / source.relative_to(root)
        if not output.resolve().is_relative_to(destination):
            raise ValueError(f"Build product would escape the export folder: {source}")
        if source.is_dir():
            # A macOS build product can be an entire .app or .dSYM bundle.
            if destination.is_relative_to(source.resolve()):
                raise ValueError(f"Cannot export a build product into itself: {source}")
            shutil.copytree(source, output, symlinks=True, dirs_exist_ok=True)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
        copied = True
    if not copied:
        raise ValueError(f"UnrealBuildTool listed no build products inside {root} in {manifest}")
