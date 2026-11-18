"""User-wide settings, stored beside the plugin registry and recent projects (§5.2)."""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import unicodedata
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .config import (
    AppConfig, ApplicationThemeConfig, ThemeConfig, UpdatesConfig,
    default_orchestrator_models, validate_orchestrator_models,
)
from .api_backends import ApiBackendConfig
from .paths import user_config_dir

log = logging.getLogger(__name__)
_file_lock = RLock()


class _ApplicationLoader(yaml.SafeLoader):
    """Do not silently collapse duplicate registry keys before CAS validation."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in result:
                    raise yaml.YAMLError("Duplicate application setting")
                result[key] = self.construct_object(value_node, deep=deep)
            except TypeError:
                raise yaml.YAMLError("Invalid application setting") from None
        return result


class _ApplicationMutationLoader(_ApplicationLoader):
    """Refuse aliases/merges rather than guessing ownership while rewriting YAML."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("Aliased application setting")
        return super().compose_node(parent, index)

    def construct_object(self, node, deep=False):
        # SafeDumper loses these tags (turning tuple pairs into ordinary lists).
        if node.tag in ("tag:yaml.org,2002:omap", "tag:yaml.org,2002:pairs"):
            raise yaml.YAMLError("Unsupported application mapping")
        return super().construct_object(node, deep=deep)

    def construct_mapping(self, node, deep=False):
        if any(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value):
            raise yaml.YAMLError("Merged application setting")
        return super().construct_mapping(node, deep=deep)


def _application_view(raw: dict) -> dict:
    """Project only owned fields into DTOs; opaque data stays on disk (§5.2.1)."""
    view = {key: raw[key] for key in ApplicationConfig.model_fields if key in raw}
    for key, model in (("theme", ApplicationThemeConfig), ("updates", UpdatesConfig)):
        if isinstance(view.get(key), dict):
            view[key] = {name: view[key][name] for name in model.model_fields if name in view[key]}
    # Profile records remain strict: unknown fields there may be credentials.
    return view


def validate_profile_name(value: object) -> str:
    """Local labels only: no wire NFC/UTF-8 byte rules (Studio design §3.1)."""
    if not isinstance(value, str) or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("Invalid profile name")
    name = value.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("Invalid profile name")
    return name


