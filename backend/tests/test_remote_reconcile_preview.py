"""I11: read-only reconcile preview against the installed, in-process Remote v2.

remote-identity/runtime-integration-plan §4 I11; studio-design §5.2/§6;
server-design §3.3. The Studio app, its catalog owner, the production
CatalogHttpTransport and the Remote app are real; ASGI replaces only the
network hop. A missing Remote is a failed prerequisite, never a skip.
"""
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_remote_catalog_activation import (
    REAL, secret_free, seed, select_prepare, select_ready, select_studio,
)
from voxarium_studio.api.sync import recheck_reconcile_preview
from voxarium_studio.sync.protocol import content_hash
from voxarium_studio.sync.state import Base, SyncNamespace, SyncStateV2
from voxarium_studio.sync.transitions import (
    ReconcilePreviewError, ReconcilePreviews, classify_reconcile,
)


COUNTS = ("upload", "download", "delete_local", "delete_remote", "conflict", "unchanged")


def tree(*roots: Path) -> dict[str, str]:
    """Every persisted byte that a preview must not touch (docs/config/home/state)."""
    result = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.name.endswith(("-wal", "-shm", "-journal")):
                if path.suffix in {".sqlite", ".sqlite3", ".db", ".log"}:
                    continue
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@asynccontextmanager
async def selected(project, monkeypatch, home):
    """A committed (disabled, selected) binding to the real Remote project."""
    from voxarium_studio.teams.docs import DocStore

    async with select_studio(project, monkeypatch, home) as s:
        response = await select_prepare(s)
        assert response.status_code == 200, response.text
        url, body = await select_ready(s, response.json())
        response = await s.http.post(url, json=body)
        assert response.status_code == 200, response.text
        s.base = f"/api/projects/{s.scope['project_id']}/remote"
        s.docs_dir = Path(s.ctx.docs.directory)
        s.calls.clear()

        def denied(*_args, **_kwargs):
            raise AssertionError("preview must never write through DocStore")

        for name in ("write", "delete", "move", "create", "rename"):
            if hasattr(DocStore, name):
                monkeypatch.setattr(DocStore, name, denied)
        yield s


async def preview(s, **changes):
    observation = (await s.http.get(s.base)).json()
    body = {"expected_revision": observation["identity_revision"],
            "config_fingerprint": observation["config_fingerprint"], **changes}
    s.calls.clear()
    return await s.http.post(s.base + "/reconcile-preview", json=body)


