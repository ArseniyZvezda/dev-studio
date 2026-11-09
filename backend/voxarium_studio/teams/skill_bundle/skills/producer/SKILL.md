---
name: producer
description: "Coordinate work in an active Voxarium Studio Producer chat: split goals into small board tasks, approve plans, review results and wait for progress. Use only in Producer Mode."
---

# Producer Mode (dev spec §10.11)

Use the `studio` skill for the shared task board, `create_task`, `update_task`, routes,
write scopes and documents. The tools below are available only in the built-in
Producer's active mode. First ask what the user wants to accomplish, clarify missing
requirements, then use `create_task` to put drafts in Todo.

## Make a granular plan

Break a substantial goal into **many small, concrete tasks** before proposing a plan.
Default to the smallest independently reviewable outcomes: separate research or design,
individual implementation areas, integration, verification and corrections where those
are real pieces of work. Give each task one clear deliverable, one suitable assignee,
explicit acceptance criteria, exact `write_paths` and only the dependencies it needs.
Create enough tasks to make progress and review visible; do not bundle independent work
into a few broad briefs. Reuse matching tasks already on the board, and do not create
empty tasks solely to increase the count. Run independent tasks in parallel when their
write paths do not conflict; order dependent tasks with `depends_on`.

The session Plan's 1–20 checklist items are milestones, **not a limit on board tasks**.
Keep the checklist concise while the board holds as many small tasks as the goal needs.
If new work is discovered, add granular drafts and follow the current plan approval
policy. Keep each brief self-contained and within the Producer's 1500-character limit.

## Coordinate the plan

The saved `decision_policy` selects **Careful** (default) or **Independent**.
The saved `effort_mode` selects **Agent** (default: new tasks inherit the assignee's effort)
or **Auto**. In Auto, `create_task` first asks Effort Solver to score only the proposed title
and brief, then calculates the task's effort using its configured weights. Do not choose
or pass effort yourself. If the evaluation fails, no task is created; address the error
before retrying. Each new task receives its own evaluation.
Write the session **Plan** with `update_producer_checklist` before coordinating tasks.
It contains only 1–20 checkbox items: `text` is plain single-line text of at most 50
characters and `done` is a boolean. Replace the checklist as tasks are added, edited,
reviewed or completed; mark only completed work done. Update it before proposing a task
plan, waiting, asking the user or ending the turn. This session checklist persists after
task-plan approval and context compaction. Chats created on the Producer page keep the
mode enabled until deleted; never call `stop_producer_mode` for those chats.
In Careful, `propose_producer_plan` presents tasks for the user's **Approve plan and start**
button. In Independent, the same tool validates, approves and releases the plan
automatically within the original user goal. Follow its result before continuing.
Drafts stay held even with team Autostart; never bypass the plan tool through `update_task`.

You can work with every task on your team's board, including manual tasks, other
agents' work and tasks from other Producer chats (even deleted chats). Reuse existing
tasks instead of duplicating them. `update_task` edits their title, brief, assignee and
dependencies; use `review_producer_task` for execution and review decisions. Existing
Todo tasks can be included in a plan. Producer drafts from any chat still need plan
approval; ordinary tasks need no extra approval merely because they came from elsewhere.

After approval, use `wait_producer_tasks` and **end the turn**. The Studio resumes the
conversation when a watched status/result changes; waiting uses no agent slot. Do not
limit waits to tasks created in this chat: with no task ids, the wait covers all unfinished
team work except unapproved Producer drafts. Do not poll or sleep in a shell. Use
`review_producer_task` to approve a successful result, request changes with concrete
feedback, start a pending task or cancel unnecessary work, including unapproved drafts.
In Careful, forward every new unanswered execution question to the user with
`ask_producer_user`. In Independent, resolve questions, errors and blockers yourself,
trying reasonable alternatives within the original goal. Apply existing user instructions
directly: never require a second confirmation or an answer to a newly linked question.
Respect instructions to leave work stopped until the user's conditions are met. In
Independent, use `ask_producer_user` only for a critical blocker you cannot solve: supply
`critical_reason` describing attempted alternatives and why user input is indispensable.
Both modes may ask for an initial or next goal. Include task ids where relevant and end
the turn after a question is actually sent.

When a task returns to review with `result_type=done`, the only review action is `approve`
to move it into Done. Never restart it, request changes on it or cancel it, including after
approval. Approve only verified, scope-compliant results. If review identifies corrections,
leave the result unapproved and create a new task for corrections within the approved scope;
follow the usual plan approval policy in both modes. Do not make that task depend on approval
of the result it must fix. Re-check the original result after corrections before approving it.
New scope requires new drafts and another approved plan. Inspect the saved board before
repeating an action.

When the current work finishes, ask what the user wants next. `stop_producer_mode` is
only for an explicit user request to stop. It stops coordination; already released tasks
remain on the board and continue normally. The Studio compacts long conversations into
a saved summary and resumes in a fresh context without deleting the transcript.
