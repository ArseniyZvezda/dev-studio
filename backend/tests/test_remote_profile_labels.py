"""Local labels (studio-design §3.1): strict input, literal legacy metadata.

All repository operations use the suite's synthetic ``studio_home``; URL strings
are metadata only. No transport, application server or public API is exercised.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from voxarium_studio.core.application_config import (
    ApplicationConfig,
    RemoteServerProfile,
    application_config_file,
    load_application_config,
    save_application_config,
)
from voxarium_studio.core.paths import HOME_ENV_VAR
from voxarium_studio.sync import credentials, profiles
from voxarium_studio.sync.profiles import ProfileConsumers, ProfileError, RemoteProfiles

PROFILE = "c503da2b-f8fe-4fb0-b945-86bc119495b3"
OTHER = "81ccda2b-f8fe-4fb0-b945-86bc119495b3"
URL = "https://labels.fixture.invalid/team"
OTHER_URL = "https://other.fixture.invalid/team"
CANARY = "VS107_SYNTHETIC_LABEL_CANARY"
KEY = "VS107_SYNTHETIC_KEY"

VALID_LABELS = [
    pytest.param("  Team  ", "Team", id="trim-ascii"),
    pytest.param("\u00a0Команда\u2003", "Команда", id="trim-unicode-spaces"),
    pytest.param("T", "T", id="one-code-point"),
    pytest.param("Team  one", "Team  one", id="internal-spaces-unchanged"),
    pytest.param("a" * 120, "a" * 120, id="120-ascii"),
    pytest.param("Я" * 120, "Я" * 120, id="120-cyrillic"),
    pytest.param("\U0001f600" * 120, "\U0001f600" * 120, id="120-astral-480-bytes"),
    pytest.param("e\u0301" * 60, "e\u0301" * 60, id="120-decomposed-no-nfc"),
    pytest.param("и\u0306" * 60, "и\u0306" * 60, id="120-cyrillic-decomposed-no-nfc"),
    pytest.param("  " + "Я" * 120 + "  ", "Я" * 120, id="bounds-after-trim"),
]
INVALID_LABELS = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces"),
    pytest.param("\u00a0\u2003", id="unicode-spaces"),
    pytest.param("a" * 121, id="121-ascii"),
    pytest.param("Я" * 121, id="121-cyrillic"),
    pytest.param("\U0001f600" * 121, id="121-astral"),
    pytest.param("e\u0301" * 60 + "e", id="121-decomposed"),
    pytest.param("e\u0301" * 61, id="nfc-must-not-shorten-input"),
    pytest.param(CANARY + "\nInjected", id="newline"),
    pytest.param("\t" + CANARY, id="leading-tab-not-trimmed-away"),
    pytest.param(CANARY + "\r", id="trailing-cr-not-trimmed-away"),
    pytest.param(CANARY + "\x00", id="nul"),
    pytest.param(CANARY + "\x7f", id="del"),
    pytest.param(CANARY + "\x85", id="c1-next-line"),
    pytest.param(None, id="none"),
    pytest.param(True, id="bool"),
    pytest.param(17, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param(b"Team", id="bytes-must-not-coerce"),
    pytest.param(bytearray(b"Team"), id="bytearray-must-not-coerce"),
    pytest.param([CANARY], id="list"),
    pytest.param({"name": CANARY}, id="mapping"),
]
LEGACY_LABELS = [
    pytest.param("  Legacy team  ", id="untrimmed"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces"),
    pytest.param("a" * 121, id="121-ascii"),
    pytest.param("Я" * 201, id="beyond-old-model-bound"),
    pytest.param("\U0001f600" * 121, id="beyond-wire-byte-limit"),
    pytest.param("e\u0301" * 61, id="decomposed-no-nfc"),
    pytest.param("Legacy\n\x00\x85team", id="controls"),
]


@pytest.fixture(autouse=True)
def no_remote_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)


@contextmanager
def _unused_profile(_profile_id: str):
    yield ProfileConsumers()


def _metadata(name: object, *, profile_id: str = PROFILE, url: str = URL) -> dict:
    return {"profile_id": profile_id, "name": name, "url": url, "revision": 7}


def _snapshot(home: Path) -> dict | None:
    """Every fixture entry, including lock/key bytes and directory/file mtimes."""
    if not home.exists():
        return None
    return {
        str(path.relative_to(home)): (
            path.is_dir(), path.stat().st_mtime_ns,
            None if path.is_dir() else path.read_bytes(),
        )
        for path in [home, *sorted(home.rglob("*"))]
    }


def _seed_legacy(label: str) -> None:
    credentials.save_profile_token(PROFILE, URL, KEY, expected_revision=0)
    raw = {
        "language": "ru", "theme": {"mode": "light", "future_theme_flag": 23},
        "future_section": {"flags": [None, False, {"value": "opaque"}]},
        "remote_servers": [_metadata(label), _metadata("Other", profile_id=OTHER, url=OTHER_URL)],
    }
    application_config_file().write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )


@pytest.mark.parametrize(("label", "expected"), VALID_LABELS)
def test_label_model_strict_input_trims_without_unicode_normalization(label, expected):
    profile = RemoteServerProfile.model_validate(_metadata(label))
    assert profile.name == expected
    assert 1 <= len(profile.name) <= 120
    # Application DTO construction is strict by default too, not a disk-load bypass.
    assert ApplicationConfig.model_validate({"remote_servers": [_metadata(label)]}).remote_servers == [profile]


@pytest.mark.parametrize("label", INVALID_LABELS)
def test_label_model_rejects_invalid_input_with_safe_errors(label, caplog):
    for context in (None, {"stored_profile_metadata": False}):
        with pytest.raises(ValidationError) as caught:
            RemoteServerProfile.model_validate(_metadata(label), context=context)
        assert CANARY not in str(caught.value) + repr(caught.value) + caplog.text


@pytest.mark.parametrize("codepoint", [*range(0x20), *range(0x7f, 0xa0)])
def test_label_model_rejects_every_c0_del_c1_control(codepoint):
    with pytest.raises(ValidationError):
        RemoteServerProfile.model_validate(_metadata("Team" + chr(codepoint) + "one"))


@pytest.mark.parametrize("label", LEGACY_LABELS)
def test_label_stored_metadata_context_preserves_literal_strings(label):
    context = {"stored_profile_metadata": True}
    profile = RemoteServerProfile.model_validate(_metadata(label), context=context)
    assert profile.name == label
    nested = ApplicationConfig.model_validate({"remote_servers": [_metadata(label)]}, context=context)
    assert nested.remote_servers == [profile]
    assert nested.model_dump()["remote_servers"][0]["name"] == label


@pytest.mark.parametrize("label", [None, 17, True, b"Team", ["Team"]])
def test_label_stored_context_does_not_bypass_string_type(label):
    with pytest.raises(ValidationError):
        RemoteServerProfile.model_validate(_metadata(label), context={"stored_profile_metadata": True})


def test_label_stored_context_does_not_bypass_other_metadata_validation():
    for changed in ({"revision": True}, {"profile_id": "not-a-uuid"},
                    {"url": "ftp://invalid"}, {"token": KEY}):
        with pytest.raises(ValidationError):
            RemoteServerProfile.model_validate(
                {**_metadata("  Legacy  "), **changed}, context={"stored_profile_metadata": True},
            )


@pytest.mark.parametrize(("label", "expected"), VALID_LABELS)
@pytest.mark.parametrize("operation", ["create", "rename"])
def test_label_repository_persists_strict_input_and_preserves_identity_keys(
    studio_home, label, expected, operation,
):
    repo = RemoteProfiles(consumer_guard=_unused_profile)
    if operation == "create":
        result = repo.create(label, URL)
        assert result.revision == 1
        assert not credentials.keys_file().exists()
    else:
        old = repo.create("Original", URL)
        old = repo.set_credential(old.profile_id, KEY, expected_revision=1)
        keys_before = (credentials.keys_file().read_bytes(), credentials.keys_file().stat().st_mtime_ns)
        result = repo.rename(old.profile_id, label, expected_revision=old.revision)
        assert (result.profile_id, result.url, result.revision) == (old.profile_id, old.url, old.revision + 1)
        assert (credentials.keys_file().read_bytes(), credentials.keys_file().stat().st_mtime_ns) == keys_before
    assert result.name == expected
    assert repo.get(result.profile_id) == result
    assert load_application_config().remote_servers == [result]
    assert yaml.safe_load(application_config_file().read_text(encoding="utf-8"))["remote_servers"] == [result.model_dump()]


@pytest.mark.parametrize("label", INVALID_LABELS)
@pytest.mark.parametrize("operation", ["create", "rename"])
@pytest.mark.parametrize("home_state", ["absent", "empty", "populated"])
def test_label_invalid_repository_input_is_entire_home_noop(
    studio_home, monkeypatch, label, operation, home_state, caplog,
):
    home = studio_home / "isolated-label-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    repo = RemoteProfiles(consumer_guard=_unused_profile)
    if home_state != "absent":
        home.mkdir()
    if home_state == "populated":
        _seed_legacy("  Untouched legacy  ")
        (home / "opaque.bin").write_bytes(b"\x00VS107 opaque fixture\xff")
    before = _snapshot(home)
    with pytest.raises(ProfileError) as caught:
        if operation == "create":
            repo.create(label, "https://new.fixture.invalid/team")
        else:
            repo.rename(PROFILE, label, expected_revision=7)
    assert str(caught.value) == "invalid_profile_metadata"
    assert CANARY not in str(caught.value) + repr(caught.value) + caplog.text
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    assert _snapshot(home) == before
    if home_state == "populated":
        assert repo.get(PROFILE).revision == 7
        assert credentials.profile_token_revision(PROFILE, URL) == 1
        assert credentials.load_profile_token(PROFILE, URL) == KEY
        assert _snapshot(home) == before


@pytest.mark.parametrize("operation", ["create", "rename"])
def test_label_validation_precedes_transaction_and_disk_reads(monkeypatch, operation):
    def prohibited(*_args, **_kwargs):
        pytest.fail("invalid label must not enter the personal-store transaction or read disk")

    monkeypatch.setattr(profiles, "application_config_transaction", prohibited)
    monkeypatch.setattr(RemoteProfiles, "_read", prohibited)
    repo = RemoteProfiles()
    with pytest.raises(ProfileError, match="^invalid_profile_metadata$"):
        if operation == "create":
            repo.create(CANARY + "\nInjected", URL)
        else:
            repo.rename(PROFILE, CANARY + "\nInjected", expected_revision=7)


@pytest.mark.parametrize("label", LEGACY_LABELS)
def test_label_legacy_diagnostic_reads_are_literal_and_whole_home_readonly(studio_home, label):
    _seed_legacy(label)
    before = _snapshot(studio_home)
    repo = RemoteProfiles()
    assert repo.get(PROFILE).name == label
    assert repo.list()[0].name == label
    loaded = load_application_config()
    assert loaded.language == "ru" and loaded.theme.mode == "light"
    assert loaded.remote_servers[0].name == label
    assert _snapshot(studio_home) == before


@pytest.mark.parametrize("label", LEGACY_LABELS)
@pytest.mark.parametrize("operation", ["preferences", "create", "rename-other", "set", "clear", "url"])
def test_label_legacy_roundtrip_preserves_literal_label_and_opaque_settings(
    studio_home, label, operation,
):
    _seed_legacy(label)
    repo = RemoteProfiles(consumer_guard=_unused_profile)
    before = yaml.safe_load(application_config_file().read_text(encoding="utf-8"))
    stale_preferences = load_application_config()
    keys_before = (credentials.keys_file().read_bytes(), credentials.keys_file().stat().st_mtime_ns)
    if operation == "preferences":
        stale_preferences.language = "en"
        save_application_config(stale_preferences)
    elif operation == "create":
        repo.create("Third", "https://third.fixture.invalid/team")
    elif operation == "rename-other":
        repo.rename(OTHER, "  Renamed other  ", expected_revision=7)
    elif operation == "set":
        repo.set_credential(PROFILE, KEY + "_replacement", expected_revision=7)
    elif operation == "clear":
        repo.clear_credential(PROFILE, expected_revision=7)
    else:
        repo.change_url(PROFILE, "https://changed.fixture.invalid/team", expected_revision=7,
                        clear_credential=True)
    after = yaml.safe_load(application_config_file().read_text(encoding="utf-8"))
    stored = next(p for p in after["remote_servers"] if p["profile_id"] == PROFILE)
    assert stored["name"] == label
    assert repo.get(PROFILE).name == label
    assert load_application_config().remote_servers[0].name == label
    assert after["future_section"] == before["future_section"]
    assert after["theme"]["future_theme_flag"] == before["theme"]["future_theme_flag"]
    assert after["theme"]["mode"] == "light"
    assert after["language"] == ("en" if operation == "preferences" else "ru")
    if operation in ("preferences", "create", "rename-other"):
        assert stored == before["remote_servers"][0]
        assert (credentials.keys_file().read_bytes(), credentials.keys_file().stat().st_mtime_ns) == keys_before
    else:
        assert stored["revision"] == 8
        assert stored["url"] == ("https://changed.fixture.invalid/team" if operation == "url" else URL)
    # A stale preference save must not put the old registry or revisions back.
    registry = after["remote_servers"]
    stale_preferences.language = "en"
    save_application_config(stale_preferences)
    assert yaml.safe_load(application_config_file().read_text(encoding="utf-8"))["remote_servers"] == registry


@pytest.mark.parametrize("label", LEGACY_LABELS)
def test_label_explicit_rename_validates_and_replaces_legacy_label(studio_home, label):
    _seed_legacy(label)
    repo = RemoteProfiles()
    before = _snapshot(studio_home)
    with pytest.raises(ProfileError, match="^invalid_profile_metadata$"):
        repo.rename(PROFILE, "z" * 121, expected_revision=7)
    assert _snapshot(studio_home) == before
    renamed = repo.rename(PROFILE, "  New label  ", expected_revision=7)
    assert (renamed.name, renamed.profile_id, renamed.url, renamed.revision) == ("New label", PROFILE, URL, 8)
    assert repo.get(PROFILE) == renamed
