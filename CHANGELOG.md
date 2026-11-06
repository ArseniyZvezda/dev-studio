# Changelog

## v5.4.0 — 2026-11-06

- Added the built-in Effort Solver agent with configurable scoring weights. Producer Auto effort now evaluates each proposed task before creation, maps the result to Low–Max and shows the four scores and final effort in a purple conversation block.
- Split global Settings from Project Settings. Language, appearance, Explorer visibility, node-cache reuse and update preferences follow the user across projects; the accent and project workflow settings remain project-specific. Existing preferences migrate automatically.
- Added required startup skills and full Documents content at team and member level, including task, chat and subagent runs. Missing requirements prevent a run from starting, and required skills are protected from deletion.
- Moved shared agent run settings and prompts to Team Settings, with explicit per-member overrides. Added discovery of installed Studio, global, project and backend-specific skills, plus a searchable picker with source and backend filters.
- Added a protected human Director role and safer member removal: active work is stopped and tasks, reporting relationships and chat history can be transferred to Director after confirmation.
- Made Teams continuously editable and improved autosave for member settings, team defaults and prompts, including changes made while a save is in progress.
- Improved Producer's first-message lifecycle so empty conversations do not pause or resume teams. Updated policy changes, startup-state migration, controls and task-card model/effort details.
- Added a shared effort slider for member and task settings, and backend readiness indicators for Claude Code, Codex and Qwen Code.
- Corrected duplicate token accounting and separated input, output, cache-read and unclassified usage. Added an offline repair tool with evidence and partial-recovery reporting for historical records.
- Unified token-usage chips across task details, Producer cards and subagents. Added daily average task duration and clearer loading, retry and partial-data states in Statistics.
- Fixed installer version labels for commit builds and added confirmed switching between commit and release backends while preserving the open project.
- Updated English and Russian interface text and expanded coverage for settings migration, agent lifecycle, required context, effort calculation and usage accounting.

## v5.3.0 — 2026-11-05

- Added persistent task subagents across agent backends, with MCP lifecycle controls, restart recovery and combined usage accounting.
- Added live token usage to tasks and Producer cards, plus token and API-cost statistics with daily averages.
- Added independent Git, Plastic SCM and Perforce settings, including task-scoped Perforce submits.
- Changed task conflict locks to use explicitly declared write paths, allowing tasks that only reference the same files to run concurrently.
- Added a single task summary in the selected interface language, preserved through continuations and delegation.
- Unified desktop, backend and frontend version identity using the exact Git tag or commit, and improved startup recovery diagnostics.
- Bound the built-in Producer's read-only responsibility to its configured system prompt.

## v5.2.0 — 2026-11-04

- Added a Windows installer build script and local build instructions.
- Fixed installer packaging to include the backend, web interface and built-in plugins.
- Bounded file observation before agent startup, with preparation progress, cancellation and safe handling of incomplete scans.

## v5.0.0 — 2026-11-04

Public release of Voxarium Studio.

- AI pipelines, plugin support and project file previews.
- Claude Code and Codex agent teams, task management and Producer Mode.
- English/Russian web interface and Windows desktop application.
