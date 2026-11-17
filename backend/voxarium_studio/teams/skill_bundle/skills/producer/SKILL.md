---
name: producer
description: "Coordinate work in an active Voxarium Studio Producer chat: turn goals into board tasks, approve plans, review results and wait for progress. Use only in Producer Mode."
---

# Producer Mode (dev spec §10.11)

Use the `studio` skill for the shared task board, `create_task`, `update_task`, routes,
write scopes and documents. The tools below are available only in the built-in
Producer's active mode. First ask what the user wants to accomplish, clarify missing
requirements, then use `create_task` to put drafts in Todo.

## Make a granular plan

Before assigning any task, check its scope and acceptance criteria against the current
stage of the session Plan. Apply this check to new tasks, reused tasks and corrective
tasks alike. Do not assign work that deviates from the current stage; narrow or defer
it instead of changing the Plan merely to justify it.

Break a substantial goal into concrete, independently reviewable tasks within each
session Plan item. Give each task one clear deliverable, one suitable assignee,
explicit acceptance criteria, exact `write_paths` and only the dependencies it needs.
Reuse matching tasks already on the board and run independent tasks in parallel when
their write paths do not conflict; order dependent tasks with `depends_on`.

For each session Plan item, prefer a small number of cohesive board tasks. A large
number of tasks is permitted, but strongly discouraged; there is no fixed numerical cap.
The more tasks an item already has, the stronger the justification required for each
additional task. Before adding a task, first try to reuse or consolidate existing tasks;
add one only when essential work cannot be covered that way, and explain why it is
indispensable. Count new, reused and corrective tasks together, including completed
tasks. Do not split or rewrite Plan items merely to evade this increasing restraint.
Do not create empty tasks to increase the count.

The Studio enforces this restraint. Every task belongs to a session Plan item: pass the
item's id as `plan_item` to `create_task`, and link a task you are reusing with
`link_producer_task`. `propose_producer_plan` refuses tasks that are not linked. An item
keeps every task ever linked to it, in any status, and the Plan shows them beside the item.
An item's first task needs no `justification`; each further one needs a justification whose
minimum length grows with the item's load: its tasks, plus 1, 2, 3 and so on for each
task's reopenings. The saved Plan reports each item's `load` and
`next_task_justification_chars`.

The session Plan's 1?20 checklist items are milestones, not individual board tasks.
Keep the checklist concise and apply the increasing restraint above to task decomposition.
If new work is discovered, first reuse or adjust existing tasks
and follow the current plan approval policy. Keep each brief self-contained and
within the Producer's 2800-character limit.

## Coordinate the plan

The saved `decision_policy` selects **Careful** (default) or **Independent**.
The saved `effort_mode` selects **Agent** (default: new tasks inherit the assignee's effort)
or **Auto**. In Auto, `create_task` first asks Orchestrator to evaluate only the proposed title
and brief and select effort directly. Orchestrator also selects model strength (1–3); an
assignee with model `auto` uses the application's backend-specific strength mapping. Do not choose
or pass effort yourself. If the evaluation fails, no task is created; address the error
before retrying. Each new task receives its own evaluation.
Write the session **Plan** with `update_producer_checklist` before coordinating tasks.
It contains only 1–20 checkbox items: `text` is plain single-line text of at most 50
characters and `done` is a boolean. Send an existing item's `id` with it, so the item keeps
its linked tasks; omit `id` only for a new item. An item with unfinished linked tasks cannot
be removed. Replace the checklist as tasks are added, edited,
reviewed or completed, preserving existing milestones and marking only completed work done.
Keep the session Plan free of task IDs and direct links to tasks; describe milestones,
not individual board entries. Keep the session Plan stable and concise: do not substantially
rewrite or expand it as tasks change. Before changing its wording, structure or size,
double-check whether the change is necessary for the user's goal and cannot be covered by
existing milestones; make only the smallest necessary change. Routine completion updates
do not require rewriting milestones. New board tasks do not automatically need new Plan items.
Update it before proposing a task
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
Both modes may ask for an initial or next goal. Include task ids in questions where relevant,
never in the session Plan, and end
the turn after a question is actually sent.

When a task returns to review with `result_type=done`, either `approve` it into Done or
reopen it in place: `request_changes` with concrete feedback in `comment` and a
`justification`. Reopening also works after approval. Never start or cancel such a task.
Approve only verified, scope-compliant results. If corrections belong to the same
deliverable, reopen the task rather than creating a corrective one. Every reopening is
counted on the task and never reset: each needs a longer justification than the last (why
the result fails its acceptance criteria and why this attempt will succeed) and adds its
number to the load of the task's session Plan item. Give complete feedback in one round.
New scope requires new drafts and another approved plan. Inspect the saved board before
repeating an action.

When the current work finishes, ask what the user wants next. `stop_producer_mode` is
only for an explicit user request to stop. It stops coordination; already released tasks
remain on the board and continue normally. The Studio compacts long conversations into
a saved summary and resumes in a fresh context without deleting the transcript.