def refused(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    secret_free(response.text)


def local(s, path, text):
    target = s.docs_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def only_reads(s):
    assert s.calls and all(method == "GET" for method, _ in s.calls), s.calls
    assert all(p in {"/healthz", "/api/v2/me"} or p.startswith("/api/v2/projects/selected-id")
               for _, p in s.calls), s.calls
    assert not any(p.startswith("/api/v1/") or p.endswith("/sync") for _, p in s.calls)
    assert s.ctx.sync._ws is None and not s.ctx.sync._sessions
    assert not s.ctx.sync._pending and not s.ctx.sync._live_writes
    assert s.ctx.sync._runner is None


def request_for(s):
    return SimpleNamespace(app=s.app_under_test)


def save_checkpoint(s, epoch, cursor, docs):
    namespace = SyncNamespace.for_project(s.ctx.paths.root, url=REAL["url"], persistent_id="selected-id")
    # Test setup only: save() itself refuses to replace another incarnation.
    namespace.path.unlink(missing_ok=True)
    state = SyncStateV2(namespace, epoch, cursor, docs)
    state.save()
    return state


# ---------------------------------------------------------------- pure policy

def test_classification_mirrors_the_engine_conflict_policy():
    h = {k: content_hash(k) for k in "abcdefgh"}
    local = {"same.md": h["a"], "pull.md": h["b"], "push.md": h["c"], "both.md": h["d"],
             "tomb_clean.md": h["e"], "tomb_edit.md": h["f"], "new_local.md": h["g"],
             "lost_remote.md": h["h"]}
    remote = {"same.md": (3, h["a"]), "pull.md": (9, content_hash("new")), "push.md": (4, h["a"]),
              "both.md": (9, content_hash("theirs")), "tomb_clean.md": (9, None),
              "tomb_edit.md": (9, None), "gone_local.md": (4, h["a"]),
              "edited_there.md": (9, h["b"]), "new_remote.md": (2, h["a"]), "old_tomb.md": (1, None)}
    bases = {"same.md": (3, h["a"]), "pull.md": (4, h["b"]), "push.md": (4, h["a"]),
             "both.md": (4, h["a"]), "tomb_clean.md": (4, h["e"]), "tomb_edit.md": (4, h["a"]),
             "gone_local.md": (4, h["a"]), "edited_there.md": (4, h["a"]),
             "lost_remote.md": (5, h["h"])}
    counts, conflicts = classify_reconcile(local, remote, bases)
    assert counts == {"upload": 3, "download": 3, "delete_local": 1, "delete_remote": 1,
                      "conflict": 2, "unchanged": 1}
    # both moved -> copy + pull; remote behind an agreed base is never "unchanged".
    assert conflicts == ("both.md", "lost_remote.md")
    # Without bases (no/foreign checkpoint) it is a full conflict-safe reconciliation.
    counts, conflicts = classify_reconcile(local, remote, {})
    assert counts["conflict"] == 3 and conflicts == ("both.md", "pull.md", "push.md")
    assert counts["upload"] == 4 and counts["download"] == 3 and counts["delete_local"] == 0


def test_registry_is_bounded_ttl_and_discards_only_one_preview():
    now = [1000.0]
    previews = ReconcilePreviews(clock=lambda: 5000.0, monotonic=lambda: now[0], ttl=10, limit=2)
    counts = dict.fromkeys(COUNTS, 0)
    first = previews.publish({"x": 1}, counts, ())
    second = previews.publish({"x": 2}, counts, ())
    assert first.expires_at == 5010.0 and first.fingerprint != second.fingerprint
    third = previews.publish({"x": 3}, counts, ())
    with pytest.raises(ReconcilePreviewError, match="preview_not_found"):
        previews.get(first.preview_id)  # Oldest evicted, bounded.
    with pytest.raises(ReconcilePreviewError, match="preview_stale"):
        previews.require(second.preview_id, second.fingerprint, {"x": 99})
    assert previews.get(third.preview_id) is third and len(previews) == 1
    assert previews.require(third.preview_id, third.fingerprint, {"x": 3}) is third
    with pytest.raises(ReconcilePreviewError, match="preview_stale"):
        previews.require(third.preview_id, "0" * 64, {"x": 3})
    fourth = previews.publish({"x": 4}, counts, ())
    now[0] += 10
    with pytest.raises(ReconcilePreviewError, match="preview_expired"):
        previews.get(fourth.preview_id)
    assert len(previews) == 0


# ------------------------------------------------------- against the real Remote

async def test_multi_page_preview_with_tombstones_is_read_only(project, monkeypatch, studio_home):
    async with selected(project, monkeypatch, studio_home) as s:
        for i in range(45):  # 45 + a.md + tombstone: three snapshot pages of 20.
            s.store.put("selected-id", f"bulk/{i:02}.md", f"# {i}\n", s.user.id)
        s.store.put("selected-id", "both.md", "# theirs\n", s.user.id)
        s.store.put("selected-id", "tomb.md", "# soon gone\n", s.user.id)
        s.store.delete("selected-id", "tomb.md", s.user.id)
        local(s, "a.md", "# A\n")              # unchanged
        local(s, "both.md", "# mine\n")         # conflict (no base)
        local(s, "tomb.md", "# kept here\n")    # tombstone vs edit: the edit goes up
        local(s, "only-here.md", "# local\n")   # upload
        before = tree(s.ctx.paths.root, s.home)
        response = await preview(s)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"preview_id", "fingerprint", "counts", "conflicts", "expires_at",
                             "base", "incarnation"}
        assert body["counts"] == {"upload": 2, "download": 45, "delete_local": 0,
                                  "delete_remote": 0, "conflict": 1, "unchanged": 1}
        assert body["conflicts"] == ["both.md"] and body["base"] == "none"
        assert body["incarnation"] == {"space_instance_id": s.selected.storage_epoch,
                                       "seq": s.store.seq("selected-id")}
        assert [p for _, p in s.calls if p.endswith("/snapshot")] == [
            "/api/v2/projects/selected-id/snapshot"] * 3
        only_reads(s)
        secret_free(response.text)
        assert tree(s.ctx.paths.root, s.home) == before
        assert len(s.ctx.docs.transitions.reconcile_previews) == 1
        stored = s.ctx.docs.transitions.reconcile_previews.get(body["preview_id"])
        assert stored.fingerprint == body["fingerprint"]


