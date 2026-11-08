"""Package an Unreal game or compile a project target on the Studio host (§9.5)."""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from unreal_compile import export_compile_products
from unreal_output import DEFAULT_FOLDER_TEMPLATE, archive_contents, build_folder_name, check_destination, publish
from voxarium_studio.plugin_api import NodeContext, kill_process_tree

BUILD_CONFIGURATIONS = ["Debug", "DebugGame", "Development", "Test", "Shipping"]


def _project_file(ctx: NodeContext) -> Path:
    configured = (
        ctx.get("project")
        or ctx.settings.get("project")
        or os.environ.get("VOXARIUM_STUDIO_UNREAL_PROJECT")
    )
    if configured:
        project = (ctx.project_root / str(configured)).resolve()
        if project.suffix.lower() != ".uproject" or not project.is_file():
            raise ValueError(f"Expected an existing .uproject file at {project}")
        return project
    projects = [path for path in ctx.project_root.glob("*.uproject") if path.is_file()]
    if len(projects) == 1:
        return projects[0].resolve()
    raise ValueError(
        f"Found {len(projects)} .uproject files in {ctx.project_root}. "
        "Select a project file on the Build node or in Unreal Engine settings."
    )


def _engine_directory(ctx: NodeContext) -> Path:
    root = ctx.settings.get("engine_root")
    if root:
        directory = Path(root).expanduser().resolve()
        if (directory / "Engine").is_dir():
            return directory / "Engine"
        if directory.is_dir() and directory.name.lower() == "engine":
            return directory
        raise ValueError(f"Expected an Unreal Engine installation at {directory}")
    editor = ctx.settings.get("editor_cmd") or os.environ.get("VOXARIUM_STUDIO_UNREAL_EDITOR")
    if editor:
        for directory in Path(editor).expanduser().resolve().parents:
            if directory.name.lower() == "engine" and (directory / "Binaries").is_dir():
                return directory
    raise ValueError("Set the engine installation folder or editor executable in Unreal Engine settings.")


def _windows_tool(engine: Path, tool: str, env: dict[str, str]) -> list[str]:
    # Use the engine's .NET host directly so project and archive paths stay single arguments.
    binaries = engine / "Binaries" / "DotNET"
    dll = binaries / tool / f"{tool}.dll"
    if dll.is_file():
        sdk = engine / "Binaries" / "ThirdParty" / "DotNet"
        arch = "win-arm64" if platform.machine().upper() == "ARM64" else "win-x64"
        hosts = [
            *sdk.glob(f"*/{arch}/dotnet.exe"),
            *sdk.glob("*/windows/dotnet.exe"),
            sdk / "Windows" / "dotnet.exe",
        ]
        bundled = max(
            (path for path in hosts if path.is_file()),
            key=lambda path: tuple(int(n) for n in re.findall(r"\d+", path.parent.parent.name)),
            default=None,
        )
        if bundled and env.get("UE_USE_SYSTEM_DOTNET") != "1":
            env["DOTNET_ROOT"] = str(bundled.parent)
            env["PATH"] = str(bundled.parent) + os.pathsep + env.get("PATH", "")
            return [str(bundled), str(dll)]
        if dotnet := shutil.which("dotnet"):
            return [dotnet, str(dll)]
    for exe in (dll.with_suffix(".exe"), binaries / f"{tool}.exe"):
        if exe.is_file():
            return [str(exe)]
    raise ValueError(f"Could not launch {tool} from {binaries}. Check the engine installation and .NET SDK.")


async def _run_tool(ctx: NodeContext, engine: Path, host: str, tool: str, arguments: list[str]) -> None:
    env = os.environ.copy()
    if host == "Win64":
        command = _windows_tool(engine, tool, env)
        cwd = Path(command[-1]).parent
    else:
        scripts = engine / "Build" / "BatchFiles"
        script = scripts / "RunUAT.sh" if tool == "AutomationTool" else scripts / host / "Build.sh"
        if not script.is_file():
            raise ValueError(f"Unreal build script not found: {script}")
        command = ["/bin/bash", str(script)]
        cwd = engine
    process = await asyncio.create_subprocess_exec(
        *command, *arguments, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        limit=1024 * 1024,
    )
    assert process.stdout is not None
    try:
        async for line in process.stdout:
            await ctx.log(line.decode("utf-8", errors="replace").rstrip("\r\n"))
        code = await process.wait()
    finally:
        if process.returncode is None:
            # Drain while stopping: outstanding compiler output can fill the pipe.
            await asyncio.gather(kill_process_tree(process), process.communicate())
    if code:
        raise RuntimeError(f"{tool} exited with code {code}. See the build log for details.")


