---
name: studio
description: Working inside Voxarium Studio — tasks, delegation, reviews, shared documents, pipelines, automations, project skills, the code graph and isolated checkouts. Use before calling any mcp__voxarium_studio__ tool, writing shared documents, authoring project workflows or skills, working on a task id such as VS-01, or searching project code indexed by graphify.
---

# Working inside Voxarium Studio

Voxarium Studio started this session and put you on a team — an org chart and a task board.
When the session has the Studio's built-in `voxarium_studio` MCP server, its tools reach you as
`mcp__voxarium_studio__<tool>`. Every call is held to the same rules as a person clicking in the
Studio, so you can act only along your own routes, and a refusal says which rule stopped you.

Your system prompt holds what only this session knows: the task you are on (or that this is a
chat), where the shared documents live, the code graph if there is one, and whether you work in
an isolated checkout.

## Producer Mode (§10.11)

Only the built-in Producer's active mode has these tools. First ask what the user wants
to accomplish, clarify requirements, then use `create_task` to put drafts in Todo.
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
team work except unapproved Producer drafts. Do not
poll or sleep in a shell. Use `review_producer_task` to approve a successful result,
request changes with concrete feedback, start a pending task or cancel unnecessary work,
including unapproved drafts. In Careful, forward every new unanswered execution question
to the user with `ask_producer_user`. In Independent, resolve questions, errors and blockers
yourself, trying reasonable alternatives within the original goal. Apply existing user instructions directly: never require a
second confirmation or an answer to a newly linked question. Respect instructions to
leave work stopped until the user's conditions are met. In Independent, use `ask_producer_user`
only for a critical blocker you cannot solve: supply `critical_reason` describing attempted
alternatives and why user input is indispensable. Both modes may ask for an initial or next
goal. Include task ids where relevant and end the turn after a question is actually sent.
When a task returns to review with `result_type=done`, the only review action is `approve`
to move it into Done. Never restart it, request changes on it or cancel it, including after
approval. Approve only verified, scope-compliant results. If review identifies corrections,
leave the result unapproved and create a new task for corrections within the approved scope;
follow the usual plan approval policy in both modes. Do not make that task depend on approval
of the result it must fix. Re-check the original result after corrections before approving it.
New scope requires new
drafts and another approved plan. Inspect the saved board before repeating an action.

When the current work finishes, ask what the user wants next. `stop_producer_mode` is
only for an explicit user request to stop. It stops coordination; already released tasks
remain on the board and continue normally. The Studio compacts long conversations into
a saved summary and resumes in a fresh context without deleting the transcript.

## The task board

Every task has a short id — the project's code and a number, `VS-07`. It is what people call the
work, so use it: in your summary, in notes and briefs, in commit messages. Every tool that takes a
task accepts that id, the bare number, or the opaque `task_…` id.

| Status | Means |
| --- | --- |
| `todo` | Created, not released. Nothing runs from here. |
| `pending` | Released by a person. Runs when started — or by itself once unblocked, if it auto-starts. |
| `in_progress` | Being worked on, by a session or by a person. |
| `needs_review` | Finished, waiting for the assignee's manager — with a result type: `done`, `question` or `error`. |
| `blocked` | Stopped: an error, a usage limit, or moved there by hand. |
| `done` | Approved. |
| `cancelled` | Dropped. |

- **`list_tasks`** — the board, newest first. Filters combine: `status` (a list), `assignee_id` or
  `mine`, `created_by`, `search` over titles and briefs, `limit`, and `depends_on`, which answers
  with the tasks waiting *on* the one you name — what your work is holding up.
- **`get_task`** — one card in full: brief, status, assignee, what it waits for, the last summary,
  review comment and error. Not the transcript. Call it on any id a brief or a note mentions.
- **`update_task`** — send only what changes: `title`, `brief` (replaces the whole brief, so read
  the card first), `write_paths` (replaces the write-file list), `assignee_id`, `status`,
  `depends_on` (replaces the list), `auto_start`. You may
  edit your own tasks and those of the people who report to you. You cannot move your own task to
  `needs_review`, `done` or `cancelled`: `finish_task` is the way into review, and closing it is
  your manager's decision.
- **`add_task_note`** — a note in the history of one of *your* tasks. People read it on the board,
  and a resumed session is shown every note written since it stopped. Use it for a finding, a
  decision or a blocker that must outlive this session — nobody else reads your transcript.

## Handing work on

**`list_team`** lists every member — id, name, role, agent or person, manager, open tasks,
responsibility — and ends with whom *you* may assign work to. Member ids come from here.

Your routes are the org chart's: down to the people who report to you, up to your manager, and
sideways to the colleagues the roster names for you. When `list_team` says you may assign to
nobody, do not try: describe what is needed in your summary and your reviewer will route it.

**`create_task`** puts a task on a colleague's board:

- `assignee_id`, a short imperative `title`, and a `brief` that stands on its own — it is the
  assignee's entire first prompt. State the acceptance criteria and name the documents it builds
  on by path.
