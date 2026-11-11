"""Persistent Producer conversations, approval gates and task waits (§10.11).

Waiting holds no agent slot. The project-local bus wakes a small reconciler; periodic
reconciliation also repairs missed events and process restarts. Only a changed snapshot
starts a model turn, and the chat lock serializes that turn with a user's message.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from contextlib import suppress
from typing import Any

from sqlmodel import delete, select

from ..models.teams import AgentChat, AgentChatMessage, TaskStatus, Team, TeamMember, TeamTask
from .producer_identity import is_producer

log = logging.getLogger(__name__)
COMPACT_CHARS = 48_000
SUMMARY_CHARS = 16_000
CHECKLIST_MAX_ITEMS = 20
CHECKLIST_ITEM_MAX_CHARS = 50
CHECKLIST_INSTRUCTIONS = (
    "Maintain the session Plan with update_producer_checklist: a short checklist only, "
    "1–20 items of plain single-line text, each at most 50 characters. "
    "Write it before creating or changing tasks; update it after adding, editing, reviewing "
    "or completing tasks and after reading changed task results, before proposing a plan, "
    "waiting, asking the user or ending the turn. Mark only completed work done; keep "
    "unfinished or cancelled work unchecked and describe remaining work concisely. "
    "The session Plan is separate from the draft task approval plan and survives approval."
)


class ProducerService:
    def __init__(self, chats) -> None:
        self.chats = chats
        self.orchestrator = chats.orchestrator
        self._worker: asyncio.Task | None = None
        self._subscription = None
        self._approval_lock = asyncio.Lock()

    async def restore_paused(self) -> None:
        """Hold each started Producer and its tasks before queues are restored (§10.11)."""
        async with self.chats.db.session() as session:
            chat_ids = (await session.exec(
                select(AgentChat.id).join(TeamMember, AgentChat.member_id == TeamMember.id)
                .where(AgentChat.producer_mode == True, AgentChat.producer_started == True)  # noqa: E712
            )).all()
        # Install every gate before touching rows or publishing events.
        self.orchestrator.paused_producer_chats.update({chat_id: set() for chat_id in chat_ids})
        for chat_id in chat_ids:
            async with self.chats.lock(chat_id):
                chat = await self.chats.get_chat(chat_id)
                if chat and chat.producer_mode and chat.producer_started:
                    await self._pause_chat(chat_id)
                else:
                    self.orchestrator.paused_producer_chats.pop(chat_id, None)

    async def _owned_agent_tasks(self, chat_id: str) -> list[TeamTask]:
        async with self.chats.db.session() as session:
            return list((await session.exec(
                select(TeamTask).join(TeamMember, TeamTask.assignee_id == TeamMember.id)
                .where(TeamTask.producer_chat_id == chat_id, TeamMember.kind == "agent")
                .order_by(TeamTask.number)
            )).all())

    def require_resumed(self, chat_id: str) -> None:
        if chat_id in self.orchestrator.paused_producer_chats:
            raise ValueError("Producer Mode is paused. Press Resume to continue.")

    async def pause(self, chat_id: str) -> AgentChat:
        async with self.chats.lock(chat_id):
            chat = await self._chat(chat_id)
            if not chat.producer_started:
                raise ValueError("Producer Mode has not started. Send the first message to begin.")
            if not chat.producer_paused:
                self.orchestrator.paused_producer_chats.setdefault(chat_id, set())
                await self._pause_chat(chat_id)
            return await self._chat(chat_id)

    async def _pause_chat(self, chat_id: str) -> None:
        await self._save(chat_id, producer_paused=True)
        tasks = await self._owned_agent_tasks(chat_id)
        interrupted = [t for t in tasks if self.orchestrator.producer_holds(t)
                       and (t.status == "in_progress" or self.chats.jobs.is_running(t.id))]
        # Persist continuation intent before cancellation, so even a process exit
        # during cleanup cannot lose an interrupted run with auto_start=False.
        async with self.chats.db.session() as session:
            for task in interrupted:
                stored = await session.get(TeamTask, task.id)
                if stored and self.orchestrator.producer_holds(stored) and stored.status in {"pending", "in_progress"}:
                    stored.pause_resume_pending = True
                    session.add(stored)
        for task in tasks:
            if self.orchestrator.producer_holds(task):
                self.orchestrator._cancel_limit_wait(task.id)
        keys = [chat_id, *(t.id for t in interrupted if self.orchestrator.producer_holds(t))]
        handles = [h for key in keys if (h := self.chats.jobs.get(key)) is not None]
        for handle in handles:
            handle.meta["producer_pause"] = True
            await self.chats.jobs.cancel(handle.id)
        # Await subprocess cleanup and final events, including jobs queued for a slot.
        await asyncio.gather(*(h.task for h in handles if h.task), return_exceptions=True)
        for task in interrupted:
            async with self.chats.db.session() as session:
                stored = await session.get(TeamTask, task.id)
                if (not stored or not self.orchestrator.producer_holds(stored)
                        or stored.status not in {"pending", "in_progress"}):
                    continue
                stored.status = TaskStatus.PENDING
                stored.pause_resume_pending = True
                session.add(stored)
            await self.orchestrator.record_message(
                task.id, task.assignee_id, role="system", kind="state",
                text="Producer paused. Work and session state preserved; waiting for Resume.",
                data={"producer_paused": True, "agent_session_id": stored.agent_session_id})
            await self.orchestrator._publish_task_changed(task.id)
        await self.chats._publish_chat_by_id(chat_id)

    async def resume(self, chat_id: str) -> AgentChat:
        async with self.chats.lock(chat_id):
            chat = await self._chat(chat_id)
            if not chat.producer_started:
                raise ValueError("Producer Mode has not started. Send the first message to begin.")
            if chat_id not in self.orchestrator.paused_producer_chats:
                return chat
            await self._save(chat_id, producer_paused=False)
            self.orchestrator.paused_producer_chats.pop(chat_id)
            tasks = await self._owned_agent_tasks(chat_id)
            for task in tasks:
                if task.status == "blocked" and task.resume_on_limit and task.limit_resume_at:
                    self.orchestrator._schedule_limit_wait(task.id, task.limit_resume_at)
            await self.orchestrator._start_if_ready([t.id for t in tasks if t.status == TaskStatus.PENDING])
            for task in tasks:
                if task.producer_approved and task.status == TaskStatus.TODO:
                    await self.orchestrator._autostart_todo(task)
            if chat.producer_state in {"running", "waiting", "error", "discussing"}:
                await self.queue(chat_id, self.chats.config.teams.prompts.text("producer_wake"))
            return await self._chat(chat_id)

    async def start(self) -> None:
        if self._worker is None:
            self._subscription = self.chats.bus.subscribe()
            self._worker = asyncio.create_task(self._listen(), name="producer-waits")

    async def shutdown(self) -> None:
        if self._subscription:
            self._subscription.close()
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def _chat(self, chat_id: str, *, active: bool = True) -> AgentChat:
        chat = await self.chats.get_chat(chat_id)
        if chat is None:
            raise KeyError(chat_id)
        async with self.chats.db.session() as session:
            member = await session.get(TeamMember, chat.member_id)
        if not member or not is_producer(member) or member.kind != "agent":
            raise ValueError("Producer Mode is available only with the built-in Producer.")
        if active and not chat.producer_mode:
            raise ValueError("Producer Mode is stopped.")
        return chat

    async def _save(self, chat_id: str, **fields: Any) -> AgentChat:
        async with self.chats.db.session() as session:
            chat = await session.get(AgentChat, chat_id)
            if chat is None:
                raise KeyError(chat_id)
            for key, value in fields.items():
                setattr(chat, key, value)
            session.add(chat)
        await self.chats._publish_chat(chat)
        return chat

    def _idle(self, chat_id: str) -> None:
        from .chat import ChatBusy
        if self.chats.jobs.is_running(chat_id):
            raise ChatBusy("The Producer is still answering. Wait for the reply or stop it.")

    @staticmethod
    def _validate_estimate(value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Estimate Task Cost must be a positive finite USD amount.")

    async def set_estimate_task_cost(self, chat_id: str, value: float) -> AgentChat:
        """Persist the estimate between turns, including while paused (§10.11)."""
        self._validate_estimate(value)
        async with self.chats.lock(chat_id):
            chat = await self._chat(chat_id, active=False)
            if chat.estimate_task_cost_usd == value:
                return chat
            self._idle(chat_id)
            return await self._save(chat_id, estimate_task_cost_usd=value)

    async def set_effort_mode(self, chat_id: str, mode: str) -> AgentChat:
        """Save the task effort policy without starting a turn or changing existing tasks (§10.11)."""
        if mode not in {"agent", "auto"}:
            raise ValueError("Unknown Producer effort mode.")
        async with self.chats.lock(chat_id):
            chat = await self._chat(chat_id, active=False)
            if chat.producer_effort_mode == mode:
                return chat
            self._idle(chat_id)
            return await self._save(chat_id, producer_effort_mode=mode)

    async def enable(self, chat_id: str, locale: str = "en", policy: str | None = None,
                     *, estimate_task_cost_usd: float | None = None,
                     effort_mode: str | None = None) -> AgentChat:
        if estimate_task_cost_usd is not None:
            self._validate_estimate(estimate_task_cost_usd)
        if effort_mode is not None and effort_mode not in {"agent", "auto"}:
            raise ValueError("Unknown Producer effort mode.")
        async with self.chats.lock(chat_id):
            chat = await self._chat(chat_id, active=False)
            if chat.producer_mode:
                return chat
            self._idle(chat_id)
            if policy is not None and policy not in {"careful", "independent"}:
                raise ValueError("Unknown Producer decision policy.")
            from .tools import SERVER_NAME
            if SERVER_NAME in self.chats.config.mcp.disabled:
                raise ValueError("Enable the built-in Studio MCP server to use Producer Mode.")
            try:
                import claude_agent_sdk  # noqa: F401
            except ImportError as exc:
                raise ValueError("Install the Studio teams extra to use Producer Mode.") from exc
            await self._save(chat_id, producer_mode=True, producer_locale=locale, producer_state="discussing",
                             producer_paused=False,
                             producer_policy=policy or chat.producer_policy,
                             producer_effort_mode=effort_mode or chat.producer_effort_mode,
                             estimate_task_cost_usd=(estimate_task_cost_usd if estimate_task_cost_usd is not None
                                                     else chat.estimate_task_cost_usd),
                             producer_plan=[], producer_plan_summary="", producer_watch=[],
                             producer_questions=[], error=None)
            if not chat.title:
                await self._save(chat_id, title="Режим продюсера" if locale == "ru" else "Producer Mode")
            await self.queue(chat_id, self.chats.config.teams.prompts.text("producer_start"),
                             new_mode=True)
            return await self._chat(chat_id)

    async def set_policy(self, chat_id: str, policy: str) -> AgentChat:
        """Switch between turns so one running turn never mixes two policies (§10.11)."""
        if policy not in {"careful", "independent"}:
            raise ValueError("Unknown Producer decision policy.")
        async with self.chats.lock(chat_id):
            chat = await self._chat(chat_id, active=False)
            if chat.producer_policy == policy:
                return chat
            if chat.producer_mode and chat.producer_started:
                self.require_resumed(chat_id)
            self._idle(chat_id)
            await self._save(chat_id, producer_policy=policy)
            # Before the first message this is only a preference; a started chat
            # receives either changed policy in its own continuation (§10.11).
            if chat.producer_mode and chat.producer_started:
                if policy == "independent" and chat.producer_plan:
                    try:
                        await self._approve_plan(chat_id, chat.producer_plan_version, automatic=True)
                    except ValueError:
                        # A stale plan must leave the selected policy unchanged as well.
                        await self._save(chat_id, producer_policy=chat.producer_policy)
                        raise
                await self.queue(chat_id, self.chats.config.teams.prompts.text("producer_wake"))
            return await self._chat(chat_id, active=False)

    async def disable(self, chat_id: str, *, cancel: bool = True, deleting: bool = False) -> AgentChat:
        # External callers lock before entering; a tool may stop its mode, but must never
        # cancel/await the job that is currently calling that tool.
        previous = await self._chat(chat_id, active=False)
        if previous.producer_workspace and not deleting:
            raise ValueError("Producer workspace chats stay enabled until deleted.")
        chat = await self._save(chat_id, producer_mode=False, producer_state="off", producer_paused=False,
                                producer_watch=[], producer_plan=[], producer_plan_summary="",
                                producer_questions=[])
        if cancel:
            await self.chats._cancel_turn(chat_id)
        self.orchestrator.paused_producer_chats.pop(chat_id, None)
        return chat

    async def queue(self, chat_id: str, prompt: str, *, new_mode: bool = False) -> None:
        from .chat import JOB_KIND
        chat = await self._chat(chat_id)
        if chat_id in self.orchestrator.paused_producer_chats:
            return
        self._idle(chat_id)
        await self._save(chat_id, producer_started=True, producer_state="running", error=None)
        await self._release_tasks(chat_id)
        if chat_id in self.orchestrator.paused_producer_chats:
            return
        self.chats.jobs.submit(
            chat_id, JOB_KIND,
            lambda handle: self.chats._run_turn(chat_id, chat.member_id, prompt,
                                                producer_skill_on_resume=new_mode),
            member_id=chat.member_id, chat_id=chat_id,
        )
        await self.chats._publish_chat_by_id(chat_id)

    async def _release_tasks(self, chat_id: str) -> None:
        chat = await self._chat(chat_id)
        async with self.chats.db.session() as session:
            member = await session.get(TeamMember, chat.member_id)
        try:
            for task_id in chat.producer_release_pending:
                task = await self.orchestrator.find_task(task_id)
                if task and task.team_id == member.team_id and task.producer_approved:
                    if task.status in {TaskStatus.TODO, TaskStatus.PENDING}:
                        await self.orchestrator.update_task(task.id, {
                            "status": TaskStatus.PENDING, "auto_start": True,
                        }, actor_id=chat.member_id)
                        await self.orchestrator._start_if_ready([task.id])
            await self._save(chat_id, producer_release_pending=[])
        except Exception as exc:
            await self._save(chat_id, producer_state="error", error=str(exc))
            raise

    async def on_user_message(self, chat: AgentChat, message) -> None:
        if not chat.producer_mode:
            return
        questions = [dict(q, answer_message_id=message.id) if not q.get("answer_message_id")
                     else q for q in chat.producer_questions]
        await self._save(chat.id, producer_started=True, producer_state="running", producer_questions=questions)

    async def turn_finished(self, chat_id: str) -> None:
        if self.chats.closing:
            return  # Preserve interrupted work for the next project startup (§10.11).
        chat = await self.chats.get_chat(chat_id)
        if chat and chat.producer_mode and not chat.producer_paused:
            if chat.error:
                await self._save(chat_id, producer_state="error")
            elif chat.producer_state == "running":
                # A turn ending without a registered task wait hands control to the user.
                await self._save(chat_id, producer_state="awaiting_user")

    async def tasks(self, chat_id: str) -> list[TeamTask]:
        chat = await self._chat(chat_id)
        async with self.chats.db.session() as session:
            member = await session.get(TeamMember, chat.member_id)
            return list((await session.exec(select(TeamTask).where(
                TeamTask.team_id == member.team_id).order_by(TeamTask.number))).all())

    async def resolve(self, chat: AgentChat, reference: str) -> TeamTask:
        async with self.chats.db.session() as session:
            member = await session.get(TeamMember, chat.member_id)
        task = await self.orchestrator.find_task(reference, team_id=member.team_id)
        if not task:
            raise ValueError("This task is not on the Producer's team board.")
        return task

    @staticmethod
    def checklist_items(items: Any) -> list[dict[str, Any]]:
        """The Plan accepts checklist data, never arbitrary Markdown/prose (§10.11)."""
        if not isinstance(items, list) or not 1 <= len(items) <= CHECKLIST_MAX_ITEMS:
            raise ValueError(f"Supply a short checklist of 1–{CHECKLIST_MAX_ITEMS} items.")
        cleaned = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "done"}:
                raise ValueError("Each checklist item must contain only text and done.")
            text = item["text"]
            if (not isinstance(text, str) or not text.strip()
                    or len(text) > CHECKLIST_ITEM_MAX_CHARS
                    or any(character.isspace() and character != " " for character in text)
                    or any(ord(character) < 32 for character in text)
                    or re.search(r"[`<>]|!?\[[^\]]*\]\(|^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|\[[ xX]\])", text)):
                raise ValueError("Checklist text must be plain single-line text of 1–50 characters, without Markdown.")
            if type(item["done"]) is not bool:
                raise ValueError("Checklist done must be a boolean.")
            cleaned.append({"text": text.strip(), "done": item["done"]})
        return cleaned

    async def checklist_revision(self, chat_id: str) -> str:
        # Meaningful board changes invalidate the Plan; costs/timestamps alone do not.
        board = [{key: getattr(task, key) for key in (
            "id", "title", "brief", "assignee_id", "depends_on", "status",
            "result_type", "result_summary", "review_comment", "error",
        )} for task in sorted(await self.tasks(chat_id), key=lambda task: task.id)]
        return hashlib.sha256(json.dumps(board, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def update_checklist(self, chat_id: str, items: Any) -> AgentChat:
        await self._chat(chat_id)
        checked = self.checklist_items(items)
        return await self._save(chat_id, producer_checklist=checked,
                                producer_checklist_revision=await self.checklist_revision(chat_id))

    async def require_checklist(self, chat_id: str, *, current: bool = False) -> None:
        chat = await self._chat(chat_id)
        # Existing ordinary Producer chats can adopt the Plan without a migration gate.
        if not chat.producer_workspace and not chat.producer_checklist:
            return
        if not chat.producer_checklist:
            raise ValueError("Write the session Plan with update_producer_checklist before coordinating tasks.")
        if current and chat.producer_checklist_revision != await self.checklist_revision(chat_id):
            raise ValueError("Tasks changed. Update the session Plan with update_producer_checklist before continuing.")

    def plan_entry(self, task: TeamTask) -> dict[str, Any]:
        return {"id": task.id, "display_id": self.orchestrator.display_id(task),
                **{key: getattr(task, key) for key in (
                    "title", "brief", "assignee_id", "depends_on", "model", "effort", "attachments")}}

    @staticmethod
    def snapshot(task: TeamTask) -> dict[str, Any]:
        return {"id": task.id, **{key: getattr(task, key) for key in (
            "status", "result_type", "result_summary", "review_comment", "error", "num_turns")},
                "started_at": task.started_at.isoformat() if task.started_at else None}

    async def propose(self, chat_id: str, references: list[str], summary: str) -> str:
        chat = await self._chat(chat_id)
        await self.require_checklist(chat_id, current=True)
        if not references or not summary.strip():
            raise ValueError("Supply the plan summary and its task ids.")
        tasks = [await self.resolve(chat, r) for r in dict.fromkeys(references)]
        if any(t.status != TaskStatus.TODO or t.producer_approved for t in tasks):
            raise ValueError("A new plan contains only unapproved Todo tasks.")
        saved = await self._save(chat_id, producer_plan=[self.plan_entry(t) for t in tasks],
                         producer_plan_summary=summary.strip(), producer_state="awaiting_approval",
                         producer_plan_version=chat.producer_plan_version + 1, producer_watch=[])
        if chat.producer_policy == "independent":
            await self._approve_plan(chat_id, saved.producer_plan_version, automatic=True)
            await self._release_tasks(chat_id)
            return "Independent plan approved and released. Continue coordinating, then register a task wait."
        return "Plan saved. Ask the user to review the tasks and press Approve plan and start. End this turn."

    async def approve_plan(self, chat_id: str, version: int) -> AgentChat:
        # Different chats can propose the same task; only one approval may dispatch it.
        async with self.chats.lock(chat_id):
            self._idle(chat_id)
            await self._approve_plan(chat_id, version, automatic=False)
            await self.queue(chat_id, self.chats.config.teams.prompts.text("producer_wake"))
            return await self._chat(chat_id)

    async def _approve_plan(self, chat_id: str, version: int, *, automatic: bool) -> None:
        async with self._approval_lock:
            self.require_resumed(chat_id)
            chat = await self._chat(chat_id)
            if automatic and chat.producer_policy != "independent":
                raise ValueError("Automatic plan approval requires Independent mode.")
            if not chat.producer_plan or chat.producer_plan_version != version:
                raise ValueError("This plan has changed. Review the current plan before approving.")
            async with self.chats.db.session() as session:
                member = await session.get(TeamMember, chat.member_id)
                tasks = []
                for entry in chat.producer_plan:
                    task = await session.get(TeamTask, entry["id"])
                    if (not task or task.team_id != member.team_id or task.producer_approved
                            or task.status != TaskStatus.TODO or self.plan_entry(task) != entry):
                        raise ValueError("A planned task changed. Ask the Producer to propose the updated plan.")
                    self.check_task_cost(chat, task)
                    tasks.append(task)
                for task in tasks:
                    task.producer_approved = True
                    session.add(task)
                # Persist approval and the dispatch intent together. A restart between
                # this commit and dispatch resumes exactly these tasks, once (§10.11).
                stored = await session.get(AgentChat, chat_id)
                stored.producer_release_pending = [t.id for t in tasks]
                stored.producer_plan = []
                stored.producer_plan_summary = ""
                stored.producer_state = "running"
                session.add(stored)
            await self.chats.record(chat_id, chat.member_id, role="assistant" if automatic else "user", kind="producer_plan",
                                    text=chat.producer_plan_summary,
                                    data={"action": "approved", "policy": chat.producer_policy,
                                          "automatic": automatic, "tasks": chat.producer_plan})
            await self.chats._publish_chat_by_id(chat_id)

    async def wait_for_tasks(self, chat_id: str, references: list[str]) -> str:
        chat = await self._chat(chat_id)
        await self.require_checklist(chat_id, current=True)
        tasks = ([await self.resolve(chat, r) for r in dict.fromkeys(references)]
                 if references else [t for t in await self.tasks(chat_id)
                                     if (not t.producer_chat_id or t.producer_approved)
                                     and t.status not in {"done", "cancelled"}])
        if not tasks:
            return "No tasks to wait for. Ask the user what they want to do next; keep Producer Mode active."
        actionable = [t for t in tasks if (t.status not in {"pending", "in_progress"}
                      and not self.chats.jobs.is_running(t.id)) or await self.orphaned(t)]
        if actionable:
            return "Handle these tasks before waiting (an agent in_progress without a job was interrupted; inspect its state and resume within the agreed scope when appropriate): " + json.dumps(
                [self.snapshot(t) for t in actionable], ensure_ascii=False)
        await self._save(chat_id, producer_state="waiting",
                         producer_watch=[{**self.snapshot(t), "is_running": self.chats.jobs.is_running(t.id)}
                                         for t in tasks])
        return "Wait registered. End this turn now. The Studio will resume you when a task changes; do not poll or sleep."

    async def ask(self, chat_id: str, references: list[str], question: str, critical_reason: str = "",
                  *, purpose: str = "execution") -> str:
        chat = await self._chat(chat_id)
        if not question.strip():
            raise ValueError("A question needs text.")
        if purpose not in {"execution", "goal"}:
            raise ValueError("Unknown Producer question purpose.")
        # A new session can clarify its goal even on an already populated team board.
        if purpose != "goal" or chat.producer_checklist:
            await self.require_checklist(chat_id, current=True)
        if chat.producer_policy == "independent" and purpose != "goal" and not critical_reason.strip():
            unfinished = [t for t in await self.tasks(chat_id) if t.status not in {"done", "cancelled"}]
            if unfinished:
                return ("Independent mode: resolve this decision yourself and continue the original goal. "
                        "Only ask for a critical blocker you cannot resolve; supply critical_reason explaining "
                        "attempted alternatives and the user input required. No question was sent.")
        tasks = [await self.resolve(chat, r) for r in dict.fromkeys(references)]
        await self.chats.record(chat_id, chat.member_id, role="assistant", kind="text", text=question)
        asked_ids = {t.id for t in tasks}
        await self._save(chat_id, producer_state="awaiting_user", producer_watch=[],
                         producer_questions=[
                             *[q for q in chat.producer_questions
                               if not q.get("task") or q["task"]["id"] not in asked_ids],
                             *[{"task": self.snapshot(t), "question": question,
                                "critical_reason": critical_reason, "answer_message_id": None} for t in tasks],
                             *([] if tasks else [{"task": None, "question": question,
                                 "critical_reason": critical_reason, "answer_message_id": None}])])
        return "Question shown to the user. End this turn and wait for their reply."

    @staticmethod
    def check_task_cost(chat: AgentChat, task: TeamTask, estimated_additional_cost_usd: float = 0.0) -> None:
        """Check every Producer-controlled dispatch, including edits and plans (§10.11)."""
        if not math.isfinite(estimated_additional_cost_usd) or estimated_additional_cost_usd < 0:
            raise ValueError("Estimated additional cost must be a finite non-negative USD amount.")
        limit = chat.estimate_task_cost_usd
        if (task.total_cost_usd >= limit
                or task.total_cost_usd + estimated_additional_cost_usd > limit):
            raise ValueError(
                f"Estimate Task Cost is ${limit:.2f}; this task has already used ${task.total_cost_usd:.2f} "
                f"and the proposed continuation costs ${estimated_additional_cost_usd:.2f}. "
                "Do not continue this task. Close it by approving verified completed work or cancelling "
                "unfinished work, preserving its result and explaining the budget limit. "
                "Create new tasks for the remaining work and follow the usual plan approval policy."
            )

    async def check_update_cost(self, chat_id: str, task: TeamTask, fields: dict[str, Any]) -> None:
        if task.producer_chat_id and not task.producer_approved:
            return  # Drafts cannot dispatch; their approval checks the budget instead.
        can_start = (task.status == TaskStatus.PENDING and fields.get("auto_start", task.auto_start)
                     and ("auto_start" in fields or "depends_on" in fields))
        if task.status == TaskStatus.TODO and fields.get("status") != TaskStatus.CANCELLED:
            async with self.chats.db.session() as session:
                team = await session.get(Team, task.team_id)
            can_start = bool(team and team.autostart_tasks)
        if can_start:
            self.check_task_cost(await self._chat(chat_id), task)

    async def review(self, chat_id: str, reference: str, action: str, comment: str,
                     estimated_additional_cost_usd: float = 0.0) -> str:
        chat = await self._chat(chat_id)
        await self.require_checklist(chat_id)
        task = await self.resolve(chat, reference)
        if task.producer_chat_id and not task.producer_approved and action != "cancel":
            raise ValueError("This task's plan has not been approved.")
        if self.chats.jobs.is_running(task.id):
            raise ValueError("The task's session is still finishing. Wait for its status update.")
        if action in {"request_changes", "start"}:
            if self.orchestrator.producer_holds(task):
                async with self.chats.db.session() as session:
                    assignee = await session.get(TeamMember, task.assignee_id)
                if assignee and assignee.kind == "agent":
                    raise ValueError(
                        "This task's Producer chat is paused. Resume that chat or run the task manually from the board."
                    )
            self.check_task_cost(chat, task, estimated_additional_cost_usd)
        # §10.11: a Done result closes this unit of work; fixes need a new task.
        if (task.result_type == "done" and task.status in {TaskStatus.NEEDS_REVIEW, TaskStatus.DONE}
                and action != "approve"):
            raise ValueError(
                "A task returned for review with result_type=done can only be approved into Done. "
                "It cannot be restarted or cancelled, including after approval. "
                "Create a new task for any corrections and follow the usual plan approval policy."
            )
        interrupted = await self.orphaned(task)
        # §10.11: the Producer resolves routine blockers and follows existing user
        # instructions without a mandatory question/answer round trip per result.
        if action == "approve":
            if task.status != "needs_review":
                raise ValueError("Only a task awaiting review can be approved.")
            await self.orchestrator.approve(task.id, actor_id=chat.member_id)
        elif action == "request_changes":
            if (task.status not in {"needs_review", "blocked"} and not interrupted) or not comment.strip():
                raise ValueError("Feedback and a task in review or blocked are required.")
            await self.orchestrator.request_changes(task.id, comment, actor_id=chat.member_id)
        elif action == "cancel":
            async with self._approval_lock:
                await self.orchestrator.update_task(task.id, {"status": TaskStatus.CANCELLED}, actor_id=chat.member_id)
                await self._remove_from_plans(task.id)
            if comment.strip():
                await self.orchestrator.record_message(
                    task.id, chat.member_id, role="assistant", kind="text", text=comment.strip(),
                    data={"producer_review": "cancel", "estimate_task_cost_usd": chat.estimate_task_cost_usd},
                )
        elif action == "start":
            if task.status not in {"todo", "pending"}:
                raise ValueError("Only an approved Todo or pending task can be started.")
            await self.orchestrator.update_task(task.id, {"status": TaskStatus.PENDING, "auto_start": True},
                                                actor_id=chat.member_id)
            await self.orchestrator._start_if_ready([task.id])
        else:
            raise ValueError("Unknown review action.")
        await self._save(chat_id, producer_questions=[q for q in chat.producer_questions
                                                      if not q.get("task") or q["task"]["id"] != task.id])
        return f"{self.orchestrator.display_id(task)}: {action} applied."

    async def _remove_from_plans(self, task_id: str) -> None:
        """Cancellation also withdraws the draft from every displayed plan (§10.11)."""
        changed = []
        async with self.chats.db.session() as session:
            for chat in (await session.exec(select(AgentChat))).all():
                plan = [entry for entry in chat.producer_plan if entry["id"] != task_id]
                if len(plan) == len(chat.producer_plan):
                    continue
                chat.producer_plan = plan
                chat.producer_plan_version += 1
                if not plan:
                    chat.producer_plan_summary = ""
                    if chat.producer_state == "awaiting_approval":
                        chat.producer_state = "discussing"
                session.add(chat)
                changed.append(chat.id)
        for chat_id in changed:
            await self.chats._publish_chat_by_id(chat_id)

    async def orphaned(self, task: TeamTask) -> bool:
        if task.status != "in_progress" or self.chats.jobs.is_running(task.id):
            return False
        async with self.chats.db.session() as session:
            member = await session.get(TeamMember, task.assignee_id)
        return bool(member and member.kind == "agent")

    async def _listen(self) -> None:
        while True:
            try:
                await self.reconcile()
                # Bus wakeups are cheap; only a changed durable snapshot calls a model.
                await asyncio.wait_for(self._subscription.get(), timeout=5)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Producer wait reconciliation failed")
                await asyncio.sleep(1)

    async def reconcile(self) -> None:
        async with self.chats.db.session() as session:
            ids = list((await session.exec(select(AgentChat.id).where(
                AgentChat.producer_mode == True,  # noqa: E712
                AgentChat.producer_started == True,  # noqa: E712
                AgentChat.producer_state.in_(["waiting", "running"]),
            ))).all())
        for chat_id in ids:
            lock = self.chats.lock(chat_id)
            if lock.locked():  # This chat can wait for the next pass without delaying others.
                continue
            async with lock:
                if self.chats.jobs.is_running(chat_id) or chat_id in self.orchestrator.paused_producer_chats:
                    continue
                chat = await self.chats.get_chat(chat_id)
                if not chat or not chat.producer_mode or chat.producer_state not in {"waiting", "running"}:
                    continue
                current = {t.id: {**self.snapshot(t), "is_running": self.chats.jobs.is_running(t.id)}
                           for t in await self.tasks(chat_id)}
                changed = any(current.get(s["id"]) != s for s in chat.producer_watch)
                # A review transition is emitted before the task job releases its slot.
                if any(self.chats.jobs.is_running(s["id"]) and
                       current.get(s["id"], {}).get("status") != "in_progress"
                       for s in chat.producer_watch):
                    continue
                if chat.producer_state == "running" or changed:
                    await self.queue(chat_id, self.chats.config.teams.prompts.text("producer_wake"))

    async def context(self, chat_id: str) -> str:
        chat = await self._chat(chat_id)
        tasks = await self.tasks(chat_id)
        # An indefinitely active producer must not grow its injected board without bound.
        # Full briefs/results stay accessible through get_task; prioritize unresolved work.
        active = [t for t in tasks if t.status not in {"done", "cancelled"}]
        closed = [t for t in tasks if t.status in {"done", "cancelled"}]
        board = [{"display_id": self.orchestrator.display_id(t), "title": t.title[:200],
                  "approved": not t.producer_chat_id or t.producer_approved,
                  "automatic_dispatch_paused": self.orchestrator.producer_holds(t),
                  "is_running": self.chats.jobs.is_running(t.id),
                  "total_cost_usd": t.total_cost_usd,
                  "remaining_task_cost_usd": max(0.0, chat.estimate_task_cost_usd - t.total_cost_usd),
                  **{k: v[:600] if isinstance(v, str) else v for k, v in self.snapshot(t).items()}}
                 for t in [*active[-30:], *closed[-5:]]]
        return json.dumps({"state": chat.producer_state, "decision_policy": chat.producer_policy,
                           "effort_mode": chat.producer_effort_mode,
                           "effort_instructions": (
                               "Before every new task is created, Effort Solver evaluates only its title "
                               "and brief, then calculates effort from four scores and its configured weights. "
                               "Do not choose or pass effort yourself. If evaluation fails, no task is created."
                               if chat.producer_effort_mode == "auto" else
                               "New tasks inherit their assignee's configured default effort. "
                               "Do not override effort when creating tasks."),
                           "session_plan": chat.producer_checklist,
                           "session_plan_instructions": CHECKLIST_INSTRUCTIONS,
                           "mode_instructions": (
                               "This chat belongs to the Producer workspace. Keep Producer Mode enabled; "
                               "only deleting the chat stops it. Never call stop_producer_mode."
                               if chat.producer_workspace else ""),
                           "estimate_task_cost_usd": chat.estimate_task_cost_usd,
                           "cost_instructions": self.chats.config.teams.prompts.text("producer_task_cost"),
                           "result_policy": self.chats.config.teams.prompts.text("producer_review"),
                           "policy_instructions": self.chats.config.teams.prompts.text(f"producer_{chat.producer_policy}"),
                           "tasks": board, "total_tasks": len(tasks),
                           "plan": [{"id": t["id"], "title": t["title"][:200]} for t in chat.producer_plan],
                           "questions": [{"task_id": (q.get("task") or {}).get("id"),
                                          "question": q["question"][:600],
                                          "critical_reason": q.get("critical_reason", "")[:600],
                                          "answer_message_id": q.get("answer_message_id")}
                                         for q in chat.producer_questions[-20:]]},
                          ensure_ascii=False)

    async def compact(self, chat_id: str, backend, request, *, force: bool = False) -> bool:
        """Summarize with the selected backend, then rotate its runtime session (§10.11).

        A successful summary replaces the captured transcript atomically. No tools are
        exposed to the summary call; failed/empty summaries keep the last usable context.
        """
        from dataclasses import replace
        chat = await self._chat(chat_id)
        messages = await self.chats.messages(chat_id)
        start = next((i + 1 for i, m in enumerate(messages)
                      if m.id == chat.producer_compacted_through), 0)
        recent = messages[start:]
        history = "\n\n".join(f"[{m.role}/{m.kind}] {m.text}\n" +
                               (json.dumps(m.data, ensure_ascii=False) if m.kind == "tool_use" else "")
                               for m in recent if m.kind not in {"thinking", "state"})
        if not force and len(history) < COMPACT_CHARS:
            return False
        summary_prompt = self.chats.config.teams.prompts.text("producer_compact")
        summary_request = replace(
            request, member=request.member.model_copy(update={"max_turns": 2, "allowed_tools": []}),
            max_turns=2,
            session_key=f"{chat_id}:summary", resume_session_id=None, recovery_prompt="",
            proactive_subagents=False,
            prompt=(f"{summary_prompt}\n\nPrevious summary:\n{chat.producer_summary}\n\nTranscript:\n" +
                    (history if len(history) <= 96_000 else history[:24_000] +
                     "\n[Middle omitted.]\n" + history[-72_000:])),
            system_prompt=summary_prompt, agent_name=None, mcp_servers={}, extra_allowed_tools=[],
            plugin_dirs=[], images=[], max_continuations=0, text_only=True,
        )
        parts: list[str] = []
        fallback = ""
        async for event in backend.run(summary_request):
            if event.kind == "error" or (event.kind == "result" and event.data.get("is_error")):
                raise RuntimeError(event.text or "Producer context compaction failed.")
            if event.kind == "text" and event.text:
                parts.append(event.text)
            if event.kind == "result":
                fallback = event.text
                await self.chats._store_result_stats(chat_id, event.data)
        summary = "\n".join(parts).strip() or fallback.strip()
        if not summary:
            raise RuntimeError("Producer context compaction returned an empty summary.")
        message = AgentChatMessage(
            chat_id=chat_id, member_id=chat.member_id, role="system", kind="producer_compacted",
            text=summary[:SUMMARY_CHARS], data={"replaces_history": True},
            **({"created_at": messages[-1].created_at} if messages else {}),
        )
        # §10.11: commit the replacement and continuation context together. Delete only
        # the captured rows, leaving anything recorded during summarization after it.
        from ..models.task_context import ChatSessionContext

        async with self.chats.db.session() as session:
            stored = await session.get(AgentChat, chat_id)
            if stored is None:
                raise KeyError(chat_id)
            if messages:
                await session.exec(delete(AgentChatMessage).where(
                    AgentChatMessage.chat_id == chat_id,
                    AgentChatMessage.id.in_([m.id for m in messages]),
                ))
            session.add(message)
            snapshot = await session.get(ChatSessionContext, chat_id)
            if snapshot is not None:
                await session.delete(snapshot)
            stored.producer_summary = message.text
            stored.producer_compacted_through = message.id
            stored.agent_session_id = None
            session.add(stored)
        await self.chats._publish_message(message)
        await self.chats._publish_chat(stored)
        return True

    def tools(self, chat_id: str) -> list[Any]:
        from claude_agent_sdk import tool
        from .tools import _text

        task_list = {"type": "array", "items": {"type": "string"},
                     "description": "Task display ids or database ids from anywhere on the team's board."}

        @tool("update_producer_checklist",
              "Write or replace the persistent session Plan. " + CHECKLIST_INSTRUCTIONS,
              {"type": "object", "additionalProperties": False, "properties": {
                  "items": {"type": "array", "minItems": 1, "maxItems": CHECKLIST_MAX_ITEMS,
                      "items": {"type": "object", "additionalProperties": False,
                          "properties": {
                              "text": {"type": "string", "minLength": 1,
                                       "maxLength": CHECKLIST_ITEM_MAX_CHARS},
                              "done": {"type": "boolean"}},
                          "required": ["text", "done"]}}}, "required": ["items"]})
        async def checklist(args):
            await self.update_checklist(chat_id, args.get("items"))
            return _text("Session Plan saved. Keep it current as tasks change.")

        @tool("propose_producer_plan",
              "Present draft Todo tasks as a plan. In Careful, end the turn and wait for explicit user "
              "approval via Approve plan and start. In Independent, this validates, approves and releases "
              "the plan automatically; continue coordinating within the original user goal.",
              {"type": "object", "properties": {"tasks": task_list, "summary": {"type": "string"}},
               "required": ["tasks", "summary"]})
        async def propose(args):
            return _text(await self.propose(chat_id, args.get("tasks") or [], str(args.get("summary") or "")))

        @tool("wait_producer_tasks",
              "Wait for a status or result change without holding a session. Call then END your turn; "
              "the Studio resumes this conversation automatically. Do not poll, sleep or wait in a shell. "
              "Omit tasks to wait on all unfinished team work except unapproved Producer drafts.",
              {"type": "object", "properties": {"tasks": task_list}})
        async def wait(args):
            return _text(await self.wait_for_tasks(chat_id, args.get("tasks") or []))

        @tool("ask_producer_user",
              "Careful: forward every unanswered question arising during execution to the user. "
              "Independent: resolve questions yourself; ask only for a critical blocker you cannot resolve, "
              "with critical_reason explaining attempted alternatives and required user input. "
              "Use purpose=goal ONLY to ask for the initial goal or the next goal after completing the "
              "requested work, never for execution decisions. Never repeat already answered questions. "
              "Include relevant task ids and end your turn only if the question was sent.",
              {"type": "object", "properties": {"tasks": task_list, "question": {"type": "string"},
                                                  "critical_reason": {"type": "string"},
                                                  "purpose": {"type": "string", "enum": ["execution", "goal"]}},
               "required": ["question"]})
        async def ask(args):
            return _text(await self.ask(chat_id, args.get("tasks") or [], str(args.get("question") or ""),
                                       str(args.get("critical_reason") or ""),
                                       purpose=str(args.get("purpose") or "execution")))

        @tool("review_producer_task",
              "Apply a review decision to any task on the team's board, including tasks created "
              "manually or in other chats. Starting Producer drafts still requires plan approval; "
              "cancelling them does not. Approve verified results, request_changes with concrete feedback "
              "or a resolved answer, cancel unnecessary work, or start approved tasks. "
              "A task returned for review with result_type=done may only be approved into Done; "
              "never restart or cancel it, even after approval. Create a new task for corrections "
              "and follow the usual plan approval policy. Resolve routine "
              "questions and blockers according to the current decision policy; follow existing user instructions "
              "without asking again. Do not restart work the user has asked to keep stopped. "
              "Before continuing, compare cumulative task API cost and estimated_additional_cost_usd with "
              "Estimate Task Cost. When insufficient, close the task and split remaining work into new tasks; "
              "approve only verified completed work, otherwise cancel without claiming success.",
              {"type": "object", "properties": {
                  "task": {"type": "string"},
                  "action": {"type": "string", "enum": ["approve", "request_changes", "cancel", "start"]},
                  "comment": {"type": "string"},
                  "estimated_additional_cost_usd": {"type": "number", "minimum": 0,
                      "description": "Estimated API cost of proposed continuation, in USD."}},
               "required": ["task", "action"]})
        async def review(args):
            return _text(await self.review(chat_id, str(args.get("task") or ""),
                                           str(args.get("action") or ""), str(args.get("comment") or ""),
                                           args.get("estimated_additional_cost_usd", 0.0)))

        @tool("stop_producer_mode",
              "Stop Producer Mode only when the USER explicitly asks to stop/finish this mode. "
              "Never stop merely because the current plan is complete; ask what to do next instead. "
              "Existing tasks and conversation history remain. End your turn after calling.",
              {"type": "object", "properties": {}})
        async def stop(args):
            await self.disable(chat_id, cancel=False)
            return _text("Producer Mode stopped. Existing tasks remain on the board.")

        return [checklist, propose, wait, ask, review, stop]


def context_overflow(text: str) -> bool:
    return any(term in text.lower() for term in (
        "context window", "context length", "context_length", "prompt is too long",
        "prompt too long", "maximum context", "too many tokens",
    ))