async def test_old_v2_bases_apply_only_to_the_same_incarnation(project, monkeypatch, studio_home):
    async with selected(project, monkeypatch, studio_home) as s:
        epoch = s.selected.storage_epoch
        s.store.put("selected-id", "edit-there.md", "# v1\n", s.user.id)          # rev 2
        s.store.put("selected-id", "deleted-there.md", "# v1\n", s.user.id)       # rev 3
        s.store.put("selected-id", "deleted-here.md", "# v1\n", s.user.id)        # rev 4
        s.store.put("selected-id", "edit-here.md", "# v1\n", s.user.id)           # rev 5
        v1 = content_hash("# v1\n")
        bases = {"a.md": Base(1, content_hash("# A\n")), "edit-there.md": Base(2, v1),
                 "deleted-there.md": Base(3, v1), "deleted-here.md": Base(4, v1),
                 "edit-here.md": Base(5, v1)}
        s.store.put("selected-id", "edit-there.md", "# v2\n", s.user.id)
        s.store.delete("selected-id", "deleted-there.md", s.user.id)
        for path in ("a.md", "edit-there.md", "deleted-there.md"):
            local(s, path, "# A\n" if path == "a.md" else "# v1\n")
        local(s, "edit-here.md", "# mine\n")
        save_checkpoint(s, epoch, 5, bases)
        before = tree(s.ctx.paths.root, s.home)
        response = await preview(s)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["base"] == "matched" and body["conflicts"] == []
        assert body["counts"] == {"upload": 1, "download": 1, "delete_local": 1,
                                  "delete_remote": 1, "conflict": 0, "unchanged": 1}
        only_reads(s)
        assert tree(s.ctx.paths.root, s.home) == before

        # A checkpoint of another incarnation keeps its bases but never applies them.
        save_checkpoint(s, "e" * 32, 5, bases)
        body = (await preview(s)).json()
        assert body["base"] == "epoch_mismatch"
        # Full reconciliation: both sides differing without an applicable base conflict.
        assert body["conflicts"] == ["edit-here.md", "edit-there.md"]
        assert body["counts"] == {"upload": 1, "download": 1, "delete_local": 0,
                                  "delete_remote": 0, "conflict": 2, "unchanged": 1}
        # Same incarnation, cursor ahead of the server: rollback, full reconciliation.
        save_checkpoint(s, epoch, 999, bases)
        assert (await preview(s)).json()["base"] == "cursor_rollback"


