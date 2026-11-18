"""Persistent metadata and local-only migration diagnostics (§5.2.1, §16.8).

S1 deliberately does not activate the v2 sync adapter, resolve legacy names to
UUIDs, read credentials/bases, or offer an identity mutation. Runtime descriptors
remain root-based (§5.3.2). The guarded I15 apply lives in api/sync.py; this
module only adds the strict, read-only v1 base reader it relies on.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .paths import ProjectPaths


class ProjectIdentity(BaseModel):
    """Portable business identity, never a runtime project_id (§5.2.1)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    revision: int = Field(ge=1)

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        # Unlike the legacy selector, new IDs must already be canonical.
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value, flags=re.ASCII) is None:
            raise ValueError("Project ID must be 1–64 canonical lowercase ASCII characters")
        return value


class IdentityMigrationPlan(BaseModel):
    """A diagnostic snapshot, not permission to apply/enable or a catalog mapping.

    has_legacy_key is unknown in S1: credential inspection belongs to S2. No key
    file, private base, environment token or network is consulted here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    migration_plan_id: str
    config_fingerprint: str | None
    persistent_id: str | None = None
    identity_revision: int | None = None
    legacy_url: str | None = None
    legacy_selector: str | None = None
    has_legacy_key: bool | None = None
    state: Literal[
        "needs_identity", "needs_profile", "identity_present",
        "migration_required", "migration_conflict", "invalid_config",
    ]
    reasons: tuple[str, ...] = ()


class _PlanConfigLoader(yaml.SafeLoader):
    """Fail closed on ambiguous YAML, without changing general config loading.

    Diagnostics must not choose an identity by mapping/merge precedence (§16.8).
    Non-cyclic aliases and non-overlapping merges retain SafeLoader semantics.
    """

    def get_single_data(self):
        node = self.get_single_node()
        if node is None:
            return None
        # Check the composed graph before flatten_mapping mutates merge nodes.
        # Shared aliases are fine; recursive aliases cannot describe project data.
        visited, active = set(), set()

        def visit(current):
            if current in active:
                raise yaml.YAMLError("Recursive config alias")
            if current in visited:
                return
            active.add(current)
            if isinstance(current, yaml.MappingNode):
                for key, value in current.value:
                    visit(key)
                    visit(value)
            elif isinstance(current, yaml.SequenceNode):
                for value in current.value:
                    visit(value)
            active.remove(current)
            visited.add(current)

        visit(node)
        return self.construct_document(node)

    def flatten_mapping(self, node):
        if sum(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value) > 1:
            raise yaml.YAMLError("Repeated config merge key")
        super().flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node)
            try:
                if key in seen:
                    raise yaml.YAMLError("Repeated config mapping key")
                seen.add(key)
            except TypeError:
                raise yaml.YAMLError("Unhashable config mapping key") from None


class LegacyBaseError(ValueError):
    """The v1 checkpoint exists but cannot be read exactly; never repaired here."""

    def __init__(self, code: str = "legacy_base_invalid") -> None:
        self.code = code
        super().__init__(code)


def read_legacy_base(paths: ProjectPaths) -> dict | None:
    """Strict, read-only view of THIS root's v1 checkpoint (§9.2, I15).

    The legacy runner keys ``sync/<sha1(root)>.json`` by the same ``paths.root``
    and loads it leniently ("starting over" on damage). Migration may not: a
    present file must have exactly the v1 shape and every base must also be a
    valid v2 base, otherwise ``legacy_base_invalid`` (no repair, no default).
    Missing is ``None``. Returns nonsecret values plus the exact-bytes digest;
    it never writes, moves or deletes the old file.
    """
    import json
    from ..sync.catalog import CatalogError
    from ..sync.catalog_reads import _path
    from ..sync.state import STATE_VERSION, SyncState

    path = SyncState.file_for(paths.root)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            pass
        raise LegacyBaseError("legacy_base_unavailable") from None
    except OSError:
        raise LegacyBaseError("legacy_base_unavailable") from None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    result = None
    try:
        raw = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
        if (not isinstance(raw, dict) or set(raw) != {"version", "url", "space_id", "cursor", "docs"}
                or type(raw["version"]) is not int or raw["version"] != STATE_VERSION
                or not isinstance(raw["url"], str) or not isinstance(raw["space_id"], str)
                or type(raw["cursor"]) is not int or raw["cursor"] < 0
                or not isinstance(raw["docs"], dict)):
            raise ValueError
        docs = {}
        for doc_path, entry in raw["docs"].items():
            _path(doc_path)
            if (not isinstance(entry, dict) or set(entry) != {"rev", "hash"}
                    or type(entry["rev"]) is not int or not 1 <= entry["rev"] <= raw["cursor"]
                    or entry["hash"] is not None and (
                        not isinstance(entry["hash"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", entry["hash"]) is None)):
                raise ValueError
            docs[doc_path] = (entry["rev"], entry["hash"])
        result = {"url": raw["url"], "space_id": raw["space_id"], "cursor": raw["cursor"],
                  "docs": docs, "fingerprint": hashlib.sha256(content).hexdigest()}
    except (ValueError, TypeError, RecursionError, CatalogError):
        pass
    if result is None:
        raise LegacyBaseError("legacy_base_invalid")
    return result


def identity_migration_plan(paths: ProjectPaths) -> IdentityMigrationPlan:
    """Read exact on-disk config without fallback/default repair (§16.8).

    The deterministic plan identifier binds this root and config bytes. It is not
    a CAS implementation: future guarded apply must reread and compare them.
    No candidate ID is generated, even for an unconfigured legacy project.
    """
    from .application_config import application_config_file
    from .config import AppConfig, _project_fields
    from ..sync.protocol import check_space_name, normalise_url

    fingerprint = None
    try:
        content = paths.config_file.read_bytes()
    except FileNotFoundError:
        raw = {}
        read_error = "missing_config"
    except OSError:
        raw = {}
        read_error = "unreadable_config"
    else:
        fingerprint = hashlib.sha256(content).hexdigest()
        try:
            raw = yaml.load(content.decode("utf-8"), Loader=_PlanConfigLoader)
            read_error = None if isinstance(raw, dict) else "invalid_config"
        except (ValueError, yaml.YAMLError, RecursionError):
            # Includes UnicodeError and SafeLoader timestamp constructor errors.
            # Only decoding/parsing is covered; model validation stays below.
            raw, read_error = {}, "invalid_config"

    plan_id = hashlib.sha256(
        (str(paths.root) + "\0" + (fingerprint or read_error or "")).encode("utf-8")
    ).hexdigest()
    base = {"migration_plan_id": plan_id, "config_fingerprint": fingerprint}
    if read_error:
        return IdentityMigrationPlan(**base, state="invalid_config", reasons=(read_error,))

    if application_config_file().exists():
        # Match load_config's scope split once global preferences exist (§5.2).
        # Only stat the home config: diagnostics need neither its values nor fallback.
        project_raw = _project_fields(raw)
        if "theme" in raw and not isinstance(raw["theme"], dict):
            # The shared filter treats a malformed mixed-scope container as empty.
            # A diagnostic must not repair potentially damaged project accent data.
            project_raw["theme"] = raw["theme"]
        raw = project_raw

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError:
        # Never echo a malformed config or validation input: it may contain secrets.
        return IdentityMigrationPlan(**base, state="invalid_config", reasons=("invalid_config",))

    identity = config.project_identity
    if identity is not None:
        base.update(persistent_id=identity.id, identity_revision=identity.revision)
    reasons = []
    selector = None
    if config.sync.space:
        try:
            selector = check_space_name(config.sync.space)
        except ValueError:
            reasons.append("invalid_legacy_selector")
    if config.sync.url:
        try:
            base["legacy_url"] = normalise_url(config.sync.url)
        except ValueError:
            reasons.append("invalid_legacy_url")
    base["legacy_selector"] = selector
    if reasons:
        return IdentityMigrationPlan(**base, state="invalid_config", reasons=tuple(reasons))
    if identity is not None and selector is not None and identity.id != selector:
        return IdentityMigrationPlan(
            **base, state="migration_conflict", reasons=("identity_selector_conflict",),
        )
    if selector is not None:
        # Even equality is NOT evidence of the v2 UUID mapping. Resolve explicitly later.
        return IdentityMigrationPlan(
            **base, state="needs_profile" if base.get("legacy_url") else "migration_required",
            reasons=("legacy_resolution_required",),
        )
    if identity is None and (config.sync.enabled or config.sync.url):
        # With an identity, URL without sync.space is the canonical §4.1 form
        # (after a migrate/select commit), not a legacy selector gone missing.
        return IdentityMigrationPlan(
            **base, state="migration_required", reasons=("legacy_selector_missing",),
        )
    return IdentityMigrationPlan(
        **base, state="identity_present" if identity else "needs_identity",
        reasons=("guarded_cutover_required",) if identity else ("explicit_identity_required",),
    )
