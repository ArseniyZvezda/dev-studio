"""Which commit the Studio reports itself as being built from (§2.5).

The git half runs against a real repository in ``tmp_path``, for the same reason
``test_vcs.py`` does: the value of the code is the exact arguments it sends to git, and a
mocked subprocess would only assert that the code says what the code says.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxarium_studio import __version__
from voxarium_studio import build_info as bi

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def _uncached() -> Iterator[None]:
    """`build_info()` is deliberately resolved once per process — but not per test."""
    bi.build_info.cache_clear()
    yield
    bi.build_info.cache_clear()


@pytest.fixture
def studio_checkout(tmp_path: Path) -> Path:
    """A stand-in for this repository: `<root>/backend/voxarium_studio/` under real git."""
    root = tmp_path / "studio"
    package = root / "backend" / "voxarium_studio"
    package.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Studio Test")
    git("config", "commit.gpgsign", "false")
    (package / "__init__.py").write_text('__version__ = "5.0.0"\n', encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "first")
    return root


def _pretend_installed_at(monkeypatch: pytest.MonkeyPatch, package: Path) -> None:
    monkeypatch.setattr(bi, "_package_dir", lambda: package)


# ------------------------------------------------------------------- the display string


def test_an_untagged_build_is_named_by_its_commit() -> None:
    info = bi.BuildInfo(version="0.1.0", short_commit="c3fd2b1", commit="c3fd2b1" * 5)
    assert info.display == "c3fd2b1"
    assert info.to_json()["display"] == "c3fd2b1"


def test_a_tag_outranks_the_commit() -> None:
    """Whoever tagged the commit named that build; the hash is the fallback, not the name."""
    info = bi.BuildInfo(version="0.1.0", short_commit="c3fd2b1", tag="v0.2.0")
    assert info.display == "v0.2.0"


def test_display_falls_back_to_the_bare_version_when_the_commit_is_unknown() -> None:
    """An installed package built from a tarball can say nothing else, and must not lie."""
    assert bi.BuildInfo(version="0.1.0").display == "0.1.0"


# ------------------------------------------------------------------------- the git source


@requires_git
def test_a_source_tree_reports_its_head(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=studio_checkout, capture_output=True, text=True
    ).stdout.strip()

    info = bi.build_info()

    assert info.source == "git"
    assert info.commit == head
    assert info.short_commit and head.startswith(info.short_commit)
    assert info.branch == "main"
    assert info.committed_at and info.committed_at.startswith("20")
    assert info.dirty is False
    assert info.tag is None
    assert info.display == info.short_commit
    assert info.version == info.short_commit


@requires_git
@pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
def test_a_tagged_commit_is_named_by_its_tag(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path, annotated: bool
) -> None:
    args = ["tag", "-a", "v0.2.0", "-m", "release"] if annotated else ["tag", "v0.2.0"]
    subprocess.run(["git", *args], cwd=studio_checkout, check=True, capture_output=True)
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")

    info = bi.build_info()
    assert info.tag == "v0.2.0"
    assert info.display == "v0.2.0"
    assert info.version == "0.2.0"


@requires_git
@pytest.mark.parametrize("tag, expected", [
    ("v5.2.0", "5.2.0"), ("v4.1", "4.1.0"),
    ("5.3.1", "5.3.1"), ("v5.3.0-beta.2", "5.3.0-beta.2"),
    ("nightly", "nightly"),
])
def test_runtime_version_uses_the_exact_release_tag(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path, tag: str, expected: str
) -> None:
    subprocess.run(["git", "tag", tag], cwd=studio_checkout, check=True, capture_output=True)
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")

    assert bi.build_info().version == expected
    assert bi.read_build_info().version == expected


@requires_git
def test_a_tag_further_back_does_not_name_this_build(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    """`--exact-match` is the point: a release two commits ago is a different Studio."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=studio_checkout, check=True, capture_output=True)

    git("tag", "v0.2.0")
    (studio_checkout / "README.md").write_text("later\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "after the release")
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")

    info = bi.build_info()
    assert info.tag is None
    assert info.display == info.short_commit
    assert info.version == info.short_commit


@requires_git
def test_uncommitted_changes_make_the_build_dirty(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    """The hash alone stops describing the build the moment the tree moves off it."""
    (studio_checkout / "backend" / "voxarium_studio" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")

    assert bi.build_info().dirty is True


@requires_git
def test_a_detached_head_reports_no_branch(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    subprocess.run(
        ["git", "checkout", "--detach", "HEAD"], cwd=studio_checkout, check=True, capture_output=True
    )
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")

    info = bi.build_info()
    assert info.commit is not None
    assert info.branch is None, "'HEAD' is not a branch anyone can name"


@requires_git
def test_a_package_outside_the_studio_layout_never_claims_a_repository(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    """site-packages can sit inside someone else's repo; its HEAD is not our version."""
    installed = studio_checkout / "venv" / "site-packages" / "voxarium_studio"
    installed.mkdir(parents=True)
    _pretend_installed_at(monkeypatch, installed)

    assert bi.studio_repo_root() is None
    info = bi.build_info()
    assert info.source == "unknown"
    assert info.commit is None
    assert info.display == __version__


def test_no_git_and_no_stamp_still_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pretend_installed_at(monkeypatch, tmp_path)
    assert bi.build_info() == bi.BuildInfo()


# ----------------------------------------------------------------------- the wheel stamp


def test_the_stamp_outranks_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An installed wheel has no repository to ask, so what it shipped with is the truth."""
    (tmp_path / bi.STAMP_NAME).write_text(
        json.dumps(
            {
                "version": "5.0.0",
                "commit": "a" * 40,
                "short_commit": "aaaaaaa",
                "tag": "v9.9.9",
                "branch": "release",
                "committed_at": "2026-08-03T22:04:06+03:00",
                "dirty": False,
            }
        ),
        encoding="utf-8",
    )
    _pretend_installed_at(monkeypatch, tmp_path)

    info = bi.build_info()
    assert info.source == "stamp"
    assert info.display == "v9.9.9", "a tag survives into the wheel, or a release is unnamed"
    assert info.version == "9.9.9"
    assert info.branch == "release"


def test_a_stamp_without_a_short_hash_derives_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / bi.STAMP_NAME).write_text(json.dumps({"commit": "b" * 40}), encoding="utf-8")
    _pretend_installed_at(monkeypatch, tmp_path)

    assert bi.build_info().short_commit == "bbbbbbb"
    assert bi.build_info().version == "bbbbbbb"


def test_untagged_stamp_uses_the_commit_instead_of_an_old_package_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / bi.STAMP_NAME).write_text(json.dumps({
        "version": "5.0.0", "commit": "abcdef0123456789", "short_commit": "abcdef0",
    }), encoding="utf-8")
    _pretend_installed_at(monkeypatch, tmp_path)

    assert bi.build_info().version == "abcdef0"


@pytest.mark.parametrize("content", ["not json at all", '"a string"', "[]", ""])
def test_an_unusable_stamp_is_ignored_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    """A corrupt version file must never be the reason the Studio will not start."""
    (tmp_path / bi.STAMP_NAME).write_text(content, encoding="utf-8")
    _pretend_installed_at(monkeypatch, tmp_path)

    assert bi.build_info().display == __version__


@requires_git
def test_stamp_payload_is_what_the_build_hook_writes(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    """It must round-trip: the hook writes it, an installed package reads it back."""
    subprocess.run(["git", "tag", "v0.3.0"], cwd=studio_checkout, check=True, capture_output=True)
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")
    payload = bi.stamp_payload()

    assert payload["commit"] and payload["branch"] == "main"
    assert payload["tag"] == "v0.3.0"
    assert payload["version"] == "0.3.0"
    assert "source" not in payload, "the reader knows it came from a stamp"

    elsewhere = studio_checkout.parent / "installed"
    elsewhere.mkdir()
    (elsewhere / bi.STAMP_NAME).write_text(json.dumps(payload), encoding="utf-8")
    _pretend_installed_at(monkeypatch, elsewhere)
    bi.build_info.cache_clear()

    reread = bi.build_info()
    assert reread.source == "stamp"
    assert reread.commit == payload["commit"]
    assert reread.display == "v0.3.0"
    assert reread.version == "0.3.0"


@requires_git
def test_installed_source_identity_changes_without_rewriting_the_running_identity(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")
    running = bi.build_info()
    assert running.version == running.short_commit
    subprocess.run(["git", "tag", "v5.2.0"], cwd=studio_checkout, check=True, capture_output=True)

    assert bi.read_build_info().version == "5.2.0"
    assert bi.build_info() is running


def test_installed_package_fallback_is_read_from_disk_without_using_the_imported_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pretend_installed_at(monkeypatch, tmp_path)
    (tmp_path / "__init__.py").write_text('__version__ = "8.0.0"\n', encoding="utf-8")
    assert bi.read_build_info().version == "8.0.0"


def test_stamp_payload_is_empty_without_a_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing to say, nothing written — the wheel is not given a stamp full of nulls."""
    _pretend_installed_at(monkeypatch, tmp_path)
    assert bi.stamp_payload() == {}


# ------------------------------------------------------------------------------ the API


def test_the_project_payload_and_healthz_agree_on_the_build(client: TestClient) -> None:
    version = client.get("/api/project").json()["version"]
    health = client.get("/healthz").json()

    assert version["version"] == bi.build_info().version
    assert version["display"] == (version["tag"] or version["short_commit"] or __version__)
    assert set(version) >= {"commit", "short_commit", "tag", "branch", "committed_at", "dirty"}
    assert health["version"] == version["version"]
    assert health["build"] == version["display"]
    assert health["commit"] == version["commit"]


@requires_git
def test_tagged_health_and_project_api_report_the_release_runtime_version(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path, project: Path
) -> None:
    from voxarium_studio.main import create_app

    subprocess.run(["git", "tag", "v5.2.0"], cwd=studio_checkout, check=True, capture_output=True)
    _pretend_installed_at(monkeypatch, studio_checkout / "backend" / "voxarium_studio")
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        health = client.get("/healthz").json()
        version = client.get("/api/project").json()["version"]
    assert health["version"] == version["version"] == "5.2.0"
    assert health["build"] == version["display"] == "v5.2.0"


# ------------------------------------------------------------------------- the changelog


@requires_git
def test_the_changelog_is_read_from_the_checkout(
    monkeypatch: pytest.MonkeyPatch, studio_checkout: Path
) -> None:
    """A source tree's own file is the one being written; the packaged copy is a fallback."""
    package = studio_checkout / "backend" / "voxarium_studio"
    _pretend_installed_at(monkeypatch, package)
    (studio_checkout / bi.CHANGELOG_NAME).write_text("# Changelog\n\nfrom the tree\n", "utf-8")
    (package / bi.CHANGELOG_NAME).write_text("# Changelog\n\nfrom the wheel\n", "utf-8")

    assert bi.changelog_path() == studio_checkout / bi.CHANGELOG_NAME


def test_an_installed_build_reads_the_copy_beside_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wheel has no repository root — the file is force-included next to the code."""
    _pretend_installed_at(monkeypatch, tmp_path)
    (tmp_path / bi.CHANGELOG_NAME).write_text("# Changelog\n", encoding="utf-8")

    assert bi.changelog_path() == tmp_path / bi.CHANGELOG_NAME


def test_a_build_without_a_changelog_answers_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pretend_installed_at(monkeypatch, tmp_path)
    assert bi.changelog_path() is None


def test_this_checkout_ships_a_changelog() -> None:
    """The rail's button opens this file; nothing else notices if it is renamed away."""
    root = bi.studio_repo_root()
    if root is None:
        pytest.skip("installed rather than a source checkout")
    assert (root / bi.CHANGELOG_NAME).is_file()


def test_the_changelog_endpoint_serves_its_markdown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Markdown, not HTML: the client renders documents, and a second renderer here would
    be one more thing to keep in step with it."""
    file = tmp_path / bi.CHANGELOG_NAME
    file.write_text("# Changelog\n\n## v9.9.9\n\n- something\n", encoding="utf-8")
    monkeypatch.setattr("voxarium_studio.api.project.changelog_path", lambda: file)

    payload = client.get("/api/changelog").json()
    assert payload["path"] == str(file)
    assert payload["text"].startswith("# Changelog")


def test_a_build_without_a_changelog_says_so_rather_than_serving_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("voxarium_studio.api.project.changelog_path", lambda: None)

    response = client.get("/api/changelog")
    assert response.status_code == 404
    assert "changelog" in response.json()["detail"].lower()