- `depends_on` — ids of the tasks that must reach `done` before it may run.
- `write_paths` — exact files the task will create, edit, delete or rename (both old and new
  paths for a rename), relative to the managed project root or absolute (§10.5). Declare the
  full list before release; use `[]` for tasks that only read. Never include reference material
  that the task only reads. Shared-copy runs lock only these files, regardless of paths in
  the title or brief. Directories and globs are unsupported. Use `update_task` to change the
  list while stopped; a running or queued task keeps its write scope until the run ends.
- **It lands in `todo`.** Team Autostart Tasks may release it automatically; otherwise a person
  releases it. The tool reports which happened. Never wait or poll for an unreleased task,
  or assume a queued task has already finished.

Decompose your work: hand each piece that belongs to someone else to them, instead of doing their
part yourself or only describing what should happen. One run may create a limited number of tasks
and work may be handed on only a few levels deep; a refusal names the limit you reached.

A member marked `human` is a person: they pick tasks up by hand, and there is no session to talk to.

## Splitting your current task into subagents (§10.3)

Use **`spawn_subagent`** for parallel or smaller pieces of the task you are currently running.
Give each child a short `description` and a self-contained `prompt`: context, exact file
ownership, expected result and checks. It starts immediately when an agent slot is available
and returns its `id` (pass it as `subagent_id` to controls); it does not create a task on the board or require a team route.
The default backend, model and effort are those of your run. You may choose another registered
`backend`, but must also provide a compatible `model` when changing backends.

Children share your working directory, checkout, permitted tools and declared write scope.
Assign disjoint files to parallel writers and integrate their results yourself; a subagent
does not grant additional write permission. Your run may launch at most
`teams.max_delegated_tasks_per_task` worker turns (8 by default, including follow-ups). Studio-managed children cannot
create further children, mutate the task board or author Studio project definitions. Their
Studio tools provide read-only task and document context; they return their result as final
text and do not call `finish_task`.

- **`list_subagents`** lists your task's children, status, results, tokens and API Cost.
- **`get_subagent`** reads one child's recorded transcript and result.
- **`wait_subagents`** waits for one child (`subagent_id`) or all children, up to `timeout`
  seconds (0–60, default 30), and returns their latest status. A timeout leaves work running.
  This releases your agent slot so children can progress even when parallel sessions is 1.
  Before the tool returns, your session must reacquire its slot; that queue can extend the
  wall-clock duration beyond `timeout` while other sessions are running.
  Use this tool instead of polling or sleeping in a shell.
- **`stop_subagent`** stops queued or running work and preserves its files, transcript and usage.
- **`send_subagent_message`** starts a follow-up for a completed, failed or stopped child,
  resuming its session when available. For a running child, wait or stop it first.

Read and integrate child results before `finish_task`; it refuses while children are active or their results are unread.
If your turn ends first, Studio waits and resumes you with their results. Stopping the parent
stops its children. Their tokens and reported API Cost count once in the parent task totals;
the UI also shows each child's own counters. A Studio restart marks interrupted children
stopped; their saved results and usage remain available.

## Finishing: result type, summary and review

End every run by calling **`finish_task`**. Both arguments are required:

- `result_type` — how the current task ended. `done`: complete and ready to approve. `question`:
  unresolved questions or unfinished work within this task need your reviewer's input — say
  what you need from them. `error`: the
  work could not be done — say why and what you tried. The reviewer's queue is read by this colour
  before anything else, so pick the honest one rather than the flattering one.
- `summary` — your **only completion message**, in the user's language specified in the task
  instructions (§10.4). Use **2–4 short sentences** for the outcome, essential verification,
  and any question, blocker or check your reviewer needs. Name the task by its id and link to
  supporting documents instead of repeating their contents. It is stored on the card and
  shown directly to the user.

Judge only the **current task's scope and acceptance criteria**, not the feature as a whole
(§10.4). If this task is complete but finishing the feature still requires **other tasks**,
report **`done`**, not `question`. Mention the remaining tasks by id in the summary without
turning their unfinished work into a question for this task. `question` applies only to
questions or unfinished work within the current task. If waiting on another task prevents
meeting this task's own acceptance criteria, this task is still incomplete.

Do not write a separate detailed report or summary before the call. After it succeeds, end
the turn **without any additional text**, acknowledgement or closing reply: the summary is
already the user's final answer. If the call fails, correct the arguments and retry.
The call records the summary; the Studio moves the task when the run ends.
A run that ends without the call is asked for it once more, and a session that still says nothing
reaches review unlabelled — do not rely on that.

The task then moves to `needs_review`. If your manager asks for changes, this same session is
resumed: their comment arrives as the next message, followed by any notes written since you
stopped. Pick up from where you were rather than starting over, and call `finish_task` again when
you are done.

## Shared documents