class RemoteServerProfile(BaseModel):
    """Personal metadata only; never an effective/project setting (§5.2.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    profile_id: str
    name: str
    url: str
    revision: int = Field(default=1, ge=1, strict=True)

    @field_validator("profile_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        try:
            valid = str(UUID(value)) == value
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Invalid profile ID")
        return value

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: object, info: ValidationInfo) -> str:
        if not isinstance(value, str):
            raise ValueError("Invalid profile name")
        # Disk metadata is not fresh input: preserve legacy labels literally on
        # reads and unrelated saves (remote-identity/studio-design §3.1).
        if info.context and info.context.get("stored_profile_metadata"):
            return value
        return validate_profile_name(value)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        from ..sync.protocol import normalise_url

        try:
            return normalise_url(value)
        except ValueError:
            raise ValueError("Invalid server address") from None


class ApplicationConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    api_backends: list[ApiBackendConfig] = Field(default_factory=list)
    remote_servers: list[RemoteServerProfile] = Field(default_factory=list)
    theme: ApplicationThemeConfig = Field(default_factory=ApplicationThemeConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    show_data_dir: bool = False
    node_cache_enabled: bool = True
    language: Literal["en", "ru"] | None = None
    respond_language: str = "English"
    orchestrator_models: dict[str, dict[str, str]] = Field(default_factory=default_orchestrator_models)

    @field_validator("orchestrator_models", mode="before")
    @classmethod
    def valid_orchestrator_models(cls, value: object) -> dict[str, dict[str, str]]:
        return validate_orchestrator_models(value)

    @model_validator(mode="after")
    def unique_profiles(self) -> ApplicationConfig:
        if len({p.id for p in self.api_backends}) != len(self.api_backends):
            raise ValueError("Duplicate API backend ID")
        if (len({p.profile_id for p in self.remote_servers}) != len(self.remote_servers)
                or len({p.url for p in self.remote_servers}) != len(self.remote_servers)):
            raise ValueError("Remote profile collision")
        return self

    @classmethod
    def from_effective(cls, config: AppConfig) -> ApplicationConfig:
        """Seed old preferences once; the accent never leaves its project."""
        return cls(
            theme=ApplicationThemeConfig.model_validate(
                config.theme.model_dump(exclude={"accent"})
            ),
            updates=config.updates.model_copy(deep=True),
            show_data_dir=config.explorer.show_data_dir,
            node_cache_enabled=config.node_cache_enabled,
            language=config.language,
            respond_language=config.respond_language,
            orchestrator_models=validate_orchestrator_models(config.orchestrator_models),
        )


def application_config_file() -> Path:
    return user_config_dir() / "config.yaml"


def load_application_config(
    *, legacy: AppConfig | None = None, migrate: bool = False,
) -> ApplicationConfig:
    """Read global preferences; only an explicit project open may persist migration.

    An empty or malformed existing file is still an application file: another project's
    old values must never take over. Malformed metadata is kept untouched; preference saves cannot repair the registry.
    """
    with _file_lock:
        path = application_config_file()
        if not path.exists():
            config = ApplicationConfig.from_effective(legacy) if legacy else ApplicationConfig()
            if migrate and legacy is not None:
                save_application_config(config)
            return config
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_ApplicationLoader) or {}
            if not isinstance(raw, dict):
                raise ValueError()
            return ApplicationConfig.model_validate(
                _application_view(raw), context={"stored_profile_metadata": True},
            )
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError):
            log.warning("Could not read application settings; using defaults")
            return ApplicationConfig()


@contextlib.contextmanager
def application_config_transaction():
    """Serialize personal metadata writers, including independent local processes.

    The stable lock file is never replaced/deleted. A process crash releases the OS
    lock. The reentrant Python lock also protects threads on platforms with flock.
    """
    with _file_lock:
        path = application_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".yaml.lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if not stream.tell():
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _load_application_document() -> tuple[dict, ApplicationConfig]:
    """Mutation read: retain opaque YAML separately from the validated public view."""
    path = application_config_file()
    if not path.exists():
        return {}, ApplicationConfig()
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_ApplicationMutationLoader)
        if not isinstance(raw, dict):
            raise ValueError()
        return raw, ApplicationConfig.model_validate(
            _application_view(raw), context={"stored_profile_metadata": True},
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError):
        raise ValueError("application_store_unavailable") from None


def _load_application_config_strict() -> ApplicationConfig:
    """Never turn malformed metadata into an empty registry for a mutation."""
    return _load_application_document()[1]


def _fsync_directory(path: Path) -> None:
    """Persist entry publication before removing a cross-file recovery fence."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_application_document(raw: dict) -> None:
    """Internal atomic publication; caller holds application_config_transaction."""
    path = application_config_file()
    text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_application_profiles(config: ApplicationConfig) -> None:
    """Profile publication changes only its metadata, not preferences (§5.2.1)."""
    raw, _ = _load_application_document()
    raw["remote_servers"] = [profile.model_dump(mode="json") for profile in config.remote_servers]
    _write_application_document(raw)


def _write_application_config(config: ApplicationConfig) -> None:
    """Overlay owned preferences without discarding future global fields (§5.2)."""
    raw, _ = _load_application_document()
    for key, value in config.model_dump(mode="json", exclude={"remote_servers", "api_backends"}).items():
        if key != "orchestrator_models" and isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    _write_application_document(raw)


def save_application_config(config: ApplicationConfig) -> None:
    """Save preferences without bypassing profile CAS/lifecycle (§5.2.1).

    Old settings clients can hold stale snapshots: preserve the current registry.
    Malformed existing metadata cannot safely be merged and is left untouched.
    """
    with application_config_transaction():
        _write_application_config(config)


def apply_application_config(config: AppConfig, application: ApplicationConfig) -> None:
    """Update the shared effective object so live services see global preferences (§5.2)."""
    config.theme = ThemeConfig.model_validate({
        **application.theme.model_dump(), "accent": config.theme.accent,
    })
    config.updates = application.updates.model_copy(deep=True)
    config.explorer.show_data_dir = application.show_data_dir
    config.node_cache_enabled = application.node_cache_enabled
    config.language = application.language
    config.respond_language = application.respond_language
    config.orchestrator_models = validate_orchestrator_models(application.orchestrator_models)
