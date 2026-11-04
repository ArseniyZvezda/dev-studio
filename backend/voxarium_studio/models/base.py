from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp. Stored naive-UTC by SQLite, normalised on read."""
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    ident = uuid.uuid4().hex[:16]
    return f"{prefix}_{ident}" if prefix else ident


class UtcDateTime(TypeDecorator):
    """A datetime column that comes back timezone-aware.

    SQLite has no timezone type, so a plain ``DateTime`` hands back naive values and
    ``model_dump(mode="json")`` emits ``2026-07-31T15:17:07`` with no offset. The SPA
    compensates by assuming UTC, but any other client — or anyone reading the API
    directly — would silently shift every timestamp by their own offset.

    Values are normalised to UTC going in and tagged with it coming out, which is what
    makes the promise in :func:`utcnow`'s docstring actually true.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class EnumString(TypeDecorator):
    """A ``StrEnum`` stored as its plain string value, and read back as the enum.

    The string column is deliberate: ``app.db`` stays readable to plain SQL, unlike
    SQLAlchemy's default enum-name mapping. But SQLAlchemy then returns ``str``, and
    Pydantic logs ``PydanticSerializationUnexpectedValue`` on every dump because the
    field is declared as an enum. Re-coercing on load keeps both properties.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], **kwargs: Any) -> None:
        self.enum_class = enum_class
        super().__init__(**kwargs)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # A value written by an older or newer Studio: hand it back untouched rather
            # than failing to load the row.
            return value
