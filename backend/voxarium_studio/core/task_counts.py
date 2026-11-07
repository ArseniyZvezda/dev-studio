"""Project task summaries without opening or migrating another project (§5.1)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .paths import existing_data_dir


def read_task_counts(root: Path) -> dict[str, int] | None:
    """Read existing SQLite state only; an unavailable database is not a zero count."""
    counts = {"in_progress": 0, "needs_review": 0}
    try:
        if not root.is_dir():
            return None
        data = existing_data_dir(root)
        if data is None:
            return counts
        path = data / "app.db"
        if not path.exists():
            return counts
        # mode=ro prevents a missing file from being created. Never construct AppContext:
        # looking at a menu must not start agents, plugins or schema migrations (§5.1).
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)) as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM team_task "
                "WHERE status IN ('in_progress', 'needs_review') GROUP BY status"
            )
            counts.update(dict(rows))
        return counts
    except (OSError, sqlite3.Error):
        return None
