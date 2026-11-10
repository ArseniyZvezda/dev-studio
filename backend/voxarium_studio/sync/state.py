"""What this checkout last agreed with the remote about (§16.3).

For every document, the server revision this Studio last had and the hash of its text at that
revision. That pair is the *base* of a three-way comparison: a file whose hash still matches
it has not been touched here, so a newer server revision can simply be written over it; one
that differs was edited here, and is either pushed or — when the server moved too — a
conflict.

Kept in the user's config directory, keyed by the project's path, not in ``.voxarium_studio/``:
it describes one person's checkout, and committing it would hand everybody else a base that
is not theirs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import user_config_dir

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass(slots=True)
class Base:
    rev: int
    hash: str | None
    """``None`` for a document the server holds a tombstone for."""


@dataclass(slots=True)
class SyncState:
    path: Path
    url: str = ""
    space_id: str = ""
    cursor: int = 0
    """The highest server revision this checkout has been told about."""
    docs: dict[str, Base] = field(default_factory=dict)

    @staticmethod
    def file_for(project_root: Path) -> Path:
        digest = hashlib.sha1(str(project_root).encode("utf-8")).hexdigest()[:16]
        return user_config_dir() / "sync" / f"{digest}.json"

    @classmethod
    def load(cls, project_root: Path, *, url: str, space_id: str) -> "SyncState":
        """The remembered state — or an empty one when it is about another server or space.

        A space's id is minted when the space is created, so a server whose database was
        replaced announces a different one and every remembered revision is rightly dropped:
        the next sync compares what is on disk with what the server has, from nothing.
        """
        path = cls.file_for(project_root)
        fresh = cls(path=path, url=url, space_id=space_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fresh
        except (OSError, ValueError) as exc:
            log.warning("Could not read sync state %s (%s); starting over", path, exc)
            return fresh
        if (
            not isinstance(raw, dict)
            or raw.get("version") != STATE_VERSION
            or raw.get("url") != url
            or raw.get("space_id") != space_id
        ):
            return fresh
        docs: dict[str, Base] = {}
        for doc_path, entry in (raw.get("docs") or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("rev"), int):
                digest = entry.get("hash")
                docs[doc_path] = Base(entry["rev"], digest if isinstance(digest, str) else None)
        cursor = raw.get("cursor")
        return cls(
            path=path, url=url, space_id=space_id,
            cursor=cursor if isinstance(cursor, int) else 0, docs=docs,
        )

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "url": self.url,
            "space_id": self.space_id,
            "cursor": self.cursor,
            "docs": {path: {"rev": base.rev, "hash": base.hash} for path, base in self.docs.items()},
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=".sync-",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream)
            temporary.replace(self.path)
        except OSError as exc:
            # Losing the state costs a full comparison on the next connect, never a document.
            log.warning("Could not save sync state %s (%s)", self.path, exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
