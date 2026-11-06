"""Async SQLite access via SQLModel/SQLAlchemy 2.0 + aiosqlite."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import JSON, Boolean, Column, Float, Integer, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from .. import models  # noqa: F401  — import registers tables on SQLModel.metadata

log = logging.getLogger(__name__)


def _default_literal(column: Column) -> str | None:  # type: ignore[type-arg]
    """SQL literal to backfill an added column with, or None to leave it NULL.

    SQLite's ``ADD COLUMN ... DEFAULT x`` stores the default in the schema, so existing
    rows read back as ``x`` — that is what keeps a project's database usable after the
    Studio gains a field, without a migration framework.
    """
    if "migration_default" in column.info:
        return column.info["migration_default"]
    default = getattr(column, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            escaped = value.replace("'", "''")
            return f"'{escaped}'"

    if isinstance(column.type, JSON):
        return "'[]'"

    # Everything below is a fallback for a NOT NULL column, which needs *some* value for the
    # rows that predate it. A nullable one already has the right answer: null means what the
    # model says it means, and backfilling `pos_x` with 0 would tell the org chart that every
    # member who existed before the column was dragged to the origin.
    if column.nullable:
        return None

    if isinstance(column.type, Boolean):
        return "0"
    if isinstance(column.type, (Integer, Float)):
        return "0"
    return None


def _add_missing_columns(conn: Connection) -> None:
    """Add columns the models gained since this database was created.

    ``metadata.create_all`` only ever creates whole tables, so a new field on an existing
    model would otherwise surface as ``no such column`` against a project that has been
    open since before the field existed. Only additive: nothing is dropped or retyped.
    """
    backfill_producer_started = False
    for table in SQLModel.metadata.sorted_tables:
        rows = conn.exec_driver_sql(f"PRAGMA table_info('{table.name}')").fetchall()
        if not rows:
            continue  # create_all just made it — already current.
        existing = {row[1] for row in rows}
        if table.name == "agent_chat" and "producer_started" not in existing:
            backfill_producer_started = True
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(conn.dialect)
            literal = _default_literal(column)
            clause = f" DEFAULT {literal}" if literal is not None else ""
            conn.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{clause}'
            )
            log.info("Added column %s.%s to %s", table.name, column.name, conn.engine.url)

    if backfill_producer_started:
        # §10.11: old workspace drafts were enabled and could inherit a team pause
        # before their first message. A runtime session id alone is insufficient:
        # compaction clears it, and a failed first turn may never receive one.
        conn.exec_driver_sql("""
            UPDATE agent_chat SET producer_started = 1
            WHERE COALESCE(producer_summary, '') != ''
               OR COALESCE(producer_compacted_through, '') != ''
               OR COALESCE(producer_plan_summary, '') != ''
               OR COALESCE(producer_plan_version, 0) > 0
               OR COALESCE(producer_checklist, '[]') NOT IN ('[]', 'null', '')
               OR COALESCE(producer_plan, '[]') NOT IN ('[]', 'null', '')
               OR COALESCE(producer_watch, '[]') NOT IN ('[]', 'null', '')
               OR COALESCE(producer_questions, '[]') NOT IN ('[]', 'null', '')
               OR COALESCE(producer_release_pending, '[]') NOT IN ('[]', 'null', '')
               OR ((producer_mode = 1 OR producer_workspace = 1) AND (
                   producer_state NOT IN ('off', 'discussing', 'not_started')
                   OR COALESCE(agent_session_id, '') != ''
                   OR COALESCE(error, '') != ''
                   OR COALESCE(num_turns, 0) > 0
                   OR COALESCE(total_cost_usd, 0) > 0
                   OR EXISTS (SELECT 1 FROM agent_chat_message
                              WHERE agent_chat_message.chat_id = agent_chat.id)
               ))
        """)
        conn.exec_driver_sql("""
            UPDATE agent_chat SET producer_paused = 0 WHERE producer_started = 0
        """)


def _add_missing_indexes(conn: Connection) -> None:
    """Create indexes the models declare and this database does not have.

    ``metadata.create_all`` builds a table's indexes only when it builds the table, so a
    column added by :func:`_add_missing_columns` arrives unindexed however the model
    declares it — and a **unique** index is not merely a speed-up: it is the constraint
    that makes ``TeamTask.number`` an identity rather than a hopeful convention.

    Separate from :meth:`Database.create_all` on purpose. A unique index cannot be created
    over rows that do not satisfy it yet, so anything backfilling those rows has to run in
    between (:meth:`AppContext.startup`). SQLite cannot add a UNIQUE *column* at all —
    ``ALTER TABLE … ADD COLUMN`` refuses it — which is why the constraint is declared as an
    index (``Field(index=True, unique=True)``) rather than on the column.
    """
    for table in SQLModel.metadata.sorted_tables:
        rows = conn.exec_driver_sql(f"PRAGMA index_list('{table.name}')").fetchall()
        existing = {row[1] for row in rows}
        for index in table.indexes:
            if index.name in existing:
                continue
            try:
                # A savepoint, so a unique index that existing rows violate leaves the rest
                # of this pass — and the connection — usable instead of poisoning both.
                with conn.begin_nested():
                    conn.execute(CreateIndex(index))
            except Exception:
                log.exception("Could not create index %s on %s", index.name, table.name)
            else:
                log.info("Added index %s to %s", index.name, table.name)


class Database:
    """Owns the engine and session factory for one managed project's ``app.db``."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path.as_posix()}",
            echo=False,
            future=True,
            connect_args={"timeout": 30},
        )
        self._configure_pragmas(self._engine)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    @staticmethod
    def _configure_pragmas(engine: AsyncEngine) -> None:
        """WAL + normal sync: concurrent readers while a run streams writes."""

        @event.listens_for(engine.sync_engine, "connect")
        def _on_connect(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            await conn.run_sync(_add_missing_columns)
        log.debug("Schema ready at %s", self.path)

    async def create_missing_indexes(self) -> None:
        """Second half of the schema step — run it *after* any backfill an index needs.

        See :func:`_add_missing_indexes`: it is not folded into :meth:`create_all` because
        a unique index over a column that was just added has to wait for the rows that
        predate the column to be given values.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_add_missing_indexes)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session scope that commits on success and rolls back on failure."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    async def healthcheck(self) -> bool:
        try:
            async with self._session_factory() as session:
                await session.exec(text("SELECT 1"))  # type: ignore[call-overload]
            return True
        except Exception:  # pragma: no cover - diagnostics only
            log.exception("Database healthcheck failed")
            return False

    async def close(self) -> None:
        await self._engine.dispose()
