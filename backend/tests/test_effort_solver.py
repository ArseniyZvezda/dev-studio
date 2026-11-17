"""Deterministic Effort Solver scores, protected identity and legacy settings compatibility (§10.11)."""
import io
import json
import math
import subprocess
import sys

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voxarium_studio.main import create_app
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams.effort_solver import (
    DEFAULT_SYSTEM_PROMPT,
    LEGACY_SYSTEM_PROMPT,
    EFFORT_SOLVER_ID,
    calculate_effort,
    main,
    parse_scores,
    parse_evaluation,
    select_effort,
)

TEAM = "team_default"
SOLVER = f"{TEAM}__{EFFORT_SOLVER_ID}"
MEMBER_URL = f"/api/teams/{TEAM}/members/{SOLVER}"
SCORES = {"complexity": 9, "creativity": 6, "depth": 8, "coverage": 8}


def solver_member(client):
    return next(member for member in client.get(f"/api/teams/{TEAM}").json()["members"]
                if member["id"] == SOLVER)


@pytest.mark.parametrize("scores, expected", [
    (SCORES, 0.7947441256587715),
    (dict(complexity=1, creativity=1, depth=1, coverage=1), 0),
    (dict(complexity=1, creativity=1, depth=1, coverage=10), 0),
    (dict(complexity=10, creativity=10, depth=10, coverage=1), 1),
    (dict(complexity=10, creativity=10, depth=10, coverage=10), 1),
    (dict(complexity=1, creativity=1, depth=10, coverage=10), math.sqrt(0.5)),
    (dict(complexity=10, creativity=1, depth=1, coverage=10), math.sqrt(0.3)),
    (dict(complexity=1, creativity=10, depth=1, coverage=10), math.sqrt(0.2)),
    (dict(complexity=5, creativity=5, depth=5, coverage=10), 4 / 9),
    (dict(complexity=5, creativity=5, depth=5, coverage=1), 46 / 81),
    (dict(complexity=4, creativity=4, depth=4, coverage=10), 1 / 3),
    (dict(complexity=4, creativity=4, depth=4, coverage=1), 4 / 9),
])
def test_fixed_formula(scores, expected):
    assert calculate_effort(scores) == pytest.approx(expected)


@pytest.mark.parametrize("multiplier, expected", [
    (1, [1, 3, 4, 5]), (0.5, [1, 2, 3, 5]),
])
def test_curve_matches_requested_examples(multiplier, expected):
    assert [select_effort(w, multiplier) for w in (0, 0.6, 0.8, 1)] == expected


@pytest.mark.parametrize("weight, expected", [(0.1249, 1), (0.125, 2), (0.375, 3),
                                              (0.625, 4), (0.875, 5)])
def test_linear_selection_uses_half_up_rounding(weight, expected):
    assert select_effort(weight) == expected


@pytest.mark.parametrize("value", [0, -1, True, "0.5", None, math.inf, -math.inf, math.nan])
def test_curve_multiplier_must_be_finite_positive_number(value):
    with pytest.raises(ValueError):
        select_effort(0.6, value)


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "0.6", None, math.inf, math.nan])
def test_curve_rejects_invalid_weights(value):
    with pytest.raises(ValueError):
        select_effort(value)


def test_curve_is_monotonic_and_preserves_endpoints_even_for_extreme_multipliers():
    for multiplier in (1e-300, 0.1, 0.5, 1, 2, 10, 1e300):
        levels = [select_effort(i / 1000, multiplier) for i in range(1001)]
        assert levels == sorted(levels)
        assert levels[0] == 1 and levels[-1] == 5
    for i in range(101):
        weight = i / 100
        assert select_effort(weight, 0.5) <= select_effort(weight) <= select_effort(weight, 2)


def test_all_scores_produce_normalized_weights_and_retain_default_effort():
    from itertools import product

    for c, r, d, v in product(range(1, 11), repeat=4):
        weight = calculate_effort(dict(complexity=c, creativity=r, depth=d, coverage=v))
        assert 0 <= weight <= 1
        assert select_effort(weight) == math.floor(1 + 4 * weight + 0.5)


