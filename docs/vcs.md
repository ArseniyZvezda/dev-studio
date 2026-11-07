# Version control entities

Version control is an independent Studio entity type, defined by dev spec §9.6.
Implementations live in `backend/voxarium_studio/vcs/`; they do not participate in plugin
discovery or activation. The catalog always contains Git, Plastic SCM and Perforce,
including systems whose CLI is missing on the current machine.

## Project configuration

Every system has one card on **Version Control**, containing all its settings. Values
live in the managed project's `.voxarium_studio/config.yaml`:

```yaml
vcs:
  backend: auto
  settings:
    git:
      executable: C:/Program Files/Git/cmd/git.exe
    plastic:
      executable: C:/Program Files/PlasticSCM5/client/cm.exe
      workspace: D:/Workspaces/MyGame
    perforce:
      executable: C:/Program Files/Perforce/p4.exe
      port: ssl:perforce.example.com:1666
      user: artist
      client: artist-game
      charset: utf8
```

These are example overrides, not required values. An empty executable uses the tool's
name on PATH: `git`, `cm` or `p4`. Other empty fields leave the backend's native defaults
in place. Perforce uses native authentication, including existing `p4 login` tickets;
the Studio does not store a Perforce password.

`vcs.backend` selects the active system. An explicit ID (`git`, `plastic`, `perforce`)
wins over detection. Unset or `auto` checks Plastic, then Perforce, then Git; `none`
turns the project's integration off. All cards remain available regardless of this
choice. Selecting a system does not install its CLI or create a server workspace.

Loading an older project migrates `plugins.plastic.settings` into
`vcs.settings.plastic`. Existing VCS settings win on matching keys. The legacy plugin
entry is removed, so clearing a value cannot bring it back on the next load. The old
plugin's `enabled` flag no longer controls Plastic availability.

## Adding a system

1. Add an implementation module under `backend/voxarium_studio/vcs/` and subclass
   `VcsBackend` from `.base`.
2. Declare `id`, `name`, `program` and `settings_fields`. Add its class to
   `BUILTIN_BACKENDS` in `vcs/__init__.py`, placing it deliberately in detection order.
3. Implement detection, status, branch, diff, add and commit. Implement revision and
   patch support when the tool can provide them. Set `supports_isolation = True` only
   when `create_workspace` and `remove_workspace` provide a practical task checkout.
4. Add English and Russian labels and hints for the new settings in the frontend's
   locale files. The Version Control card renders the returned field metadata.
5. Test output parsing, unavailable CLI, failed commands, empty results, workspace
   paths and scoped commits. Preserve unrelated pending files when a caller names a
   subset of paths.

`VcsBackend.settings_fields` is a tuple of field dictionaries with `key`, `type`
(`string` or `path`) and optional `default`. The `setting_fields()` class method adds
the common executable field. `GET /api/vcs` returns these as `settings_fields` together
with configured `settings`; neither requires a plugin ID. `PUT /api/vcs/settings`
validates the complete request before applying it, merges individual keys and removes
overrides when their value is empty.

The constructor receives the managed project root; `self.settings` contains that
system's VCS settings. If an explicit workspace setting changes `self.root`, keep
caller-supplied paths relative to the original managed project when building CLI targets.
`self.run(...)` runs the CLI without a shell, handles timeouts
and cancellation, and returns `ToolResult(code, stdout, stderr)`. Set `check=True` to
turn a failed command into `VcsError`; use `input=` for commands that accept a form on
stdin. Tool output and errors are data rather than translatable UI messages.

`detect()` must not modify the project. Return `False` when a workspace cannot be
verified. A missing or unavailable tool must not prevent other catalog entries from
loading. Read operations, including status previews, must not open files for edit or
change pending submissions.

`VcsStatus.root` names the actual workspace root. A `FileChange.path` is relative to
that root, or absolute when the native tool returns absolute paths. Use the shared
change-kind vocabulary, and preserve rename origins. The API rebases workspace paths
to the managed project and omits files outside it. This matters when the managed
project is only one directory of a larger checkout.

`commit()` returns `CommitResult(committed=False)` when there is nothing to record;
only a rejected operation raises `VcsError`. A task's `paths` restrict the commit to
those files. A centralized backend must not submit other files merely because they
share a pending changelist. Git uses worktrees for isolation; Plastic and Perforce
currently declare no isolation, so Teams coordinate shared files with explicit write
locks (§10.5).

Perforce creates a dedicated numbered changelist for each submit and leaves it pending
if submission fails, allowing recovery through `p4` or P4V without reverting local
files. File patches compare the synced base revision with local bytes. Authentication,
server trust and workspace creation remain native client operations. Paths containing
a literal `...` are rejected because Perforce interprets that sequence as a recursive
filespec wildcard.

## Plastic pipeline compatibility

`vcs/nodes.py` registers `plastic.status`, `plastic.add` and `plastic.checkin` as
built-in VCS nodes. Their IDs and ports remain compatible with existing pipelines.
They read `vcs.settings.plastic` and remain available when plugins are reloaded or
disabled. Their internal owner ID is not a plugin and has no Plugins page.

All three nodes are non-cacheable. A cached status could describe an old workspace;
a cached add or checkin could report a write that never happened. Plastic checkin uses
`--all` to include files changed directly by an asset-generating pipeline, even if
those files were not explicitly checked out first.