@pytest.mark.parametrize("drift,code", [("seq", "snapshot_changed"), ("epoch", "instance_mismatch")])
async def test_drift_between_pages_is_409_and_creates_no_preview(project, monkeypatch, studio_home,
                                                                 drift, code):
    async with selected(project, monkeypatch, studio_home) as s:
        for i in range(25):
            s.store.put("selected-id", f"bulk/{i:02}.md", f"# {i}\n", s.user.id)
        local(s, "a.md", "# A\n")
        pages = []

        async def hook(request):
            if request.url.path.endswith("/snapshot"):
                pages.append(request.url.query)
                if len(pages) == 2:  # Before the Remote serves the continuation.
                    if drift == "seq":
                        s.store.put("selected-id", "late.md", "# late\n", s.user.id)
                    else:
                        s.store._db.execute("UPDATE spaces SET storage_epoch=? WHERE id=?",
                                            ("d" * 32, "selected-id"))

        s.hook = hook
        before = tree(s.ctx.paths.root, s.home)
        refused(await preview(s), 409, code)
        assert len(pages) == 2  # The continuation was refused by the Remote.
        assert len(s.ctx.docs.transitions.reconcile_previews) == 0
        only_reads(s)
        assert tree(s.ctx.paths.root, s.home) == before


async def test_selected_incarnation_replaced_before_preview_is_instance_mismatch(
        project, monkeypatch, studio_home):
    async with selected(project, monkeypatch, studio_home) as s:
        s.store._db.execute("UPDATE spaces SET storage_epoch=? WHERE id=?", ("d" * 32, "selected-id"))
        refused(await preview(s), 409, "instance_mismatch")
        only_reads(s)
        assert len(s.ctx.docs.transitions.reconcile_previews) == 0


async def test_recheck_reproduces_fingerprint_and_local_change_makes_it_stale(
        project, monkeypatch, studio_home):
    async with selected(project, monkeypatch, studio_home) as s:
        local(s, "a.md", "# A\n")
        observation = (await s.http.get(s.base)).json()
        cas = dict(expected_revision=observation["identity_revision"],
                   config_fingerprint=observation["config_fingerprint"])
        first = (await preview(s)).json()
        other = (await preview(s)).json()
        previews = s.ctx.docs.transitions.reconcile_previews
        assert first["fingerprint"] != other["fingerprint"]  # Bound to id + TTL.
        evidence = dict(previews.get(first["preview_id"]).evidence)
        again = await recheck_reconcile_preview(request_for(s), s.ctx, first["preview_id"],
                                                first["fingerprint"], **cas)
        assert again.fingerprint == first["fingerprint"]
        before = tree(s.ctx.paths.root, s.home)
        local(s, "a.md", "# A changed here\n")
        changed = tree(s.ctx.paths.root, s.home)
        with pytest.raises(ReconcilePreviewError, match="preview_stale"):
            await recheck_reconcile_preview(request_for(s), s.ctx, first["preview_id"],
                                            first["fingerprint"], **cas)
        # Only that temporary preview is gone; the other one, drafts and bases remain.
        with pytest.raises(ReconcilePreviewError, match="preview_not_found"):
            previews.get(first["preview_id"])
        assert previews.get(other["preview_id"]).fingerprint == other["fingerprint"]
        assert tree(s.ctx.paths.root, s.home) == changed != before
        fresh = (await preview(s)).json()
        new_evidence = previews.get(fresh["preview_id"]).evidence
        assert new_evidence["local_fingerprint"] != evidence["local_fingerprint"]
        assert new_evidence["snapshot"] == evidence["snapshot"]
        # A remote change between preview and recheck is just as stale.
        s.store.put("selected-id", "b.md", "# B\n", s.user.id)
        with pytest.raises(ReconcilePreviewError, match="preview_stale"):
            await recheck_reconcile_preview(request_for(s), s.ctx, fresh["preview_id"],
                                            fresh["fingerprint"], **cas)
        # A refusal during the fresh read discards that preview as well.
        last = (await preview(s)).json()
        s.store._db.execute("UPDATE spaces SET storage_epoch=? WHERE id=?", ("d" * 32, "selected-id"))
        with pytest.raises(HTTPException) as caught:
            await recheck_reconcile_preview(request_for(s), s.ctx, last["preview_id"],
                                            last["fingerprint"], **cas)
        assert caught.value.detail == {"code": "instance_mismatch", "details": {}}
        with pytest.raises(ReconcilePreviewError, match="preview_not_found"):
            previews.get(last["preview_id"])
        only_reads(s)


