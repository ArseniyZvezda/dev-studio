"""Session Plan items own their tasks, and the task restraint is enforced (dev spec §10.11)."""
from __future__ import annotations

import pytest

from test_producer import DEV, TEAM, allow, draft, finished, opened, producer, tools_for  # noqa: F401
from test_producer_checklist import workspace_chat
from voxarium_studio.teams.producer import (
    ITEM_JUSTIFICATION_STEP,
    REOPEN_JUSTIFICATION_STEP,
    ProducerService,
)

PLAN = [{"text": "Implement movement", "done": False}, {"text": "Verify movement", "done": False}]


def text(result) -> str:
    return result["content"][0]["text"]


def task_args(title: str, **extra) -> dict:
    return {"assignee_id": DEV, "title": title, "brief": f"{title}: acceptance criteria.", **extra}


async def planned(ctx):
    chat = await workspace_chat(ctx)
    await ctx.chats.producer.update_checklist(chat.id, PLAN)
    return chat, await tools_for(ctx, chat)


async def plan(ctx, chat):
    return (await ctx.chats.get_chat(chat.id)).producer_checklist


def test_items_gain_stable_ids_and_keep_them_through_rewording_and_reordering():
    first = ProducerService.merge_checklist([], PLAN)
    assert [item["id"] for item in first] == ["p1", "p2"] and all(item["tasks"] == [] for item in first)
    first[0]["tasks"] = [{"id": "task_a", "status": "done"}]
    reworded = ProducerService.merge_checklist(first, [
        {"id": "p2", "text": "Verify movement", "done": True},
        {"text": "Polish movement", "done": False},
        {"id": "p1", "text": "Implement movement and jumping", "done": True},
    ])
    assert [(item["id"], item["text"]) for item in reworded] == [
        ("p2", "Verify movement"), ("p3", "Polish movement"), ("p1", "Implement movement and jumping")]
    assert reworded[2]["tasks"] == first[0]["tasks"]


def test_unchanged_text_identifies_an_item_sent_without_its_id():
    saved = ProducerService.merge_checklist([], PLAN)
    saved[1]["tasks"] = [{"id": "task_a", "status": "in_progress"}]
    again = ProducerService.merge_checklist(saved, [dict(item, done=True) for item in PLAN])
    assert [item["id"] for item in again] == ["p1", "p2"] and again[1]["tasks"] == saved[1]["tasks"]


def test_item_with_unfinished_tasks_cannot_be_removed_but_a_closed_one_can():
    saved = ProducerService.merge_checklist([], PLAN)
    saved[0]["tasks"] = [{"id": "task_a", "status": "needs_review"}]
    with pytest.raises(ValueError, match="cannot be removed.*p1"):
        ProducerService.merge_checklist(saved, [{"id": "p2", "text": "Verify movement", "done": False}])
    # Rewording without the id would detach the tasks just the same.
    with pytest.raises(ValueError, match="cannot be removed"):
        ProducerService.merge_checklist(saved, [{"text": "Movement", "done": False}])
    saved[0]["tasks"][0]["status"] = "done"
    kept = ProducerService.merge_checklist(saved, [{"id": "p2", "text": "Verify movement", "done": False}])
    assert [item["id"] for item in kept] == ["p2"]


@pytest.mark.parametrize("items", [
    [{"id": "p9", "text": "Unknown", "done": False}],
    [{"id": "p1", "text": "Twice", "done": False}, {"id": "p1", "text": "Twice again", "done": False}],
])
def test_unknown_or_repeated_item_ids_are_rejected(items):
    with pytest.raises(ValueError):
        ProducerService.merge_checklist(ProducerService.merge_checklist([], PLAN), items)


def test_load_counts_tasks_and_weighs_each_reopening_more_than_the_last():
    item = {"tasks": [
        {"status": "done", "reopens": 0},
        {"status": "pending", "reopens": 1},      # 1 + 1
        {"status": "needs_review", "reopens": 3},  # 1 + (1 + 2 + 3)
        {"status": "cancelled", "reopens": 2},     # cancelled work is not load
    ]}
    assert ProducerService.item_load(item) == 1 + 2 + 7


async def test_created_task_is_recorded_on_its_item_and_first_one_needs_no_justification(producer):
    chat, tools = await planned(producer)
    missing = await tools["create_task"](task_args("Movement"))
    assert missing["is_error"] and "plan_item" in text(missing) and "p1 (Implement movement)" in text(missing)
    unknown = await tools["create_task"](task_args("Movement", plan_item="p7"))
    assert unknown["is_error"] and not await producer.chats.producer.tasks(chat.id)

    created = await tools["create_task"](task_args("Movement", plan_item="p1"))
    assert not created.get("is_error"), created
    task, = await producer.chats.producer.tasks(chat.id)
    first, second = await plan(producer, chat)
    assert second["tasks"] == []
    assert first["tasks"] == [{"id": task.id, "display_id": producer.teams.display_id(task),
                               "title": "Movement", "status": "todo", "reopens": 0, "justification": ""}]
    assert producer.chats.to_json(await producer.chats.get_chat(chat.id))["producer_checklist"][0]["tasks"]


