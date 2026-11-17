"""Config read diagnostics retain categories, never rejected input (VS-103 C).

All input/exception secrets are synthetic. No app context, server or credentials
are needed: the shared fixtures isolate Studio home and disable external work.
"""

from __future__ import annotations

import logging
import socket
import tempfile
from pathlib import Path

import pytest
import yaml

from voxarium_studio.core.application_config import application_config_file
from voxarium_studio.core.config import AppConfig, _read_project_config, load_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project_identity import identity_migration_plan


CANARY = "VS103_SYNTHETIC_CONFIG_SECRET_DO_NOT_LOG"
MALFORMED_YAML = f"project_identity: {{id: proposed-id, revision: 1}}\nsecret: [{CANARY}\n"


@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path: Path, studio_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even fallback OS home/temp lookups must remain inside the test sandbox."""
    home, temporary = tmp_path / "os-home", tmp_path / "os-temp"
    home.mkdir()
    temporary.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def no_network(*args, **kwargs):
        raise AssertionError("Config read regressions must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


def snapshot(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Detect new/deleted paths, rewrites and identical-byte replacements."""
    return {
        str(path.relative_to(root)): (
            (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else None
        )
        for path in root.rglob("*")
    }


def assert_redacted_warning(
    caplog: pytest.LogCaptureFixture, paths: ProjectPaths, category: str,
) -> None:
    records = [r for r in caplog.records if r.name == "voxarium_studio.core.config"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.msg.startswith("Could not read %s (%s)")
    assert record.msg.endswith("falling back to defaults")
    # Inspect deferred logging args as well as the rendered message: an exception
    # hidden in args still leaks to structured handlers even if formatting is safe.
    assert record.args == (paths.config_file, category)
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    for surface in (record.msg, record.getMessage(), repr(record.args), repr(record.__dict__), caplog.text):
        assert CANARY not in surface
        assert MALFORMED_YAML not in surface
    assert str(paths.config_file) in record.getMessage()
    assert f"({category})" in record.getMessage()


@pytest.mark.parametrize("entrypoint", ["read", "load", "load_with_migration"])
@pytest.mark.parametrize("existing_home_config", [False, True])
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        ("malformed_yaml", "yaml_error"),
        ("invalid_utf8", "unicode_error"),
        ("unicode", "unicode_error"),
        ("decode", "unicode_error"),
        ("encode", "unicode_error"),
        ("translate", "unicode_error"),
        ("os", "read_error"),
        ("yaml", "yaml_error"),
    ],
)
def test_config_read_failure_is_redacted_read_only_and_keeps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    entrypoint: str, existing_home_config: bool, failure: str, category: str,
) -> None:
    paths = ProjectPaths.for_project(tmp_path / "managed-project")
    paths.data.mkdir(parents=True)
    content = MALFORMED_YAML.encode() if failure == "malformed_yaml" else b"project_name: ignored\n"
    if failure == "invalid_utf8":
        content = b"secret: " + CANARY.encode() + b"\xff\n"
    paths.config_file.write_bytes(content)
    if existing_home_config:
        application_config_file().write_text("{}\n", encoding="utf-8")
    before = snapshot(tmp_path)

    errors = {
        "unicode": UnicodeError(CANARY),
        "decode": UnicodeDecodeError(CANARY, CANARY.encode(), 0, 1, CANARY),
        "encode": UnicodeEncodeError(CANARY, CANARY, 0, 1, CANARY),
        "translate": UnicodeTranslateError(CANARY, 0, 1, CANARY),
        # Both filenames and strerror are untrusted, not just exception.args[0].
        "os": OSError(5, CANARY, CANARY, None, CANARY + "-destination"),
        "yaml": yaml.YAMLError(CANARY),
    }
    if failure in errors:
        original_read_text = Path.read_text

        def rejected_read(path: Path, *args, **kwargs):
            if path == paths.config_file:
                raise errors[failure]
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", rejected_read)

    with caplog.at_level(logging.WARNING, logger="voxarium_studio.core.config"):
        if entrypoint == "read":
            assert _read_project_config(paths) == ({"project_name": paths.root.name}, False)
        else:
            config = load_config(paths, migrate_application=entrypoint == "load_with_migration")
            assert config.model_dump() == AppConfig(project_name=paths.root.name).model_dump()
            assert config.project_identity is None

    assert_redacted_warning(caplog, paths, category)
    assert snapshot(tmp_path) == before


def test_identity_plan_does_not_repair_or_assign_identity_for_malformed_yaml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    paths = ProjectPaths.for_project(tmp_path / "managed-project")
    paths.data.mkdir(parents=True)
    paths.config_file.write_text(MALFORMED_YAML, encoding="utf-8")
    before = snapshot(tmp_path)

    first = identity_migration_plan(paths)
    second = identity_migration_plan(paths)

    assert first == second
    assert first.state == "invalid_config"
    assert first.reasons == ("invalid_config",)
    assert first.persistent_id is None
    assert first.identity_revision is None
    assert first.config_fingerprint is not None
    assert CANARY not in first.model_dump_json()
    assert CANARY not in caplog.text
    assert snapshot(tmp_path) == before
