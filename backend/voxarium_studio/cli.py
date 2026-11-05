"""``voxarium_studio`` command line entry point (§2.5)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import typer
import uvicorn

from .build_info import build_info
from .core.config import load_config
from .core.project import init_project
from .core.security import AuthSettings, InsecureBindError

# Said after the project path when the Studio picked one instead of being handed it, so a
# start that opens something unexpected explains itself on the line that names it (§5.1).
WHY_THIS_PROJECT = {"recent": "   (opened last time)", "demo": "   (built-in demo project)"}

app = typer.Typer(
    name="voxarium_studio",
    help="Voxarium Studio — AI pipelines, plugins and agent teams for a game project.",
    add_completion=False,
    no_args_is_help=True,
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("watchfiles").setLevel(logging.WARNING)


@app.command()
def run(
    project: Path | None = typer.Argument(
        None,
        help=(
            "The managed project directory to open (its voxarium_studio/ folder is created here). "
            "Defaults to the project opened last, or the built-in demo project."
        ),
    ),
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8765, help="Port to listen on."),
    token: str | None = typer.Option(
        None,
        help="Shared secret required by the API. Defaults to $VOXARIUM_STUDIO_TOKEN.",
    ),
    allow_insecure: bool = typer.Option(
        False,
        "--allow-insecure",
        help="Permit a non-loopback bind with no token. Only on a trusted network.",
    ),
    dev: bool = typer.Option(
        False, "--dev", help="Enable CORS for the Vite dev server on :5173."
    ),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload the backend on edits."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Start the Studio against a managed project.

    With no path it opens the one it had open last, and the built-in demo project when
    there is nothing to reopen (§5.1) — never the working directory, which is a fact about
    the shell rather than a statement about which project to initialise.
    """
    _configure_logging(verbose)

    # Imported here rather than at the top: this pulls in the application context, and
    # with it FastAPI and the ORM, which `voxarium_studio token` and `--help` have no use for.
    from .core.workspace import resolve_startup_project

    try:
        chosen = resolve_startup_project(project)
    except OSError as exc:
        typer.secho(
            f"Could not create the built-in demo project: {exc}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2) from exc

    project_root = chosen.root
    if not project_root.is_dir():
        typer.secho(f"No such directory: {project_root}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        auth = AuthSettings.resolve(
            host=host,
            token=token or os.environ.get("VOXARIUM_STUDIO_TOKEN"),
            allow_insecure=allow_insecure,
        )
    except InsecureBindError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    paths = init_project(project_root)
    typer.secho(
        f"Project   {paths.root}{WHY_THIS_PROJECT.get(chosen.source, '')}", fg=typer.colors.CYAN
    )
    typer.secho(f"Data      {paths.data}", fg=typer.colors.CYAN)

    url = f"http://{'localhost' if host == '0.0.0.0' else host}:{port}"
    if auth.enabled:
        typer.secho(f"Token     {auth.token}", fg=typer.colors.YELLOW)
        typer.secho(f"Open      {url}/?token={auth.token}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Open      {url}", fg=typer.colors.GREEN)

    if open_browser and not reload:
        import threading
        import webbrowser

        target = f"{url}/?token={auth.token}" if auth.enabled else url
        threading.Timer(1.2, lambda: webbrowser.open(target)).start()

    dev_origins = ["http://localhost:5173", "http://127.0.0.1:5173"] if dev else None

    if reload:
        # Reload needs an import string, so hand the settings over via the environment.
        os.environ["VOXARIUM_STUDIO_PROJECT"] = str(project_root)
        os.environ["VOXARIUM_STUDIO_HOST"] = host
        os.environ["VOXARIUM_STUDIO_ALLOW_INSECURE"] = "1" if allow_insecure else "0"
        os.environ["VOXARIUM_STUDIO_DEV"] = "1" if dev else "0"
        if auth.token:
            os.environ["VOXARIUM_STUDIO_TOKEN"] = auth.token
        else:
            os.environ.pop("VOXARIUM_STUDIO_TOKEN", None)
        uvicorn.run(
            "voxarium_studio.asgi:app", host=host, port=port, reload=True, factory=False
        )
        return

    from .main import create_app

    server = uvicorn.Server(uvicorn.Config(
        create_app(str(project_root), auth=auth, dev_origins=dev_origins),
        host=host, port=port, log_level="debug" if verbose else "info",
    ))
    from .desktop import supervise

    supervise(server)
    server.run()


@app.command()
def init(
    project: Path = typer.Argument(Path.cwd(), help="Directory to initialise."),
    samples: bool = typer.Option(True, "--samples/--no-samples", help="Write example files."),
) -> None:
    """Create the voxarium_studio/ data directory in a project without starting the server."""
    _configure_logging(False)
    project_root = project.expanduser().resolve()
    if not project_root.is_dir():
        typer.secho(f"No such directory: {project_root}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    paths = init_project(project_root, with_samples=samples)
    typer.secho(f"Initialised {paths.data}", fg=typer.colors.GREEN)
    for directory in paths.all_dirs:
        typer.echo(f"  {directory.relative_to(paths.root)}")


@app.command("token")
def make_token() -> None:
    """Print a fresh random token suitable for --token / $VOXARIUM_STUDIO_TOKEN."""
    typer.echo(AuthSettings.generate_token())


@app.command()
def info(project: Path = typer.Argument(Path.cwd())) -> None:
    """Show what the Studio sees for a project: config, plugins, pipelines."""
    _configure_logging(False)
    from .plugins import loader as plugin_loader
    from .core.paths import ProjectPaths
    from .engine.store import PipelineStore

    paths = ProjectPaths.for_project(project)
    if not paths.data.exists():
        typer.secho(f"{paths.root} is not initialised. Run `voxarium_studio init` first.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    config = load_config(paths)
    registry = plugin_loader.load_all(paths, config)

    typer.secho(f"\nStudio: {_build_label()}", fg=typer.colors.CYAN, bold=True)
    typer.secho(f"\nProject: {paths.root}", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Theme: {config.theme.mode} / {config.theme.accent}")
    typer.echo(f"Concurrency: {config.concurrency.as_dict()}")
    typer.echo(f"Version control: {_describe_vcs(paths, config, registry)}")

    typer.secho("\nPlugins", fg=typer.colors.CYAN, bold=True)
    for plugin in registry.plugins():
        marker = {"loaded": "+", "disabled": "-", "error": "!"}.get(plugin.status, "?")
        typer.echo(f"  {marker} {plugin.id:<12} {plugin.manifest.version:<8} ({plugin.manifest.scope})")
        if plugin.error:
            typer.secho(f"      {plugin.error}", fg=typer.colors.RED)

    typer.secho("\nNode types", fg=typer.colors.CYAN, bold=True)
    for schema in registry.node_types():
        typer.echo(f"  {schema.id:<26} {schema.category:<18} {schema.execution.kind}")

    typer.secho("\nPipelines", fg=typer.colors.CYAN, bold=True)
    for pipeline in PipelineStore(paths).list():
        typer.echo(f"  {pipeline.id:<28} {len(pipeline.nodes)} nodes")


def _describe_vcs(paths, config, registry) -> str:
    """Which backend claims the project, and whether its tool is actually installed."""
    import asyncio

    from .vcs import detect_backend

    if config.vcs.disabled:
        return "disabled in config.yaml"

    def settings_for(backend_id: str) -> dict:
        return dict(config.vcs.settings.get(backend_id, {}))

    backend = asyncio.run(
        detect_backend(
            paths.root,
            configured=config.vcs.selected(),
            settings_for=settings_for,
        )
    )
    if backend is None:
        return "none detected"
    available, reason = backend.is_available()
    return f"{backend.name}" if available else f"{backend.name} ({reason})"


def _build_label() -> str:
    """`v0.2.0*` or `c3fd2b1*` — the trailing star is the uncommitted-changes marker the
    UI uses too."""
    build = build_info()
    return build.display + ("*" if build.dirty else "")


@app.command()
def version(verbose: bool = typer.Option(False, "-v", "--verbose", help="Show the commit.")) -> None:
    """Print what this build is called: its tag, or the commit it was built from."""
    build = build_info()
    typer.echo(_build_label())
    if not verbose:
        return
    typer.echo(f"  release: {build.version}")
    typer.echo(f"  tag:     {build.tag or '(untagged commit)'}")
    typer.echo(f"  commit:  {build.commit or 'unknown'}")
    typer.echo(f"  branch:  {build.branch or '(detached or unknown)'}")
    typer.echo(f"  date:    {build.committed_at or 'unknown'}")
    typer.echo(f"  source:  {build.source}" + (" (uncommitted changes)" if build.dirty else ""))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
