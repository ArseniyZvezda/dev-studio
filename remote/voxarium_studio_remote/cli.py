"""``voxarium-studio-remote`` — run the server and manage who may connect (dev spec §16.2).

Users are managed here rather than over HTTP on purpose: issuing a key is something the
person who runs the server does, from a shell on it (``docker compose exec``), and an admin
API would be one more thing on the internet to protect.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from . import __version__
from .auth import hash_token, new_token
from .store import Store, UnknownUser, UserExists

DATA_ENV_VAR = "VOXARIUM_REMOTE_DATA"

cli = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Voxarium Studio Remote — the shared server local Studios synchronise through.",
)
user_cli = typer.Typer(no_args_is_help=True, help="Issue, list and revoke user keys.")
space_cli = typer.Typer(no_args_is_help=True, help="Inspect spaces.")
cli.add_typer(user_cli, name="user")
cli.add_typer(space_cli, name="space")

DataOption = typer.Option(
    None,
    "--data",
    help=f"Directory holding remote.db. Defaults to ${DATA_ENV_VAR}, then ./data.",
)


def _data_dir(value: Path | None) -> Path:
    return value or Path(os.environ.get(DATA_ENV_VAR) or "data")


def _store(data: Path | None) -> Store:
    return Store(_data_dir(data) / "remote.db")


@cli.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Address to bind. 0.0.0.0 inside a container."),
    port: int = typer.Option(8787, help="Port to listen on."),
    data: Path | None = DataOption,
    log_level: str = typer.Option("info", help="uvicorn log level."),
) -> None:
    """Run the server. Put TLS in front of it — keys travel in a header."""
    import uvicorn

    from .app import create_app

    logging.basicConfig(
        level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = create_app(_data_dir(data))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        # A seeded room's state for a large document, base64-encoded, is one frame.
        ws_max_size=32 * 1024 * 1024,
        # Behind a reverse proxy the peer is the proxy; trust its forwarded scheme so the
        # log shows who really connected. The proxy is the only thing that can reach us.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@cli.command()
def version() -> None:
    """Print the server's version."""
    typer.echo(__version__)


@user_cli.command("add")
def user_add(
    name: str = typer.Argument(..., help="How this person is shown to the others."),
    color: str | None = typer.Option(None, help="Cursor colour, e.g. #3e63dd. Picked if omitted."),
    data: Path | None = DataOption,
) -> None:
    """Create a user and print their key — once; it cannot be shown again."""
    store = _store(data)
    token = new_token()
    try:
        user = store.add_user(name, hash_token(token), color=color)
    except (UserExists, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"User {user.name} created. Their key, shown only now:")
    typer.echo(token)


@user_cli.command("list")
def user_list(data: Path | None = DataOption) -> None:
    """List users. Keys are not stored, so they are not listed."""
    store = _store(data)
    try:
        users = store.list_users()
    finally:
        store.close()
    if not users:
        typer.echo("No users yet. Create one with:  voxarium-studio-remote user add <name>")
        return
    for user in users:
        state = "revoked" if user.revoked_at else "active"
        typer.echo(f"{user.name}\t{state}\t{user.color}\tcreated {user.created_at}")


@user_cli.command("rotate")
def user_rotate(name: str, data: Path | None = DataOption) -> None:
    """Replace a user's key. The old one stops working; a revoked user is restored."""
    store = _store(data)
    token = new_token()
    try:
        store.set_token_hash(name, hash_token(token))
    except UnknownUser as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"New key for {name}, shown only now:")
    typer.echo(token)


@user_cli.command("revoke")
def user_revoke(name: str, data: Path | None = DataOption) -> None:
    """Stop a user's key from working. Their open connections end within half a minute."""
    store = _store(data)
    try:
        store.revoke_user(name)
    except UnknownUser as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"User {name} revoked.")


@space_cli.command("list")
def space_list(data: Path | None = DataOption) -> None:
    """List spaces and how many documents each holds."""
    store = _store(data)
    try:
        spaces = store.list_spaces()
    finally:
        store.close()
    if not spaces:
        typer.echo("No spaces yet. One is created when the first Studio connects to it.")
        return
    for space, documents in spaces:
        typer.echo(f"{space.name}\t{documents} document(s)\trevision {space.seq}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
