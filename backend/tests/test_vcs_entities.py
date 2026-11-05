"""VCS ownership, config migration, and card settings contracts (§9.6)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from voxarium_studio.core.config import AppConfig, load_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.main import create_app
from voxarium_studio.vcs.base import run_tool


def test_migrated_settings_can_be_cleared_without_resurrecting_legacy_values(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    raw = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    raw["plugins"]["plastic"] = {
        "enabled": False, "settings": {"executable": "C:/old/cm.exe", "workspace": ".."}
    }
    paths.config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        before = client.get("/api/vcs").json()
        assert {entry["id"] for entry in before["backends"]} == {"git", "plastic", "perforce"}
        plastic = next(entry for entry in before["backends"] if entry["id"] == "plastic")
        assert plastic["settings"] == {"executable": "C:/old/cm.exe", "workspace": ".."}
        response = client.put("/api/vcs/settings", json={"settings": {"plastic": {"executable": "", "workspace": None}}})
        assert response.status_code == 200
        cleared = next(entry for entry in response.json()["backends"] if entry["id"] == "plastic")
        assert cleared["settings"] == {} and cleared["executable"] == "cm"

    config = load_config(paths)
    assert "plastic" not in config.plugins
    assert "plastic" not in config.vcs.settings


@pytest.mark.parametrize("settings", [
    {"unknown": {"executable": "tool"}},
    {"git": {"workspace": "elsewhere"}},
    {"plastic": {"workspace": ["invalid"]}},
    {"perforce": {"client": "bad\nClient: other"}},
])
def test_invalid_patch_is_atomic_even_when_it_also_changes_the_backend(client, project, settings) -> None:
    before = client.get("/api/settings").json()["vcs"]
    disk_before = ProjectPaths.for_project(project).config_file.read_bytes()
    response = client.put("/api/vcs/settings", json={"backend": "none", "settings": settings})
    assert response.status_code == 400
    assert client.get("/api/settings").json()["vcs"] == before
    assert ProjectPaths.for_project(project).config_file.read_bytes() == disk_before


def test_perforce_card_exposes_and_saves_every_setting(client) -> None:
    values = {"executable": "C:/missing/p4.exe", "port": "ssl:server:1666", "user": "artist", "client": "game", "charset": "utf8"}
    response = client.put("/api/vcs/settings", json={"settings": {"perforce": values}})
    assert response.status_code == 200
    card = next(entry for entry in response.json()["backends"] if entry["id"] == "perforce")
    assert card["settings"] == values
    assert {field["key"] for field in card["settings_fields"]} == set(values)


async def test_builtin_plastic_nodes_use_card_settings_after_plugin_reload(ctx) -> None:
    ctx.config.vcs.settings["plastic"] = {"executable": "C:/card/cm.exe", "workspace": ".."}
    await ctx.reload_plugins()
    assert ctx.registry.plugin("plastic") is None
    for node_id in ("plastic.checkin", "plastic.add", "plastic.status"):
        schema = ctx.registry.node_type(node_id)
        assert schema is not None
        assert ctx.runner._plugin_settings(schema.plugin_id) == ctx.config.vcs.settings["plastic"]
        assert ctx.registry.manifest_for_node_type(node_id) is None


def test_cli_detection_uses_vcs_settings(monkeypatch, project: Path) -> None:
    from voxarium_studio import vcs
    from voxarium_studio.cli import _describe_vcs
    from voxarium_studio.vcs.plastic import PlasticBackend

    config = AppConfig(vcs={"backend": "plastic", "settings": {"plastic": {"executable": "C:/missing/cm.exe"}}})
    async def detect(root, *, configured, settings_for):
        assert configured == "plastic"
        assert settings_for("plastic") == config.vcs.settings["plastic"]
        return PlasticBackend(root, settings_for("plastic"))

    monkeypatch.setattr(vcs, "detect_backend", detect)
    assert "C:/missing/cm.exe" in _describe_vcs(ProjectPaths.for_project(project), config, None)


async def test_vcs_subprocess_accepts_utf8_input_without_shell(tmp_path: Path) -> None:
    result = await run_tool(
        sys.executable,
        ["-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        cwd=tmp_path, input="Description:\n\tИзменение ассета\n",
    )
    assert result.ok
    assert result.stdout == "Description:\n\tИзменение ассета\n"