async def test_preview_ttl_expires_without_touching_anything(project, monkeypatch, studio_home):
    async with selected(project, monkeypatch, studio_home) as s:
        body = (await preview(s)).json()
        previews = s.ctx.docs.transitions.reconcile_previews
        stored = previews.get(body["preview_id"])
        assert body["expires_at"] == stored.expires_at
        before = tree(s.ctx.paths.root, s.home)
        clock = previews._monotonic
        monkeypatch.setattr(previews, "_monotonic", lambda: clock() + previews.ttl)
        with pytest.raises(ReconcilePreviewError, match="preview_expired"):
            previews.get(body["preview_id"])
        assert len(previews) == 0
        assert tree(s.ctx.paths.root, s.home) == before


async def test_same_id_at_a_different_url_is_a_typed_refusal(project, monkeypatch, studio_home):
    from voxarium_studio.sync.bindings import RemoteBindings

    async with selected(project, monkeypatch, studio_home) as s:
        # The profile now points at another server, while the binding keeps the old URL.
        seed(s.home, rows=({**REAL, "url": "http://127.0.0.1/other"},))
        refused(await preview(s), 409, "url_mismatch")
        assert s.calls == []
        seed(s.home, rows=(REAL,))
        # The private row names the same ID at another URL than the project config.
        store = RemoteBindings()
        snapshot = store.read()
        row = snapshot.get(s.ctx.paths.root)
        store.save(row.model_copy(update={"url": "http://127.0.0.1/other"}),
                   expected_fingerprint=snapshot.fingerprint)
        refused(await preview(s), 409, "url_mismatch")
        assert s.calls == [] and len(s.ctx.docs.transitions.reconcile_previews) == 0


@pytest.mark.parametrize("case,code", [("copied_root", "duplicate_local_binding"),
                                       ("second_binding", "duplicate_local_binding"),
                                       ("unbound", "binding_required")])
async def test_copied_root_and_missing_binding_are_refused_before_network(
        project, monkeypatch, studio_home, tmp_path, case, code):
    from voxarium_studio.core.project_scope import canonical_root
    from voxarium_studio.sync.bindings import RemoteBindings

    async with selected(project, monkeypatch, studio_home) as s:
        store = RemoteBindings()
        snapshot = store.read()
        row = snapshot.get(s.ctx.paths.root)
        original = tmp_path / "original-checkout"
        original.mkdir()
        if case == "copied_root":
            # This Ctx is a copy: the binding belongs to the original root only.
            snapshot = store.remove(s.ctx.paths.root, expected_fingerprint=snapshot.fingerprint)
        if case != "unbound":
            store.save(row.model_copy(update={"canonical_root": str(canonical_root(original))}),
                       expected_fingerprint=snapshot.fingerprint)
        else:
            store.remove(s.ctx.paths.root, expected_fingerprint=snapshot.fingerprint)
        before = tree(s.ctx.paths.root, s.home)
        refused(await preview(s), 409, code)
        assert s.calls == [] and tree(s.ctx.paths.root, s.home) == before


async def test_unavailable_owner_and_stale_cas(project, monkeypatch, studio_home):
    async with selected(project, monkeypatch, studio_home) as s:
        holder = s.app_under_test.state.remote_catalog
        runtime = holder.runtime
        holder.runtime = None
        try:
            refused(await preview(s), 503, "catalog_unavailable")
        finally:
            holder.runtime = runtime
        assert s.calls == []
        refused(await preview(s, expected_revision=1), 409, "stale_binding")
        refused(await preview(s, config_fingerprint="0" * 64), 409, "stale_binding")
        refused(await preview(s, backend_epoch="not-this-backend"), 409, "stale_backend_epoch")
        response = await preview(s, url="http://evil.invalid")
        refused(response, 422, "invalid_request")
        assert s.calls == [] and len(s.ctx.docs.transitions.reconcile_previews) == 0
