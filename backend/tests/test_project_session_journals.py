"""Default-auto native journal reads require captured project/launch evidence (§5.3.2)."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from voxarium_studio.core.project import init_project
from voxarium_studio.core.project_scope import canonical_root, resolve_project
from voxarium_studio.main import create_app
from voxarium_studio.models import AgentChat, TeamMember, TeamTask
from voxarium_studio.models.task_context import ChatSessionContext, TaskContext
from voxarium_studio.teams.backends.base import AgentRunRequest, RuntimeSessionKey, runtime_session_owner
from voxarium_studio.teams.context_journals import dispatch_marker
from voxarium_studio.teams.context_viewer import capture_request, fragment_key

OWNER, MEMBER, SID = "copied_owner", "team_default__dev", "copied-native-session"


@pytest.fixture
async def journal_projects(project, tmp_path, monkeypatch):
    home = tmp_path / "native-home"
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "QWEN_RUNTIME_DIR"):
        monkeypatch.setenv(variable, str(home))
    other = tmp_path / "other"
    other.mkdir()
    init_project(other)
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            scopes = []
            for label, root in (("A", project), ("B", other)):
                response = await http.post("/api/projects/open", json={"path": str(root)})
                assert response.status_code == 200, response.text
                descriptor = response.json()
                ctx = resolve_project(app.state, descriptor["project_id"]).ctx
                async with ctx.db.session() as session:
                    session.add(TeamTask(id=OWNER, number=1000, team_id="team_default", assignee_id=MEMBER,
                                         title=label, auto_start=False, agent_session_id=SID))
                    session.add(AgentChat(id=OWNER, member_id=MEMBER, agent_session_id=SID))
                scopes.append(SimpleNamespace(label=label, ctx=ctx,
                                               prefix=f"/api/projects/{descriptor['project_id']}"))
            yield http, scopes, home


def _url(scope, kind):
    owner = f"teams/team_default/tasks/{OWNER}" if kind == "task" else f"chats/{OWNER}"
    return f"{scope.prefix}/{owner}/context"


async def _capture(scope, kind, backend, workdir):
    async with scope.ctx.db.session() as session:
        member = await session.get(TeamMember, MEMBER)
        member.agent_backend = backend
        session.add(member)
        task = await session.get(TeamTask, OWNER) if kind == "task" else None
    request = AgentRunRequest(member=member, task=task, session_key=OWNER, workdir=workdir,
                              system_prompt=f"{scope.label} captured system", prompt=f"{scope.label} Studio fallback",
                              runtime_key=RuntimeSessionKey(runtime_session_owner(scope.ctx.paths.root, kind, OWNER)))
    await capture_request(scope.ctx.db, OWNER, request)
    return request


def _native_message(backend, text, *, role="user"):
    if backend == "codex":
        return {"type": "response_item", "payload": {"type": "message", "role": role, "content": text}}
    if backend == "claude_code":
        return {"message": {"role": role, "content": text}}
    return {"message": {"role": "model" if role == "assistant" else role, "parts": [{"text": text}]}}


def _journal(home, backend, label, workdir, *, binding="valid", text=None, dispatch=None):
    if backend == "codex":
        path = home / "sessions" / label / f"rollout-test-{SID}.jsonl"
        metadata = {"type": "session_meta", "payload": {"cwd": str(workdir)}}
        if binding == "missing":
            metadata["payload"].clear()
        elif binding == "relative":
            metadata["payload"]["cwd"] = "relative"
    else:
        suffix = f"projects/{label}/{SID}.jsonl" if backend == "claude_code" else f"projects/{label}/chats/{SID}.jsonl"
        path = home / suffix
        metadata = {"cwd": str(workdir)}
        if binding == "missing":
            metadata.clear()
        elif binding == "relative":
            metadata["cwd"] = "relative"
    rows = [metadata]
    if dispatch is not None:
        rows.append(_native_message(backend, dispatch))
    rows.append(_native_message(backend, text or f"{label} PRIVATE NATIVE",
                                role="assistant" if dispatch is not None else "user"))
    if binding == "conflicting":
        extra = {"cwd": str(workdir.parent / "unrelated")}
        rows.append({"type": "turn_context", "payload": extra} if backend == "codex" else extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf8")
    return path


async def _read_with_fragment(http, url, expected, *, native):
    response = await http.get(url)  # Deliberately omit view: the frontend defaults to auto.
    assert response.status_code == 200, response.text
    data = response.json()
    assert bool(data.get("journal_path")) is native
    parts = data["sources"]
    part = next(item for item in parts if item["text"] == expected)
    response = await http.get(f"{url}/fragments/{part['id']}", params={"key": fragment_key(part)})
    assert response.status_code == 200, response.text
    assert response.json() == {"text": expected}
    assert "runtime_provenance" not in str(data), "Internal binding is metadata, never visible source text"
    return data


@pytest.mark.parametrize("backend", ["claude_code", "codex", "qwen"])
@pytest.mark.parametrize("kind", ["task", "chat"])
@pytest.mark.parametrize("route", ["project", "legacy"])
@pytest.mark.parametrize("read", ["inventory", "fragment"])
@pytest.mark.parametrize("evidence", ["cwd-only-legacy-snapshot", "foreign-dispatch"])
async def test_shared_external_workdir_and_copied_sid_do_not_authorize_other_project_journal(
    journal_projects, tmp_path, backend, kind, route, read, evidence,
):
    http, (a, b), home = journal_projects
    shared = tmp_path / "shared-external-workdir"
    shared.mkdir()
    assert not shared.is_relative_to(a.ctx.paths.root)
    assert not shared.is_relative_to(b.ctx.paths.root)
    # Both databases already carry the same owner and native SID. Even genuine
    # captures for that SID and cwd cannot distinguish projects sharing a workdir.
    for scope in (a, b):
        request = await _capture(scope, kind, backend, shared)
        if evidence == "cwd-only-legacy-snapshot":
            async with scope.ctx.db.session() as session:
                snapshot = await session.get(TaskContext if kind == "task" else ChatSessionContext, OWNER)
                sources = [dict(part) for part in snapshot.sources]
                sources[-1]["runtime_provenance"] = {
                    "version": 1, "project_root": str(canonical_root(scope.ctx.paths.root)),
                    "workdir": str(canonical_root(shared)),
                }
                snapshot.sources = sources
                session.add(snapshot)
    # Only B has a native journal. A has a valid, project-bound Studio snapshot,
    # not a copied or obsolete capture, so failing closed must retain A's text.
    _journal(home, backend, "B", shared,
             dispatch=request.prompt if evidence == "foreign-dispatch" else None)
    if route == "legacy":
        response = await http.post("/api/project/open", json={"path": str(a.ctx.paths.root)})
        assert response.status_code == 200, response.text
        url = _url(SimpleNamespace(prefix="/api"), kind)
    else:
        # B remains selected: the immutable project route must still use A.
        url = _url(a, kind)
    expected = "A Studio fallback"
    if read == "inventory":
        response = await http.get(url)  # No view parameter: default auto, not Studio opt-in.
        assert response.status_code == 200, response.text
        data = response.json()
        assert "B PRIVATE NATIVE" not in response.text
        assert data["origin"] == "studio"
        assert not data.get("journal_path")
        assert expected in [part["text"] for part in data["sources"]]
    else:
        # Obtain a legitimate deferred-read key independently of default-auto
        # inventory, which can already be compromised by the foreign journal.
        response = await http.get(url, params={"view": "studio"})
        assert response.status_code == 200, response.text
        part = next(part for part in response.json()["sources"] if part["text"] == expected)
        response = await http.get(f"{url}/fragments/{part['id']}", params={"key": fragment_key(part)})
        assert "B PRIVATE NATIVE" not in response.text
        assert response.status_code == 200, response.text
        assert response.json() == {"text": expected}


@pytest.mark.parametrize("kind", ["task", "chat"])
@pytest.mark.parametrize("route", ["project", "legacy"])
@pytest.mark.parametrize("read", ["inventory", "fragment"])
async def test_qwen_memory_catalog_after_own_genesis_falls_back_without_foreign_user_input(
    journal_projects, tmp_path, kind, route, read,
):
    http, (a, b), home = journal_projects
    shared = tmp_path / "shared-catalog-workdir"
    shared.mkdir()
    assert not shared.is_relative_to(a.ctx.paths.root)
    assert not shared.is_relative_to(b.ctx.paths.root)
    own = await _capture(a, kind, "qwen", shared)
    await _capture(b, kind, "qwen", shared)
    # A genuinely dispatched the genesis. A linked system catalog is nevertheless
    # unknown startup content, not authenticated by that earlier receipt (§5.3.2).
    # No foreign user input is present to trigger ordinary dispatch rejection.
    genesis = {"uuid": "own-genesis", "parentUuid": None, **_native_message("qwen", own.prompt)}
    catalog = {
        "uuid": "foreign-catalog", "parentUuid": "own-genesis",
        "type": "system", "subtype": "memory_catalog_snapshot",
        "message": {"role": "user", "parts": [{
            "text": "B PRIVATE MEMORY CATALOG",
            "partMetadata": {"qwen-code:memory-catalog-snapshot": True},
        }]},
    }
    path = _journal(home, "qwen", "mixed-catalog", shared, dispatch=own.prompt)
    rows = [{"cwd": str(shared), "sessionId": SID}, genesis, catalog]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf8")
    if route == "legacy":
        response = await http.post("/api/project/open", json={"path": str(a.ctx.paths.root)})
        assert response.status_code == 200, response.text
        url = _url(SimpleNamespace(prefix="/api"), kind)
    else:
        # B remains selected; project-scoped reads must still bind to A.
        url = _url(a, kind)
    expected = "A Studio fallback"
    if read == "inventory":
        response = await http.get(url)  # Default auto, never an explicit Studio opt-in.
        assert response.status_code == 200, response.text
        assert "B PRIVATE MEMORY CATALOG" not in response.text
        data = response.json()
        assert data["origin"] == "studio"
        assert not data.get("journal_path")
        assert expected in [part["text"] for part in data["sources"]]
    else:
        # Read the deferred key from Studio independently: compromised auto
        # inventory must not hide a fragment-endpoint regression.
        response = await http.get(url, params={"view": "studio"})
        assert response.status_code == 200, response.text
        assert "B PRIVATE MEMORY CATALOG" not in response.text
        part = next(part for part in response.json()["sources"] if part["text"] == expected)
        response = await http.get(f"{url}/fragments/{part['id']}", params={"key": fragment_key(part)})
        assert "B PRIVATE MEMORY CATALOG" not in response.text
        assert response.status_code == 200, response.text
        assert response.json() == {"text": expected}


@pytest.mark.parametrize("backend", ["claude_code", "codex", "qwen"])
@pytest.mark.parametrize("kind", ["task", "chat"])
async def test_default_auto_and_fragments_choose_own_copied_id_journal_even_outside_root(
    journal_projects, tmp_path, backend, kind,
):
    http, (a, b), home = journal_projects
    # B really launched outside its project (also covers an isolated checkout);
    # its binding must come from capture, not a guessed project-directory rule.
    workdirs = (a.ctx.paths.root, tmp_path / "external-launch-workdir")
    workdirs[1].mkdir()
    for scope, workdir in zip((a, b), workdirs, strict=True):
        request = await _capture(scope, kind, backend, workdir)
        _journal(home, backend, scope.label, workdir, dispatch=request.prompt)
    assert (await http.post("/api/project/open", json={"path": str(b.ctx.paths.root)})).status_code == 200
    for scope in (a, b):
        await _read_with_fragment(http, _url(scope, kind), f"{scope.label} PRIVATE NATIVE", native=True)
    await _read_with_fragment(http, _url(SimpleNamespace(prefix="/api"), kind), "B PRIVATE NATIVE", native=True)


@pytest.mark.parametrize("backend", ["claude_code", "codex", "qwen"])
@pytest.mark.parametrize("kind", ["task", "chat"])
async def test_dispatch_proof_selects_own_journal_with_copied_sid_and_shared_workdir(
    journal_projects, tmp_path, backend, kind,
):
    http, scopes, home = journal_projects
    shared = tmp_path / "shared-native-workdir"
    shared.mkdir()
    requests = [await _capture(scope, kind, backend, shared) for scope in scopes]
    assert requests[0].prompt.splitlines()[0] != requests[1].prompt.splitlines()[0]
    for scope, request in zip(scopes, requests, strict=True):
        _journal(home, backend, scope.label, shared, dispatch=request.prompt)
    for scope in scopes:
        data = await _read_with_fragment(http, _url(scope, kind), f"{scope.label} PRIVATE NATIVE", native=True)
        foreign = "B" if scope.label == "A" else "A"
        assert f"{foreign} PRIVATE NATIVE" not in str(data)
        response = await http.post("/api/project/open", json={"path": str(scope.ctx.paths.root)})
        assert response.status_code == 200, response.text
        data = await _read_with_fragment(http, _url(SimpleNamespace(prefix="/api"), kind),
                                         f"{scope.label} PRIVATE NATIVE", native=True)
        assert f"{foreign} PRIVATE NATIVE" not in str(data)


@pytest.mark.parametrize("backend", ["claude_code", "codex", "qwen"])
@pytest.mark.parametrize("kind", ["task", "chat"])
@pytest.mark.parametrize("history", ["foreign-first", "foreign-middle"])
async def test_mixed_foreign_history_is_not_authorized_by_latest_own_dispatch(
    journal_projects, tmp_path, backend, kind, history,
):
    http, (a, b), home = journal_projects
    shared = tmp_path / "shared-mixed-workdir"
    shared.mkdir()
    own = await _capture(a, kind, backend, shared)
    foreign = await _capture(b, kind, backend, shared)
    genesis = own.prompt
    own.resume_session_id = SID
    own.prompt = "A resumed fallback"
    await capture_request(a.ctx.db, OWNER, own)
    first = foreign.prompt if history == "foreign-first" else genesis
    path = _journal(home, backend, "mixed", shared, text="B PRIVATE NATIVE", dispatch=first)
    with path.open("a", encoding="utf8") as stream:
        if history == "foreign-middle":
            stream.write(json.dumps(_native_message(backend, foreign.prompt)) + "\n")
        stream.write(json.dumps(_native_message(backend, own.prompt)) + "\n")
        stream.write(json.dumps(_native_message(backend, "A latest native", role="assistant")) + "\n")
    data = await _read_with_fragment(http, _url(a, kind), "A resumed fallback", native=False)
    assert "B PRIVATE NATIVE" not in str(data)
    assert "A latest native" not in str(data)
    response = await http.post("/api/project/open", json={"path": str(a.ctx.paths.root)})
    assert response.status_code == 200, response.text
    data = await _read_with_fragment(http, _url(SimpleNamespace(prefix="/api"), kind),
                                     "A resumed fallback", native=False)
    assert "B PRIVATE NATIVE" not in str(data)


@pytest.mark.parametrize("backend", ["claude_code", "codex", "qwen"])
@pytest.mark.parametrize("failure", ["missing", "relative", "conflicting", "wrong-cwd", "ambiguous", "old-snapshot", "copied-snapshot", "cwd-only", "legacy-snapshot"])
async def test_unproven_or_ambiguous_native_binding_falls_back_for_auto_and_fragments(
    journal_projects, backend, failure,
):
    http, (a, b), home = journal_projects
    requests = {}
    for scope in (a, b):
        requests[scope.label] = await _capture(scope, "task", backend, scope.ctx.paths.root)
    target = b if failure == "copied-snapshot" else a
    workdir = target.ctx.paths.root
    _journal(home, backend, "candidate", workdir.parent / "wrong" if failure == "wrong-cwd" else workdir,
             binding=failure, dispatch=None if failure == "cwd-only" else requests[target.label].prompt)
    if failure == "ambiguous":
        _journal(home, backend, "duplicate", workdir, dispatch=requests[target.label].prompt)
    expected = f"{target.label} Studio fallback"
    if failure in {"old-snapshot", "copied-snapshot", "legacy-snapshot"}:
        copied = None
        if failure == "copied-snapshot":
            async with a.ctx.db.session() as session:
                copied = [dict(part) for part in (await session.get(TaskContext, OWNER)).sources]
            expected = "A Studio fallback"
        async with target.ctx.db.session() as session:
            snapshot = await session.get(TaskContext, OWNER)
            sources = [dict(part) for part in snapshot.sources]
            if failure == "old-snapshot":
                sources[-1].pop("runtime_provenance")
            elif failure == "legacy-snapshot":
                sources[-1]["runtime_provenance"] = {
                    "version": 1, "project_root": str(canonical_root(target.ctx.paths.root)),
                    "workdir": str(canonical_root(workdir)),
                }
            else:
                sources = copied
                # This native file fits the copied A evidence, not current B.
                _journal(home, backend, "original-A", a.ctx.paths.root, dispatch=requests["A"].prompt)
            snapshot.sources = sources
            session.add(snapshot)
    await _read_with_fragment(http, _url(target, "task"), expected, native=False)


async def test_capture_refreshes_binding_on_resume_without_rewriting_text_or_session_key(journal_projects, tmp_path):
    _, (a, _), _ = journal_projects
    request = await _capture(a, "chat", "codex", a.ctx.paths.root)
    initial_dispatch = request.prompt
    async with a.ctx.db.session() as session:
        initial = (await session.get(ChatSessionContext, OWNER)).sources[-1]["runtime_provenance"]
    assert re.fullmatch(r"[0-9a-f]{32}", initial["token"])
    assert initial_dispatch == dispatch_marker(initial["token"]) + "A Studio fallback"
    request.resume_session_id = SID
    request.workdir = tmp_path / "new-isolated-workdir"
    request.prompt = "resumed input"
    await capture_request(a.ctx.db, OWNER, request)
    async with a.ctx.db.session() as session:
        snapshot = await session.get(ChatSessionContext, OWNER)
    assert snapshot.sources[-1]["text"] == "resumed input"
    provenance = snapshot.sources[-1]["runtime_provenance"]
    assert provenance["version"] == 2
    assert provenance["project_root"] == str(canonical_root(a.ctx.paths.root))
    assert provenance["workdir"] == str(canonical_root(request.workdir))
    assert provenance["owner_kind"] == "chat"
    assert provenance["owner_id"] == OWNER
    assert provenance["generation"] == initial["generation"]
    assert provenance["token"] == initial["token"]
    assert request.prompt == dispatch_marker(initial["token"]) + "resumed input"
    assert provenance["dispatches"] == [hashlib.sha256(text.encode("utf8")).hexdigest()
                                        for text in (initial_dispatch, request.prompt)]
    task_texts = [part["text"] for part in snapshot.sources if part["category"] == "task"]
    assert task_texts == [dispatch_marker(initial["token"]), "A Studio fallback",
                          dispatch_marker(initial["token"]), "resumed input"]
    assert request.session_key == OWNER


@pytest.mark.parametrize("backend", ["claude_code", "codex", "qwen"])
@pytest.mark.parametrize("field,value", [("owner_kind", "task"), ("owner_id", "another-owner")])
async def test_native_receipt_copied_to_other_owner_is_not_authorized(journal_projects, backend, field, value):
    http, (a, _), home = journal_projects
    request = await _capture(a, "chat", backend, a.ctx.paths.root)
    _journal(home, backend, "candidate", request.workdir, dispatch=request.prompt)
    async with a.ctx.db.session() as session:
        snapshot = await session.get(ChatSessionContext, OWNER)
        sources = [dict(part) for part in snapshot.sources]
        sources[-1]["runtime_provenance"] = {**sources[-1]["runtime_provenance"], field: value}
        snapshot.sources = sources
        session.add(snapshot)
    await _read_with_fragment(http, _url(a, "chat"), "A Studio fallback", native=False)


@pytest.mark.parametrize("kind", ["task", "chat"])
async def test_first_captured_resume_does_not_adopt_native_history(journal_projects, kind):
    http, (a, _), home = journal_projects
    request = await _capture(a, kind, "codex", a.ctx.paths.root)
    _journal(home, "codex", "candidate", request.workdir, dispatch=request.prompt)
    model = TaskContext if kind == "task" else ChatSessionContext
    async with a.ctx.db.session() as session:
        await session.delete(await session.get(model, OWNER))
    request.resume_session_id = SID
    request.prompt = "unproven resume"
    await capture_request(a.ctx.db, OWNER, request)
    assert request.prompt == "unproven resume"
    await _read_with_fragment(http, _url(a, kind), "unproven resume", native=False)


@pytest.mark.parametrize("kind", ["task", "chat"])
async def test_recovery_capture_does_not_fabricate_a_dispatched_receipt(journal_projects, kind):
    http, (a, _), home = journal_projects
    request = await _capture(a, kind, "codex", a.ctx.paths.root)
    _journal(home, "codex", "candidate", request.workdir, dispatch=request.prompt)
    recovery = replace(request, prompt="already dispatched recovery", resume_session_id=None)
    await capture_request(a.ctx.db, OWNER, recovery)
    assert recovery.prompt == "already dispatched recovery"
    await _read_with_fragment(http, _url(a, kind), "already dispatched recovery", native=False)