async def run_build(ctx: NodeContext) -> dict[str, str]:
    project = _project_file(ctx)
    engine = _engine_directory(ctx)
    host = {"win32": "Win64", "darwin": "Mac", "linux": "Linux"}.get(sys.platform)
    if not host:
        raise ValueError(f"Unsupported Unreal build host: {sys.platform}")
    mode = ctx.get("mode") or "Package"
    target = str(ctx.get("target") or "").strip()
    configuration = ctx.get("configuration") or "Development"
    target_platform = str(ctx.get("platform") or "").strip()
    if target_platform in ("", "Host"):
        target_platform = host
    architecture = str(ctx.get("architecture") or "").strip()
    if mode not in ("Package", "Compile") or configuration not in BUILD_CONFIGURATIONS:
        raise ValueError(f"Invalid Unreal build mode or configuration: {mode}, {configuration}")
    # UBT interprets leading switches and '+'-separated positional targets.
    for name, value in (("target", target), ("platform", target_platform)):
        if value and not re.fullmatch(r"\w+", value):
            raise ValueError(f"Expected an Unreal {name} name, got {value!r}")

    fields = {
        "game": project.stem,
        "target": target or "Game",
        "platform": target_platform,
        "configuration": configuration,
        "architecture": architecture or "Default",
        "mode": mode,
    }
    leaf = build_folder_name(str(ctx.get("folder_template", DEFAULT_FOLDER_TEMPLATE) or ""), fields)
    parent = (ctx.project_root / (ctx.get("output_dir") or "Builds")).resolve()
    destination = parent / leaf
    check_destination(destination)
    parent.mkdir(parents=True, exist_ok=True)
    await ctx.log(f"{mode}: {project.name} · {fields['target']} · {target_platform} · {configuration} → {destination}")
    staging = tempfile.TemporaryDirectory(prefix=".unreal-build-", dir=parent, delete=False)
    work = Path(staging.name)
    published = False
    try:
        if mode == "Package":
            archive = work / f"{target_platform}-archive"
            args = [
                "BuildCookRun", f"-project={project}", f"-platform={target_platform}",
                f"-clientconfig={configuration}", f"-serverconfig={configuration}",
                "-build", "-cook", "-stage", "-pak", "-package", "-archive", f"-archivedirectory={archive}",
                "-unattended", "-nop4", "-utf8output", "-WaitForUATMutex",
            ]
            if target:
                args.append(f"-target={target}")
            if architecture:
                args.extend([f"-clientarchitecture={architecture}", f"-serverarchitecture={architecture}"])
            await _run_tool(ctx, engine, host, "AutomationTool", args)
            source = archive_contents(archive, target_platform)
        else:
            manifest = work / "BuildManifest.xml"
            args = [
                target or "-TargetType=Game", target_platform, configuration,
                f"-Project={project}", f"-Manifest={manifest}", "-WaitMutex",
            ]
            if architecture:
                args.append(f"-Architecture={architecture}")
            await _run_tool(ctx, engine, host, "UnrealBuildTool", args)
            source = work / "products"
            # Keep the private directory alive while the worker is copying large binaries.
            copy = asyncio.create_task(asyncio.to_thread(export_compile_products, project, manifest, source))
            try:
                await asyncio.shield(copy)
            except asyncio.CancelledError:
                await copy
                raise
        publish(source, destination, work)
        published = True
    finally:
        # Retain the previous build if another process prevented its rollback.
        if published or not (work / "previous").exists():
            try:
                await asyncio.to_thread(staging.cleanup)
            except OSError as exc:
                await ctx.log(f"Could not clean temporary build directory {work}: {exc}", stream="stderr")
    await ctx.log(f"Build folder: {destination}")
    return {
        "build_dir": str(destination),
        "project": str(project),
        "target": target or "Game",
        "platform": target_platform,
        "configuration": configuration,
    }
