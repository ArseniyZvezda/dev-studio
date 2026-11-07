# The Studio's files in the project

The project the Studio has open is the game itself. The Studio keeps its own data inside it, so the
team's AI workflow is versioned together with the game.

## `.voxarium_studio/` — the Studio's data directory

| Path | What it is | Yours to change? |
| --- | --- | --- |
| `.voxarium_studio/docs/` | The team's shared documents. | Yes — through `write_doc`, or as files when the Studio's tools are off. |
| `.voxarium_studio/pipelines/*.yaml` | AI pipelines, edited and run on the Pipelines page. | When the task is about a pipeline — see below. |
| `.voxarium_studio/teams/*.yaml` | The team roster: members, roles, who reports to whom, models, tools, skills. | No. People edit it on the Teams page, and it decides who you are and where you may route work. |
| `.voxarium_studio/skills/<name>/` | The project's skills: a `SKILL.md` (a `description` in its frontmatter, then instructions) and the files it refers to. A skill reaches only the members it is given to; yours, if any, are listed in your system prompt with their paths. | Only when the task is about a skill — and a skill's name is its folder's, so renaming the folder takes it away from everyone who has it. |
| `.voxarium_studio/automations.yaml` | What the project does by itself: one trigger and one action per automation. Enabled entries listen while the project is open; changing a task or writing a watched file may start an action. | When the task calls for automations, use the MCP tools described in SKILL.md. People also edit these on the Automations page. |
| `.voxarium_studio/config.yaml` | Project settings: plugins, appearance, limits, the prompts sent to agents. | No — Settings and Teams → Team Settings own it. |
| `.voxarium_studio/app.db` | The database: tasks, transcripts, runs. | Never. |
| `.voxarium_studio/runs/`, `.voxarium_studio/assets/` | Run logs and generated artifacts. | Read only. |
| `.voxarium_studio/worktrees/` | Isolated checkouts, one per task. | Only the one your system prompt names as yours. |
| `.voxarium_studio/plugins/` | Studio plugins local to this project. | Only when the task is about one. |

`app.db`, `assets/`, `runs/` and `worktrees/` are kept out of version control; everything else in
`.voxarium_studio/` is meant to be committed.

## At the project root

- **`.claude/agents/*.md`** — the Studio writes one per agent member from the roster, and you are
  running as yours. A definition carrying a `voxarium_studio_member` key is rewritten on the next roster
  change, so never edit one; definitions without that key belong to people.
- **`.mcp.json`** — the project's MCP servers, shared with the developer's terminal. Their tools
  reach you as `mcp__<server>__<tool>`. The Studio's own `voxarium_studio` server is not in this file and
  must not be added to it.
- **`graphify-out/`** — the code graph the Studio's graphify plugin keeps up to date. Query it; never
  edit it.

## Pipelines

A pipeline is a YAML file in `.voxarium_studio/pipelines/`, named after its `id`. Prefer
`create_pipeline` / `write_pipeline`: they validate against the live node registry before saving.
The Pipelines page reads these same files. Saving does not start a run; a person can run it
from that page, or an enabled automation can start it on a matching event.

```yaml
id: export-notes
name: Export notes
description: Copies every .txt under Assets/Notes to the same place under Export.
nodes:
  - id: walk
    type: core.for_each_file
    position: { x: 0, y: 0 }
    params: { directory: Assets/Notes, extensions: .txt, recursive: true }
  - id: target
    type: core.reparent_path
    position: { x: 260, y: -80 }
    params: { parents: 1, new_parent: Export }
  - id: read
    type: core.read_text_file
    position: { x: 260, y: 80 }
  - id: write
    type: core.write_text_file
    position: { x: 520, y: 0 }
edges:
  - from: { node: walk, port: path }
    to: { node: target, port: path }
  - from: { node: walk, port: path }
    to: { node: read, port: path }
  - from: { node: read, port: text }
    to: { node: write, port: text }
  - from: { node: target, port: path }
    to: { node: write, port: path }
```

- A node's `type` is `<plugin>.<node>`. `params` sets its inputs by port id; an edge carries one
  node's output port into another's input, and wins over a param of the same name.
- Optional on a node: `label`, `pins` (which inputs are drawn as connectable), `pause_on_complete`
  (hold the run after this node until someone continues it) and `disabled` (skip it).
- A looping node such as `core.for_each_file` answers with a list, and everything downstream of it
  runs once per item.
- Node types come from the Studio's enabled plugins. Ask `list_node_types` for their schemas:
  an unknown type or port fails validation before anything runs.

## The Studio, as the user sees it

The sections of the rail, for when someone asks where to find something:

- **Explorer** — the project's files, with version-control marks.
- **Documents** — the shared documents.
- **Pipelines** — the AI pipelines and their runs.
- **Tasks** — the task board.
- **Teams** — the org chart, every member's settings, and the Team Settings panel to its left: shared Permission mode, Max turns and Agent prompts. Individual run settings apply only with Override Team Settings enabled.
- **Statistics** — what the team's work has cost.
- **Automations** — what the project does by itself: trigger → action, and a log of what fired.
- **Version Control**, **Skills** (Studio and installed agent skills, their sources and assignments), **MCP Servers**,
  **Plugins**.
- **Settings** — appearance, execution limits and other project settings.
