"""Files attached to "request changes" (§10.4, ``teams/attachments.py``).

The review entry records them, the session that resumes is handed the comment with every
file named by its absolute path and the pictures beside it as image blocks, and a member
busy elsewhere gets the same on the resume the queue starts later. Against a recording
backend, so nothing is billed.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import AgentMessage, TaskStatus, TeamMember
from voxarium_studio.teams import attachments as review_attachments
from voxarium_studio.teams.backends.base import AgentEvent, register_backend

TEAM = "team_default"
DIRECTOR = f"{TEAM}__director"
DEV = f"{TEAM}__dev"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
async def recorded(ctx: AppContext) -> tuple[AppContext, list[Any]]:
    """Every agent member on a backend that remembers each run's request."""
    requests: list[Any] = []

    class Recording:
        name = "recording_attachments"

        def is_available(self):
            return True, "recording"

        async def run(self, request):
            requests.append(request)
            yield AgentEvent(kind="result", text="Done.", session_id="sess_1", is_final=True)

    register_backend(Recording.name, Recording)
    async with ctx.db.session() as session:
        for member in (await session.exec(select(TeamMember))).all():
            member.agent_backend = Recording.name
            member.isolate_worktree = False
            session.add(member)
    return ctx, requests


async def _reviewed_task(ctx: AppContext):
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Draw the door", brief="b", auto_start=True
    )
    await ctx.teams.update_task(task.id, {"status": "pending"})
    return task


def test_names_are_cleaned_and_never_overwritten(project) -> None:
    from voxarium_studio.core.paths import ProjectPaths

    store = review_attachments.AttachmentStore(ProjectPaths.for_project(project))
    first = store.add("task_1", "../../shots/Screen Shot (1).png", PNG)
    assert first["name"] == "Screen Shot (1).png", "only the last path component survives"
    assert first["image"] is True and first["media_type"] == "image/png"
    assert first["path"] == "voxarium_studio/attachments/task_1/Screen Shot (1).png"
    second = store.add("task_1", "Screen Shot (1).png", b"other")
    assert second["name"] == "Screen Shot (1)-2.png", "a taken name gets a suffix"
    log = store.add("task_1", "build.log", b"text")
    assert log["image"] is False and log["media_type"] is None
    with pytest.raises(ValueError):
        store.path_for("task_1", "../config.yaml")
    with pytest.raises(FileNotFoundError):
        store.path_for("task_1", "missing.png")
    with pytest.raises(ValueError):
        store.add("task_1", "big.png", b"x" * (review_attachments.MAX_ATTACHMENT_BYTES + 1))


async def test_a_review_carries_its_files_to_the_session(recorded, wait_for) -> None:
    ctx, requests = recorded
    task = await _reviewed_task(ctx)
    await wait_for(task.id, timeout=20)
    assert (await ctx.teams.find_task(task.id)).status == TaskStatus.NEEDS_REVIEW

    shot = ctx.teams.attachments.add(task.id, "door.png", PNG)
    notes = ctx.teams.attachments.add(task.id, "notes.txt", b"hinge on the left")
    await ctx.teams.request_changes(
        task.id, "The door opens the wrong way", actor_id=DIRECTOR,
        attachments=[shot["name"], notes["name"]],
    )
    await wait_for(task.id, timeout=20)

    assert len(requests) == 2, "the first run and the resume"
    resume = requests[1]
    assert resume.prompt.startswith("The door opens the wrong way")
    assert "Attached files (open with Read):" in resume.prompt
    assert str(ctx.paths.root / "voxarium_studio" / "attachments" / task.id / "door.png") in resume.prompt
    assert str(ctx.paths.root / "voxarium_studio" / "attachments" / task.id / "notes.txt") in resume.prompt
    assert [image["media_type"] for image in resume.images] == ["image/png"], (
        "only the picture is an image block; the text file is reached by its path"
    )
    assert resume.images[0]["path"].endswith("door.png")
    assert any(
        rule.startswith("Read(") and "attachments" in rule for rule in resume.extra_allowed_tools
    ), "the folder is opened to the session"

    async with ctx.db.session() as session:
        entries = (
            await session.exec(select(AgentMessage).where(AgentMessage.task_id == task.id))
        ).all()
    review = next(entry for entry in entries if entry.kind == "review")
    assert [record["name"] for record in review.data["attachments"]] == ["door.png", "notes.txt"]
    assert review.text == "The door opens the wrong way", "the trailer is the session's, not the transcript's"


async def test_a_review_naming_a_missing_file_is_refused_whole(recorded, wait_for) -> None:
    ctx, _ = recorded
    task = await _reviewed_task(ctx)
    await wait_for(task.id, timeout=20)
    with pytest.raises(FileNotFoundError):
        await ctx.teams.request_changes(
            task.id, "see the picture", actor_id=DIRECTOR, attachments=["nowhere.png"]
        )
    async with ctx.db.session() as session:
        entries = (
            await session.exec(select(AgentMessage).where(AgentMessage.task_id == task.id))
        ).all()
    assert not any(entry.kind == "review" for entry in entries), "nothing was written"


def test_attachments_over_rest(client) -> None:
    created = client.post(
        f"/api/teams/{TEAM}/tasks", json={"assignee_id": DEV, "title": "a"}
    ).json()
    task_id = created["id"]

    stored = client.post(
        f"/api/teams/{TEAM}/tasks/{task_id}/attachments",
        files={"file": ("shot.png", PNG, "image/png")},
    )
    assert stored.status_code == 201, stored.text
    record = stored.json()
    assert record["name"] == "shot.png" and record["image"] is True

    fetched = client.get(f"/api/teams/{TEAM}/tasks/{task_id}/attachments/shot.png")
    assert fetched.status_code == 200 and fetched.content == PNG
    assert fetched.headers["content-disposition"].startswith("inline")

    assert (
        client.get(f"/api/teams/{TEAM}/tasks/{task_id}/attachments/other.png").status_code
        == 404
    )
    assert (
        client.post(
            f"/api/teams/{TEAM}/tasks/ghost/attachments",
            files={"file": ("shot.png", PNG, "image/png")},
        ).status_code
        == 404
    )

    refused = client.post(
        f"/api/teams/{TEAM}/tasks/{task_id}/request-changes",
        json={"comment": "x", "attachments": ["nowhere.png"]},
    )
    assert refused.status_code == 404

    assert (
        client.delete(f"/api/teams/{TEAM}/tasks/{task_id}/attachments/shot.png").status_code
        == 204
    )
    assert (
        client.get(f"/api/teams/{TEAM}/tasks/{task_id}/attachments/shot.png").status_code == 404
    )