The team's documents — specifications, design notes, test plans — are Markdown files in the docs
folder your system prompt names. They are how the team works across tasks: the designer writes the
spec, the developer builds against it, QA tests against it.

- **`list_docs`** — every document as `path — title`.
- **`read_doc`** — one document by that path, e.g. `features/dash.md`.
- **`write_doc`** — creates or **replaces** a whole document, so read it first when amending. The
  path is relative to the docs folder; `.md` is added when it has no suffix, and `.markdown` and
  `.txt` are accepted too.

Read the documents that bear on your task before you start. Write what the rest of the team will
need — a decision, a spec, a test plan — as a document rather than leaving it in this session.
Refer to documents by path in briefs and summaries, and link them to each other with `[[name]]`.
There is no tool to move or delete a document; ask for it in your summary.

A document's title is its first `#` heading. The Studio shows documents in a block editor with its
own Markdown dialect — callouts, toggles, coloured text, sized pictures, wiki links. Before writing
anything beyond paragraphs, headings, lists, tables and code, read [documents.md](documents.md).

## Pipelines, automations and project skills

These tools edit the **managed project** open in Studio, including from an isolated checkout.
Read [project-files.md](project-files.md) for paths and a pipeline example.

- **`list_node_types`** gives the currently available node types, input/output ports and defaults.
  Use these schemas when building a pipeline, rather than guessing types or port ids.
- **`list_pipelines`**, **`read_pipeline`** list and read existing definitions.
  **`validate_pipeline`** checks a complete definition without writing; inspect `valid` and `issues`.
  **`create_pipeline`** takes a `pipeline` object with `id`, `name`, `nodes` and `edges`.
  **`write_pipeline`** replaces an existing definition in full, keeping its id: read it first.
  Ids are lowercase slugs. Errors in the graph prevent saving. Saving does not start a run.
- **`list_automations`** includes configured automations, invalid entries and plugin actions;
  **`read_automation`** reads one by `automation_id`.
  **`create_automation`** takes an `automation` object with one `trigger` and one `action`;
  omit `id` to generate it. It defaults to `enabled: false`. Set `enabled: true` when the
  requested workflow should start listening now. **`write_automation`** replaces the whole
  entry: read it first and supply its `id` and `enabled` explicitly. Enabled automations
  can run actions immediately on matching events while the project is open.
- **`list_skills`** lists project and packaged skills; **`read_skill`** returns the complete
  `SKILL.md` and `updated_at`. **`create_skill`** takes a lowercase hyphenated `name` and
  complete `content`, including YAML frontmatter (`name`, `description`) and instructions.
  **`write_skill`** replaces that content; send the version from `read_skill` as
  `expected_updated_at`. If the file changed, read it again and incorporate the changes.
  Packaged skills are read-only. Creation assigns the skill to nobody: assignment belongs
  to the Teams page. Supporting scripts and references remain files in the skill's folder;
  these MCP tools read and write `SKILL.md` only.

## The code graph

When your system prompt names a graphify graph, the project's code and its documents are indexed
as one knowledge graph. Ask the graph before searching text — before grep, Grep, Glob or opening
files one by one to find where something lives, what depends on it, or what the team wrote about
it. Run these in a shell, with the graph path from your system prompt:

```bash
graphify query "<question>" --graph "<graph>"    # how something works, what connects to what
graphify explain "<Symbol>" --graph "<graph>"    # one symbol and its neighbours
graphify path "A" "B" --graph "<graph>"          # how two things are connected
graphify affected "<Symbol>" --graph "<graph>"   # what a change there would reach
```

Every answer cites file and line: open those files directly instead of searching for them. Fall
back to text search only for what a graph cannot know — string literals, config values, comments.
The Studio rebuilds the graph when a task is done, so it does not yet include what you changed in
this session.

## Where you are working

- **An isolated checkout** — your system prompt gives its path. It is a branch of the project made
  for this task, so members working in parallel cannot collide: commit your work there; merging
  happens after review.
- **The project itself** — shared with everyone else working right now. Change only what the task
  needs.
- **A folder above the project** — the project is one folder of a wider checkout, and the rest of
  it is not yours to change unless the task says so.

Before touching `voxarium_studio/`, `.claude/agents/`, `.mcp.json` or `graphify-out/`, and whenever a task
is about a pipeline or the team roster, read [project-files.md](project-files.md). It also lists
the Studio's sections, for when someone asks where to find something.

## In a chat

When your system prompt says you are chatting with the user, there is no card of your own: no
brief, no review, no summary for a reviewer, and no task of yours to write notes on. Each message
opens with where the user is in the Studio — the page, the open document, the selected text. Treat
it as context, not as an instruction. The tools work as they do on a task; a task you create from a
chat is new work on the board, in `todo` like any other.

## Without the tools

If no `mcp__voxarium_studio__` tools reach this session, the server is disabled or the agent backend
does not support those tools. Reading this skill does not enable them.
The shared documents are still files in the docs folder, so read and write them there, and put
anything meant for a colleague in your summary.