@pytest.mark.parametrize("value", [0, 11, 1.0, True, "5", None])
def test_scores_require_integers_in_range(value):
    with pytest.raises(ValidationError):
        calculate_effort({**SCORES, "depth": value})


def test_json_and_fenced_json_are_accepted_but_missing_or_extra_scores_fail():
    assert parse_scores(json.dumps(SCORES)).model_dump() == SCORES
    assert calculate_effort(f"```json\n{json.dumps(SCORES)}\n```") == calculate_effort(SCORES)
    with pytest.raises(ValidationError):
        parse_scores({"complexity": 5})
    with pytest.raises(ValidationError):
        parse_scores({**SCORES, "effort": 5})
    with pytest.raises(ValueError):
        parse_scores("Here are the scores: " + json.dumps(SCORES))


def test_cli_reads_stdin_and_prints_normalized_weight(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["effort_solver"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCORES)))
    main()
    assert float(capsys.readouterr().out) == calculate_effort(SCORES)


def test_package_cli_accepts_stdin_without_runtime_warnings():
    result = subprocess.run(
        [sys.executable, "-m", "voxarium_studio.effort_solver"],
        input=json.dumps(SCORES), text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout) == calculate_effort(SCORES)
    assert result.stderr == ""


def test_builtin_is_added_with_prompt_and_cannot_be_deleted_or_made_human(client):
    solver = solver_member(client)
    assert solver["builtin"] == "effort_solver"
    assert solver["kind"] == "agent"
    assert solver["responsibility"] == DEFAULT_SYSTEM_PROMPT
    assert "effort_solver_weights" not in solver
    renamed = client.patch(MEMBER_URL, json={"name": "Task scorer", "role_title": "Estimator"})
    assert renamed.status_code == 200
    assert client.patch(MEMBER_URL, json={"kind": "human"}).status_code == 400
    for query in ("", "?reassign_to_director=true"):
        assert client.delete(MEMBER_URL + query).status_code == 409


def test_removed_weights_are_absent_from_api_schema(project):
    schema = create_app(str(project)).openapi()["components"]["schemas"]
    assert "effort_solver_weights" not in schema["MemberUpdate"]["properties"]
    assert "effort_solver_weights" not in TeamMember.model_fields


def test_curve_multiplier_defaults_and_is_solver_only(client, project):
    assert solver_member(client)["effort_solver_curve_multiplier"] == 1.0
    ordinary = f"/api/teams/{TEAM}/members/{TEAM}__dev"
    assert client.patch(ordinary, json={"effort_solver_curve_multiplier": 2.0}).status_code == 400
    for value in (0.5, 2, 1.0):
        response = client.patch(MEMBER_URL, json={"effort_solver_curve_multiplier": value})
        assert response.status_code == 200
        assert response.json()["effort_solver_curve_multiplier"] == value
    roster = yaml.safe_load((project / ".voxarium_studio/teams/default-team.yaml").read_text("utf-8"))
    for member in roster["members"]:
        if member["id"] == EFFORT_SOLVER_ID:
            assert member["effort_solver_curve_multiplier"] == 1.0
        else:
            assert "effort_solver_curve_multiplier" not in member


@pytest.mark.parametrize("value", [True, False, "1.5", None, 0, -1, float("nan"),
                                  float("inf"), float("-inf"), [], {}])