async def test_each_further_task_on_an_item_needs_a_longer_justification(producer):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("First", plan_item="p1"))).get("is_error")
    for load in (1, 2, 3):
        required = ITEM_JUSTIFICATION_STEP * load
        short = await tools["create_task"](task_args(
            f"Task {load}", plan_item="p1", justification="x" * (required - 1)))
        assert short["is_error"] and f"at least {required} characters" in text(short)
        # Padding with whitespace is not justification.
        padded = await tools["create_task"](task_args(
            f"Task {load}", plan_item="p1", justification="x" + " " * required))
        assert padded["is_error"]
        assert len(await producer.chats.producer.tasks(chat.id)) == load
        enough = await tools["create_task"](task_args(
            f"Task {load}", plan_item="p1", justification="x" * required))
        assert not enough.get("is_error"), enough
    # Another item starts from nothing.
    assert not (await tools["create_task"](task_args("Verify", plan_item="p2"))).get("is_error")
    first, second = await plan(producer, chat)
    assert len(first["tasks"]) == 4 and len(second["tasks"]) == 1
    assert first["tasks"][1]["justification"] == "x" * ITEM_JUSTIFICATION_STEP


async def test_reused_task_is_linked_counted_and_required_before_proposing(producer):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p1"))).get("is_error")
    reused = await draft(producer, chat, "Existing draft")
    created, = [t for t in await producer.chats.producer.tasks(chat.id) if t.id != reused.id]
    await tools["update_producer_checklist"]({"items": [{"id": "p1", **PLAN[0]}, {"id": "p2", **PLAN[1]}]})

    refused = await tools["propose_producer_plan"]({"tasks": [created.id, reused.id], "summary": "Movement"})
    assert refused["is_error"] and producer.teams.display_id(reused) in text(refused)
    assert producer.teams.display_id(created) not in text(refused)

    short = await tools["link_producer_task"]({"task": reused.id, "plan_item": "p1"})
    assert short["is_error"] and f"at least {ITEM_JUSTIFICATION_STEP} characters" in text(short)
    linked = await tools["link_producer_task"]({
        "task": reused.id, "plan_item": "p1", "justification": "y" * ITEM_JUSTIFICATION_STEP})
    assert not linked.get("is_error"), linked
    # Linking twice is not a second task.
    again = await tools["link_producer_task"]({"task": reused.id, "plan_item": "p1"})
    assert not again.get("is_error")
    assert [record["id"] for record in (await plan(producer, chat))[0]["tasks"]] == [created.id, reused.id]

    proposed = await tools["propose_producer_plan"]({"tasks": [created.id, reused.id], "summary": "Movement"})
    assert not proposed.get("is_error"), proposed


async def test_item_shows_each_task_in_its_current_status_whatever_it_becomes(producer):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p1"))).get("is_error")
    assert not (await tools["create_task"](task_args(
        "Animation", plan_item="p1", justification="z" * ITEM_JUSTIFICATION_STEP))).get("is_error")
    movement, animation = await producer.chats.producer.tasks(chat.id)
    service = producer.chats.producer

    await producer.teams.update_task(animation.id, {"status": "cancelled"})
    await allow(producer, movement)
    await producer.teams.submit_for_review(movement.id, "Movement implemented", "done")
    await service.sync_plan_tasks()
    assert [record["status"] for record in (await plan(producer, chat))[0]["tasks"]] == [
        "needs_review", "cancelled"]

    await service.review(chat.id, movement.id, "approve", "")
    statuses = [record["status"] for record in (await plan(producer, chat))[0]["tasks"]]
    assert statuses == ["done", "cancelled"]
    # A closed item may leave the Plan; until then it keeps both tasks.
    await service.update_checklist(chat.id, [{"id": "p2", **PLAN[1]}])
    assert [item["id"] for item in await plan(producer, chat)] == ["p2"]


async def test_ordinary_chat_without_a_plan_creates_tasks_as_before(producer):
    chat = await opened(producer)
    tools = await tools_for(producer, chat)
    assert not (await tools["create_task"](task_args("Movement"))).get("is_error")
    task, = await producer.chats.producer.tasks(chat.id)
    assert not (await tools["propose_producer_plan"]({"tasks": [task.id], "summary": "Movement"})).get("is_error")
    assert (await producer.chats.get_chat(chat.id)).producer_checklist == []


async def test_plan_written_before_items_had_ids_gains_them_on_first_use(producer):
    from voxarium_studio.models.teams import AgentChat

    chat = await workspace_chat(producer)
    async with producer.db.session() as session:
        stored = await session.get(AgentChat, chat.id)
        stored.producer_checklist = [dict(item) for item in PLAN]
        stored.producer_checklist_revision = await producer.chats.producer.checklist_revision(chat.id)
        session.add(stored)
    tools = await tools_for(producer, chat)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p2"))).get("is_error")
    first, second = await plan(producer, chat)
    assert (first["id"], first["tasks"]) == ("p1", []) and len(second["tasks"]) == 1


