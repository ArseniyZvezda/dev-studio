# Voxarium Studio

Local web hub for AI game-dev pipelines, plugins and agent teams. FastAPI backend +
Vue 3 SPA, shipped as one pip package that serves both from one process on one port.

The design spec in `docs/voxarium-studio-dev-spec.md` is the source of truth for intent.
Code comments cite it by section (`§8.2`, `§10.4`); keep that convention when adding
code that implements a spec decision, and prefer amending the spec over quietly
diverging from it. Keep intentional scope limitations explicit in the spec.

`docs/plugins.md` is the plugin-author guide: manifest format, the three execution
kinds, the node-script wire protocol, and the frontend page contract.

`docs/block-editor-spec.md` is a second, narrower spec: it replaces the Documents
section's three views with one Notion-style block editor over seven staged PRs. It
overrides the dev spec's §10.4 reasoning about that section, and its §2 decisions are
fixed — cite it as `block-editor spec §N` so the two are never confused.

## Three things called "project"

This distinction runs through the whole codebase — getting it wrong produces confusing
names and misplaced files:

| Term | Meaning |
|---|---|
| **the Studio** | this application, this repository |
| **managed project** | the user's game project the Studio is pointed at (`voxarium_studio run <path>`) |
| **`voxarium_studio/`** | the Studio's data directory *inside* the managed project — no leading dot |

The Studio keeps no global machine state beyond the plugin registry and the list of
recently opened projects (paths only — §5.1; `$VOXARIUM_STUDIO_HOME` relocates both). Runs, logs,
pipelines, team definitions, the team's shared documents and the SQLite database all live in
the managed project's `voxarium_studio/` directory, so a project is portable and its AI workflow is
diffable in git. The one file the Studio writes *outside* it is `.mcp.json` at the project
root — Codex's own MCP server config, shared with the developer's terminal (§10.7).

Which project is open is decided at runtime, not only by `voxarium_studio run <path>`: opening
another one from the UI builds a second `AppContext` and retires the first
(`backend/voxarium_studio/core/workspace.py`, §5.1). Two invariants there are load-bearing — the
replacement starts *before* the incumbent is shut down, and the event bus is carried over
rather than recreated, because connected browsers are subscribed to that object.

## Commands

```bash
# backend (from backend/)
uv pip install -e ".[teams,dev]"
python -m voxarium_studio.cli run <managed-project> --dev   # --dev enables CORS for Vite

# frontend (from frontend/)
npm run dev        # http://localhost:5173, proxies /api and /ws to the backend
npm run build      # type-checks, then writes ../backend/voxarium_studio/static
npm run typecheck
npm test           # vitest, node environment: lib/md, lib/editor, lib/status, stores/docs, i18n
```

`voxarium_studio info <managed-project>` is the fastest way to see what the Studio actually
loaded — config, every plugin with its load status and error, node types, pipelines —
without starting a server. `voxarium_studio init` creates `voxarium_studio/` alone; `voxarium_studio token`
prints a fresh secret for `--token`.

`npm run build` output lands **inside the Python package** (`backend/voxarium_studio/static`),
which is what `voxarium_studio.main.static_dir()` looks for first and what the wheel ships. It
is generated, gitignored, and must never be edited by hand.

## One instance, fixed ports, started by `run.ps1`

`run.ps1` is the only thing that starts a server here; `run.cmd` wraps it for developers and Electron invokes it hidden. It
owns the fixed ports — backend `:8765`, Vite `:5173` — kills whatever it started last
time along with any orphan still holding those ports, records PIDs and logs in
`.scratch/run/`, and waits for `/healthz` before reporting.

```bash
run.cmd           # backend only, :8765, reopens the project that was open last
run.cmd -Dev      # adds Vite on :5173 with HMR, backend gets --dev CORS
run.cmd -Build    # rebuild the SPA into backend/voxarium_studio/static first
run.cmd -Stop
```

**Never start a second backend on another port.** The human tests on `:8765`; a private
instance on `:8799` means the two of you are looking at different processes and neither
notices. Any preview launcher must attach to the running instance rather than starting another server. If nothing is listening, run `run.cmd` — do not improvise a launch.

