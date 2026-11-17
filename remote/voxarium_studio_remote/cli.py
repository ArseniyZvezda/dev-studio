"""``voxarium-studio-remote`` — run the server and manage who may connect (dev spec §16.2).

Users are managed here rather than over HTTP on purpose: issuing a key is something the
person who runs the server does, from a shell on it (``docker compose exec``), and an admin
API would be one more thing on the internet to protect.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import typer

from . import __version__
from .auth import hash_token, new_token
from .store import Store, UnknownUser, UserExists

DATA_ENV_VAR = "VOXARIUM_REMOTE_DATA"

cli = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    no_args_is_help=True,
    help="Voxarium Studio Remote — the shared server local Studios synchronise through.",
)
user_cli = typer.Typer(no_args_is_help=True, help="Issue, list and revoke user keys.")
migration_cli = typer.Typer(no_args_is_help=True, help="Explicit offline migration. Stop Remote and flush rooms first; backups contain credentials.")
space_cli = typer.Typer(no_args_is_help=True, help="Inspect spaces.")
cli.add_typer(user_cli, name="user")
cli.add_typer(space_cli, name="space")
cli.add_typer(migration_cli, name="migration")

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


def _read_only_user_list(path: Path) -> list[tuple]:
    """Legacy Admin fallback: metadata only, supporting existing v1 AND v2.

    Never construct a Store here: it bootstraps absent/empty databases and is a
    write-capable v2 runtime. Schema recognition and rows share a read snapshot.
    """
    import sqlite3
    from .migrations import schema_version, SCHEMA_VERSION, SchemaError
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        version = schema_version(db)
        if version not in (1, SCHEMA_VERSION):
            raise SchemaError("Unsupported schema")
        return db.execute(
            "SELECT name,color,created_at,revoked_at FROM users" +
            (" WHERE deleted_at IS NULL" if version == SCHEMA_VERSION else "") +
            " ORDER BY created_at,id").fetchall()
    finally:
        db.close()


def _metadata_text(value: object) -> str:
    import unicodedata
    # Each field occupies one terminal column: no escape, bidi, newline or tab
    # from legacy names/colors/timestamps may inject formatting or new rows.
    return "".join(char for char in str(value) if not unicodedata.category(char).startswith("C")
                   and char not in "\u2028\u2029")


@user_cli.command("list")
def user_list(data: Path | None = DataOption) -> None:
    """Read existing v1/v2 users without creating, upgrading or writing a DB."""
    try:
        users = _read_only_user_list(_data_dir(data) / "remote.db")
    except Exception:
        typer.secho("Cannot list users: an existing supported Remote database is required. No database was created or migrated.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    if not users:
        typer.echo("No users yet. Create one with:  voxarium-studio-remote user add <name>")
        return
    for name, color, created_at, revoked_at in users:
        state = "revoked" if revoked_at else "active"
        typer.echo(f"{_metadata_text(name)}\t{state}\t{_metadata_text(color)}\tcreated {_metadata_text(created_at)}")


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
        typer.echo("No projects yet. Create one explicitly through the v2 project catalog.")
        return
    for space, documents in spaces:
        typer.echo(f"{space.name}\t{documents} document(s)\trevision {space.seq}")


def _safe_cli_action(action):
    try:
        return action()
    except Exception:
        typer.secho("Operation failed. Check schema, UUID and expected revisions; no secret diagnostics are displayed.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None


def _by_id(operation, payload, data):
    """Structured by-ID commands reuse the same bounded validation/domain path."""
    from .admin_rpc import handle_request
    from uuid import uuid4
    import json
    request = {"admin_rpc_version": 1, "request_id": uuid4().hex, "operation": operation, "payload": payload}
    response = json.loads(handle_request(json.dumps(request).encode(), _data_dir(data)))
    typer.echo(json.dumps(response, ensure_ascii=True))
    if "error" in response: raise typer.Exit(1)


@user_cli.command("get")
def user_get(user_id: str, data: Path | None = DataOption):
    """Read metadata by immutable UUID; never returns a key."""
    _by_id("users.get", {"user_id": user_id}, data)


@user_cli.command("create")
def user_create(display_name: str, user_id: str | None = typer.Option(None),
                color: str | None = typer.Option(None), data: Path | None = DataOption):
    """Create a v2 UUID user; JSON contains the key once. Legacy `add` keeps its handle."""
    from uuid import uuid4
    _by_id("users.create", {"user_id": uuid4().hex if user_id is None else user_id, "display_name": display_name,
                            **({"color": color} if color is not None else {})}, data)


@user_cli.command("update")
def user_update(user_id: str, expected_revision: int = typer.Option(...),
                display_name: str | None = typer.Option(None), color: str | None = typer.Option(None),
                data: Path | None = DataOption):
    """CAS metadata update, not a credential change."""
    _by_id("users.update", {"user_id": user_id, "expected_metadata_revision": expected_revision,
                            **({"display_name": display_name} if display_name is not None else {}),
                            **({"color": color} if color is not None else {})}, data)


def _credential_payload(user_id, expected_revision, expected_credential_version):
    return {"user_id": user_id, "expected_metadata_revision": expected_revision,
            "expected_credential_version": expected_credential_version}


@user_cli.command("rotate-id")
def user_rotate_id(user_id: str, expected_revision: int = typer.Option(...),
                   expected_credential_version: int = typer.Option(...),
                   restore_revoked: bool = typer.Option(False), data: Path | None = DataOption):
    """Replace key once and terminate old sessions. Lost response: GET, never replay."""
    _by_id("users.rotate", {**_credential_payload(user_id, expected_revision, expected_credential_version),
                            "restore_revoked": restore_revoked}, data)


@user_cli.command("revoke-id")
def user_revoke_id(user_id: str, expected_revision: int = typer.Option(...),
                   expected_credential_version: int = typer.Option(...), data: Path | None = DataOption):
    """Destroy the key and stop sessions, retaining documents/authorship."""
    _by_id("users.revoke", _credential_payload(user_id, expected_revision, expected_credential_version), data)


@user_cli.command("delete-id")
def user_delete_id(user_id: str, expected_revision: int = typer.Option(...),
                   expected_credential_version: int = typer.Option(...), data: Path | None = DataOption):
    """Irreversible logical deletion; even the last user may be removed (SSH recovery)."""
    _by_id("users.delete", _credential_payload(user_id, expected_revision, expected_credential_version), data)


@migration_cli.command("inspect")
def migration_inspect(data: Path | None = DataOption):
    """Read-only schema/counts/mapping report, without token hashes."""
    from .migrations import inspect_database
    import json
    report = _safe_cli_action(lambda: inspect_database(_data_dir(data) / "remote.db"))
    typer.echo(json.dumps(report, ensure_ascii=True))


@migration_cli.command("dry-run")
def migration_dry_run(data: Path | None = DataOption):
    """Validate and migrate a consistent in-memory copy; source is not upgraded."""
    from .migrations import migrate_database
    import json
    report = _safe_cli_action(lambda: migrate_database(_data_dir(data) / "remote.db", dry_run=True))
    typer.echo(json.dumps(report, ensure_ascii=True))


@migration_cli.command("apply")
def migration_apply(backup: Path = typer.Option(..., help="New protected backup file; never overwritten."),
                    confirm_offline: bool = typer.Option(False, help="Assert Remote stopped, rooms flushed/recovery saved and correct data path selected."),
                    data: Path | None = DataOption):
    """Explicit offline v1-to-v2 migration. Does not deploy or start any process."""
    from .migrations import migrate_database
    import json
    def apply_private():
        # This CLI is an offline, single-operation process. Protect the worker's
        # exclusively-created backup from the first byte, not chmod after copy.
        previous = os.umask(0o077)
        try:
            return migrate_database(_data_dir(data) / "remote.db", backup_path=backup,
                                    confirm_offline=confirm_offline)
        finally:
            os.umask(previous)
    report = _safe_cli_action(apply_private)
    typer.echo(json.dumps(report, ensure_ascii=True))


def _offline_backup(source: Path, destination: Path):
    import sqlite3
    from .migrations import inspect_database
    if source.resolve() == destination.resolve(): raise ValueError()
    inspect_database(source)
    # Exclusive destination + private mode; a backup contains credential hashes.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(destination))
    try: src.backup(dst)
    finally: src.close(); dst.close()
    inspect_database(destination)


@migration_cli.command("backup")
def migration_backup(output: Path = typer.Option(...), confirm_offline: bool = typer.Option(False),
                     data: Path | None = DataOption):
    """Consistent SQLite backup, not a copy of the main file of a WAL database."""
    def action():
        if not confirm_offline: raise ValueError()
        _offline_backup(_data_dir(data) / "remote.db", output)
    _safe_cli_action(action)
    typer.echo("Backup created. Protect it like the credential database.")


@migration_cli.command("rollback")
def migration_rollback(backup: Path = typer.Option(..., help="Whole validated v1 backup."),
                       recovery_backup: Path = typer.Option(..., help="New backup of current state for separate recovery/export."),
                       confirm_offline: bool = typer.Option(False),
                       accept_data_loss: bool = typer.Option(False, help="Explicitly accept losing all writes since the v1 backup."),
                       data: Path | None = DataOption):
    """Offline restoration of a WHOLE v1 backup; never run v1 on a v2 schema."""
    import sqlite3
    from .migrations import inspect_database
    def action():
        if not confirm_offline or not accept_data_loss: raise ValueError()
        path = _data_dir(data) / "remote.db"
        if len({path.resolve(), backup.resolve(), recovery_backup.resolve()}) != 3: raise ValueError()
        if inspect_database(backup)["schema_version"] != 1: raise ValueError()
        _offline_backup(path, recovery_backup)
        source = sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(str(path))
        try:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally: source.close(); target.close()
        if inspect_database(path)["schema_version"] != 1: raise ValueError()
    _safe_cli_action(action)
    typer.echo("Whole v1 backup restored. Post-backup writes are not present; current-state recovery backup retained. Keep v2 stopped.")


@cli.command("admin-rpc")
def admin_rpc(data: Path | None = DataOption) -> None:
    """One bounded JSON request/response on stdin/stdout; no payload in argv."""
    from .admin_rpc import MAX_REQUEST_BYTES, handle_request
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    response = handle_request(raw, _data_dir(data))
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