@pytest.mark.parametrize("approved", [False, True])
async def test_done_task_reopens_in_place_counted_with_a_rising_price(producer, approved):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p1"))).get("is_error")
    task, = await producer.chats.producer.tasks(chat.id)
    await allow(producer, task)
    service = producer.chats.producer

    for number in (1, 2, 3):
        await producer.teams.submit_for_review(task.id, f"Movement implemented, attempt {number}", "done")
        if approved:
            await service.review(chat.id, task.id, "approve", "")
        required = REOPEN_JUSTIFICATION_STEP * number
        short = await tools["review_producer_task"]({
            "task": task.id, "action": "request_changes", "comment": "Diagonal movement is too fast",
            "justification": "j" * (required - 1)})
        assert short["is_error"] and f"reopening #{number}" in text(short)
        assert f"at least {required} characters" in text(short)
        unchanged = await producer.teams._reload(task.id)
        assert unchanged.reopen_count == number - 1
        assert unchanged.status == ("done" if approved else "needs_review")

        reopened = await tools["review_producer_task"]({
            "task": task.id, "action": "request_changes", "comment": "Diagonal movement is too fast",
            "justification": "j" * required})
        assert not reopened.get("is_error"), reopened
        assert f"reopening #{number}" in text(reopened)
        await finished(producer, task.id)
        saved = await producer.teams._reload(task.id)
        assert saved.reopen_count == number and saved.review_comment == "Diagonal movement is too fast"
        assert saved.status != "done"
        record, = (await plan(producer, chat))[0]["tasks"]
        assert record["reopens"] == number

    # One task reopened three times weighs 1 + (1 + 2 + 3): the next task pays for it.
    item = (await plan(producer, chat))[0]
    assert service.item_load(item) == 7
    refused = await tools["create_task"](task_args(
        "Corrective task", plan_item="p1", justification="c" * (ITEM_JUSTIFICATION_STEP * 7 - 1)))
    assert refused["is_error"] and f"at least {ITEM_JUSTIFICATION_STEP * 7} characters" in text(refused)
    from sqlmodel import select

    from voxarium_studio.models.teams import AgentMessage
    async with producer.db.session() as session:
        messages = (await session.exec(select(AgentMessage).where(
            AgentMessage.task_id == task.id).order_by(AgentMessage.created_at))).all()
    assert [m.data["reopen_count"] for m in messages if (m.data or {}).get("producer_review") == "reopen"] == [1, 2, 3]


async def test_reopening_an_archived_task_returns_it_to_the_board(producer):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p1"))).get("is_error")
    task, = await producer.chats.producer.tasks(chat.id)
    await allow(producer, task)
    await producer.teams.submit_for_review(task.id, "Movement implemented", "done")
    await producer.chats.producer.review(chat.id, task.id, "approve", "")
    await producer.teams.update_task(task.id, {"archived": True})
    reopened = await tools["review_producer_task"]({
        "task": task.id, "action": "request_changes", "comment": "Diagonal movement is too fast",
        "justification": "j" * REOPEN_JUSTIFICATION_STEP})
    assert not reopened.get("is_error"), reopened
    await finished(producer, task.id)
    saved = await producer.teams._reload(task.id)
    assert not saved.archived and saved.status != "done" and saved.reopen_count == 1


async def test_reopening_needs_feedback_and_never_starts_or_cancels_done_work(producer):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p1"))).get("is_error")
    task, = await producer.chats.producer.tasks(chat.id)
    await allow(producer, task)
    await producer.teams.submit_for_review(task.id, "Movement implemented", "done")
    justification = "j" * REOPEN_JUSTIFICATION_STEP
    silent = await tools["review_producer_task"]({
        "task": task.id, "action": "request_changes", "comment": " ", "justification": justification})
    assert silent["is_error"] and "feedback" in text(silent)
    for action in ("start", "cancel"):
        denied = await tools["review_producer_task"]({
            "task": task.id, "action": action, "comment": "Stop", "justification": justification})
        assert denied["is_error"] and "cannot be started or cancelled" in text(denied)
    saved = await producer.teams._reload(task.id)
    assert saved.status == "needs_review" and saved.reopen_count == 0


async def test_question_and_error_results_are_returned_without_counting_a_reopening(producer):
    chat, tools = await planned(producer)
    assert not (await tools["create_task"](task_args("Movement", plan_item="p1"))).get("is_error")
    task, = await producer.chats.producer.tasks(chat.id)
    await allow(producer, task)
    await producer.teams.submit_for_review(task.id, "Which input keys?", "question")
    answered = await tools["review_producer_task"]({
        "task": task.id, "action": "request_changes", "comment": "Use WASD"})
    assert not answered.get("is_error"), answered
    await finished(producer, task.id)
    assert (await producer.teams._reload(task.id)).reopen_count == 0