def test_invalid_curve_multiplier_api_edits_are_atomic(client, project, value):
    before = solver_member(client)
    path = project / ".voxarium_studio/teams/default-team.yaml"
    yaml_before = path.read_bytes()
    response = client.patch(
        MEMBER_URL,
        content=json.dumps({"name": "Do not save", "effort_solver_curve_multiplier": value}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert solver_member(client) == before
    assert path.read_bytes() == yaml_before


@pytest.mark.parametrize("value", [True, "1.5", None, 0, -1, float("nan"), float("inf")])
def test_invalid_curve_multiplier_models_and_yaml(value):
    from voxarium_studio.teams.store import MemberDefinition

    fields = dict(id=EFFORT_SOLVER_ID, name="Scorer", role_title="Solver",
                  effort_solver_curve_multiplier=value)
    with pytest.raises(ValidationError):
        TeamMember.model_validate({**fields, "team_id": TEAM})
    with pytest.raises(ValidationError):
        MemberDefinition.model_validate(yaml.safe_load(yaml.safe_dump(fields)))


def test_solver_settings_survive_restart(project):
    settings = dict(model="custom-model", responsibility="Custom scorer instructions",
                    effort_solver_curve_multiplier=1.75)
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        assert client.patch(MEMBER_URL, json=settings).status_code == 200
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        saved = solver_member(client)
        assert all(saved[key] == value for key, value in settings.items())
    path = project / ".voxarium_studio/teams/default-team.yaml"
    roster = yaml.safe_load(path.read_text("utf-8"))
    solver = next(member for member in roster["members"] if member["id"] == EFFORT_SOLVER_ID)
    assert solver["effort_solver_curve_multiplier"] == 1.75
    solver["effort_solver_curve_multiplier"] = 0.625
    path.write_text(yaml.safe_dump(roster), encoding="utf-8")
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        assert solver_member(client)["effort_solver_curve_multiplier"] == 0.625
        assert client.patch(MEMBER_URL, json={"name": "YAML roundtrip"}).status_code == 200
    roster = yaml.safe_load(path.read_text("utf-8"))
    solver = next(member for member in roster["members"] if member["id"] == EFFORT_SOLVER_ID)
    assert solver["effort_solver_curve_multiplier"] == 0.625


async def test_yaml_restores_removed_solver_with_custom_settings_and_normalises_kind(ctx):
    path = ctx.teams.store.file_for(TEAM)
    roster = yaml.safe_load(path.read_text("utf-8"))
    solver = next(member for member in roster["members"] if member["id"] == EFFORT_SOLVER_ID)
    solver.update(kind="human", name="Custom scorer", responsibility="Custom instructions",
                  effort_solver_curve_multiplier=2.25,
                  effort_solver_weights=dict(complexity=1, creativity=1, depth=5, coverage=0))
    path.write_text(yaml.safe_dump(roster), encoding="utf-8")
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        saved = await session.get(TeamMember, SOLVER)
        assert saved.kind == "agent" and saved.builtin == "effort_solver"
    roster["members"] = [member for member in roster["members"] if member["id"] != EFFORT_SOLVER_ID]
    path.write_text(yaml.safe_dump(roster), encoding="utf-8")
    await ctx.teams.sync_definitions()
    restored = next(member for member in yaml.safe_load(path.read_text("utf-8"))["members"]
                    if member["id"] == EFFORT_SOLVER_ID)
    assert restored["name"] == "Custom scorer"
    assert restored["responsibility"] == "Custom instructions"
    assert restored["effort_solver_curve_multiplier"] == 2.25
    assert "effort_solver_weights" not in restored
    assert restored.get("kind", "agent") == "agent"
    empty = ctx.teams.store.directory / "empty.yaml"
    empty.write_text("id: empty\nname: Empty\nmembers: []\n", encoding="utf-8")
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        assert (await session.get(TeamMember, f"empty__{EFFORT_SOLVER_ID}")).kind == "agent"


async def test_legacy_database_weights_are_ignored(ctx):
    from sqlalchemy import text

    async with ctx.db._engine.begin() as connection:
        await connection.execute(text("ALTER TABLE team_member ADD COLUMN effort_solver_weights JSON"))
        await connection.execute(text(
            "UPDATE team_member SET effort_solver_weights = :weights WHERE id = :id"
        ), {"weights": json.dumps(dict(complexity=0, creativity=0, depth=0, coverage=1)),
            "id": SOLVER})
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, SOLVER)
        assert "effort_solver_weights" not in member.model_dump()


async def test_existing_database_gains_default_curve_multiplier(tmp_path):
    import sqlite3
    from sqlalchemy import text
    from voxarium_studio.core.db import Database

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE team_member (id TEXT PRIMARY KEY, team_id TEXT, name TEXT, role_title TEXT)")
        connection.execute("INSERT INTO team_member VALUES ('old', 'team', 'Old', 'Role')")
    db = Database(path)
    try:
        await db.create_all()
        async with db.session() as session:
            row = (await session.exec(text(
                "SELECT effort_solver_curve_multiplier, skills FROM team_member WHERE id = 'old'"
            ))).one()
        assert row.effort_solver_curve_multiplier == 1.0
        assert row.skills == "[]"
    finally:
        await db.close()


@pytest.mark.parametrize("model", [1, 2, 3])
@pytest.mark.parametrize("effort", [1, 2, 3, 4, 5])
def test_orchestrator_contract_accepts_independent_model_and_effort(model, effort):
    assert parse_evaluation(json.dumps({"model": model, "effort": effort})).model_dump() == {
        "model": model, "effort": effort,
    }


@pytest.mark.parametrize("value", [
    {}, SCORES, {"model": 1}, {"effort": 2}, {"model": 1, "effort": 3, "extra": 1},
    {"model": True, "effort": 1}, {"model": 1, "effort": False},
    {"model": 1.0, "effort": 2}, {"model": 1, "effort": 2.0},
    {"model": "2", "effort": 2}, {"model": 1, "effort": "2"},
    {"model": 0, "effort": 1}, {"model": 4, "effort": 1},
    {"model": 1, "effort": 0}, {"model": 1, "effort": 6},
    [], None, '```json\n{"model": 2, "effort": 4}\n```',
    '{"model": 1, "model": 2, "effort": 3}',
])
def test_orchestrator_contract_rejects_non_strict_or_legacy_outputs(value):
    with pytest.raises(ValueError):
        parse_evaluation(value)


def test_orchestrator_name_prompt_and_auto_recursion_guard(client):
    solver = solver_member(client)
    assert solver["name"] == solver["role_title"] == "Orchestrator"
    assert solver["responsibility"] == DEFAULT_SYSTEM_PROMPT
    assert "**Orchestrator Agent**" in DEFAULT_SYSTEM_PROMPT
    assert "exactly two integer fields: model (1–3) and effort (1–5)" in DEFAULT_SYSTEM_PROMPT
    assert client.patch(MEMBER_URL, json={"model": "auto"}).status_code == 400
    assert solver_member(client) == solver
    from voxarium_studio.teams.store import MemberDefinition
    with pytest.raises(ValidationError):
        MemberDefinition(id=EFFORT_SOLVER_ID, name="Orchestrator", model="auto")


@pytest.mark.parametrize("custom", [False, True])
async def test_roster_migrates_only_legacy_evaluator_defaults_and_is_idempotent(ctx, custom):
    path = ctx.teams.store.file_for(TEAM)
    roster = yaml.safe_load(path.read_text("utf-8"))
    solver = next(m for m in roster["members"] if m["id"] == EFFORT_SOLVER_ID)
    prompt = "My evaluator policy" if custom else LEGACY_SYSTEM_PROMPT
    name = "Custom evaluator" if custom else "Effort Solver"
    solver.update(name=name, role_title=name, responsibility=prompt)
    path.write_text("# Keep this roster comment\n" + yaml.safe_dump(roster), encoding="utf-8")
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, SOLVER)
        assert member.name == (name if custom else "Orchestrator")
        assert member.role_title == (name if custom else "Orchestrator")
        assert member.responsibility == (prompt if custom else DEFAULT_SYSTEM_PROMPT)
        assert member.builtin == "effort_solver"
    migrated = path.read_bytes()
    assert migrated.startswith(b"# Keep this roster comment")
    persisted = next(m for m in yaml.safe_load(migrated)["members"] if m["id"] == EFFORT_SOLVER_ID)
    assert persisted["responsibility"] == (prompt if custom else DEFAULT_SYSTEM_PROMPT)
    await ctx.teams.sync_definitions()
    assert path.read_bytes() == migrated