**Call `run.cmd` with `run_in_background: true`.** A foreground call never comes back:
`run.ps1` itself exits within seconds — it health-checks the backend, prints where it is,
and leaves it detached — but the harness then waits on that detached backend, so the call
burns its whole timeout and only "completes" when someone stops the server. Redirecting
its output to a file does not help; both were measured on 2026-08-04, against
`run.cmd -Stop` (which leaves nothing running) returning in 2.5 s. Backgrounded, the same
call finishes on its own with its banner in the task output.

Then wait on the port rather than on the task — one call, no sleep loop:

```bash
curl -s --retry 60 --retry-delay 1 --retry-connrefused -m 90 http://127.0.0.1:8765/healthz
```

What must never be a background task is `uvicorn` or Vite launched **directly**: those do
not exit, so the task really does hang forever. `run.ps1` is the entry point because it
detaches them for you and returns.

`run.ps1` has a second caller now: the Studio's own update button invokes it to come back
up (§12.1, `backend/voxarium_studio/updates/`). Two things there are a contract rather than a
detail — `Stop-Recorded` kills the recorded backend's whole **process tree**, which is why
the relauncher orphans itself before touching anything, and `-Project` / `-Port` / `-Dev` /
`-NoOpen` are the parameters the restart passes. Renaming or removing one of those, or
making the kill reach further, silently breaks the restart.

What a reload actually picks up, once one instance is running:

| Changed | Visible after |
|---|---|
| plugin frontend (`plugins_builtin/*/frontend/*.js`) | browser reload — served from disk |
| backend Python | `run.cmd` (or `-Dev`, which runs uvicorn `--reload`) |
| the SPA (`frontend/src`) | `run.cmd -Build`, or `run.cmd -Dev` for HMR |

That middle column is the whole reason to keep one instance: a plugin edit is live
immediately, an SPA edit is not, and finding that out on a second server wastes the
session.

## Every UI string is in two languages

The interface is localised (§6.4): English is the source, Russian the first translation, and the
reader picks one in Settings. A string the app shows is never written into a component — it is a
key in `frontend/src/locales/en.json` **and** `ru.json`, read with `t()` from `@/i18n` (or
`I18nT` when markup sits inside the sentence). Adding UI means writing the Russian at the same
time; `npm test` fails when the two files disagree on keys, placeholders or plural forms. A
built-in plugin's page keeps its messages in its own module through `runtime.i18n.translator`.
What arrives as data — manifests, backend errors, transcripts, document content — is not
translated. Keep both locale files in sync.

## Verifying changes

`.scratch/` is gitignored scratch space. `.scratch/demo-project` is a fully initialised
managed project whose database already holds completed pipeline runs and team tasks, so
the UI renders populated without spending agent credits. Verify against it rather than
creating a fresh project. Put throwaway scripts and probes there too, never in
`backend/` or `frontend/`.

**Name it: `run.cmd -Project .scratch\demo-project`.** With no `-Project`, the backend now
reopens whatever project was open last (§5.1), which may be a real game project.
Use the demo explicitly for verification.

## Scope discipline

The spec deliberately defers several things to post-v1: plugin sandboxing, RBAC and
multi-user access, and Docker packaging. These are accepted trade-offs recorded in §9.4
and §14, not oversights — do not "fix" them without being asked.
Consult the design spec before changing these intentional trade-offs.

3D preview in the Explorer was on that list and is now shipped, but only as plugins
(`vox_preview`, `fbx_preview`, `mesh_preview`) hanging off the previewer extension point in
§6.2. The Studio core still has no 3D dependency, and that is the constraint to preserve —
which is also why the `preview3d.view` pipeline node ships no viewer of its own: it asks the
host for whichever previewer claimed the file's suffix and mounts that
(`runtime.filePreviewer`). Adding a format is adding a previewer plugin, never a branch.

Version control goes through the independent `VcsBackend` entities in
`backend/voxarium_studio/vcs/` (§9.6), not through plugins. Git, Plastic SCM and Perforce
are always listed on Version Control; all their settings live on their own cards and
in `vcs.settings[<id>]`. Adding a system means extending the VCS catalog, as described
in `docs/vcs.md`. Team task isolation (§10.5) asks the detected backend rather than
assuming worktrees.
