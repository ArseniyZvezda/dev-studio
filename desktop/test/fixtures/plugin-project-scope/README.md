# VS-43 / VS-45: native plugin project-scope verification

This is a reproducible **Electron/Chromium fixture integration harness**, not a live
backend end-to-end test, not the Studio application shell, and not evidence that
desktop tabs/transfer/restore ship. VS-27 remains on review. No production fixes are
included in VS-43; VS-44 supplied the lifecycle correction, independently accepted
in VS-45 without further production changes.

## Run on Windows

Prerequisites: Node compatible with `frontend/package.json`, its dependencies
installed (`cd frontend; npm ci`), and an installed Windows Voxarium Studio runtime
(tested with Electron 45.1.0 / Chromium 156.0.8078.49). By default the runtime is read
from `%LOCALAPPDATA%\Programs\Voxarium Studio`; set `VS43_INSTALLED_DIR` to another
installed runtime directory if needed. No dependency download or backend launch is
performed by the harness.

From the repository root:

```powershell
# VS-45: task-private build/profile/log output; assertions and fixture unchanged.
$env:VS43_TEMP_DIR = 'D:\VoxariumStudio\.voxarium_studio\temp\TASK-45'
# Strict acceptance: 45/45, exit 0.
node desktop/test/plugin-project-scope.cjs --run-native --record

# Diagnostic control only, NOT a fix or replacement for the strict regression.
node desktop/test/plugin-project-scope.cjs --run-native --precompute-urls --record
```

Omitting `--run-native` prints usage without starting Electron. Each run prints its
evidence directory under `VS43_TEMP_DIR` (absolute task-private directory); the
default remains `D:\VoxariumStudio\.voxarium_studio\temp\TASK-43`. VS-45 added
only this output-directory override to the harness; no assertions, fixture behaviour
or production modules were changed.
`--record` additionally replaces the checked-in `evidence.json` (strict) or
`evidence-precomputed.json` (control), including **failed** strict results. Ordinary
runs do not edit repository files. Do not use `--record` concurrently.

## What actually runs

- Vite builds the current `frontend/src` production `PluginLoader` (default native
  importer), `createRuntime`, node-param bindings, `WsConnection`, grammar registry
  and real WASM-backed Oniguruma/Shiki highlighter. There are no mock production
  module aliases and no substituted fetch/importer/factory. Vue and Vue Router
  aliases resolve the real frontend dependency copies, not test doubles.
- Two simultaneously live Chromium renderer processes share one isolated browser
  session/origin. Each mounts real Vue `RouterView`/async components, plus minimal
  node/Explorer consumer roots. These are **not** full `App.vue`, PipelineEditor or
  Explorer views; their broader integration remains covered by the frontend tests.
- The Electron `fixture://studio` protocol serves inventory/preview/settings/action
  responses and different A/B ES module source, CSS, SVG pixels and TextMate JSON.
  Both projects deliberately reuse plugin/page/node/previewer/language/scope IDs and
  `v=same-stamp`. JS factories execute only after Chromium's real `import()` fetch.
- Serialized event envelopes enter the actual `WsConnection.dispatch` parser and
  project-scoped loader subscriptions. There is **no native WebSocket handshake**,
  reconnect or server replay test; the transport is explicitly fixture injection.
  Tests never call loader `reset()` or grammar invalidation to fake lifecycle events.
- The isolated session blocks all non-fixture network requests. It does not access,
  switch, restart or launch any Studio backend. It copies the installed **Electron
  runtime only** to task temp and never loads Studio `main.cjs`/`app.asar`.

Assertions cover delayed A import after completed B; actual DOM/computed CSS and
canvas-decoded SVG/raw pixels; page/node UI clicks/nested previewers; URL scope and
token/header auth; equal-ID distinct grammar token DOM; native same-URL module cache
and equal-stamp reload with new `studio_generation`; unchanged B snapshots and live
callbacks; disable/re-enable; repeated gaps/coalescing/local refresh; failed refresh
and retry; late inventory/import rejection; VS-41 delayed initial registry and
page → pipeline → page/editor replacement. Stale callbacks must cause **no HTTP or
node writes**. Uncaught errors/rejections, unexpected warnings, console errors and
wrong-project requests/writes are checked, not silently suppressed.

This verifies generated client requests, **not** real backend authentication,
Host/Origin/CSP/path-boundary enforcement, Python plugin loading, native WS or
desktop lifecycle. It does not certify arbitrary third-party plugins or rewrite
their direct legacy URLs.

## Historical VS-43 defect: reactive stale-page render on withdrawal

Before VS-44, the VS-43 strict run completed 44 assertions and failed assertion 45
`No Chromium/production console errors or unexpected warnings`: four A-only
`Error: Plugin registration has been replaced` console errors. B is unchanged;
there are no cross-project writes, uncaught errors or unhandled rejections.
This was reproduced independently, not inferred solely from source inspection.

Exact reproduction inside the default command:

1. A's native plugin page calls `runtime.assetUrl('pixel.svg')` inside its Vue render
   function (`component.js`), alongside `runtime.rawFileUrl(...)`. This is a supported
   URL helper, not a hand-built legacy URL. B renders its own page concurrently.
2. Hold A's pages inventory response, change the fixture JS to A2 without changing
   `v=same-stamp`, and deliver A-scoped `plugin.changed` through `WsConnection`.
3. `PluginLoader.changed()` calls `withdraw()`, advancing the reactive registration
   generation and revoking the old runtime before REST/router replacement completes.
4. The previous render read that reactive generation via `PluginLoader.capture()` →
   `createRuntime.guard()` → `assetUrl()`. Withdrawal therefore queues another render
   of the still-mounted old page. The URL helper now throws; Vue logs a render error.
5. The same render error also occurs on the repeated resync gap, invalid-inventory
   refresh attempt, and disable while an older inventory response is held.

The original VS-43 stacks identified `component.js` render → runtime `assetUrl`/guard →
`PluginLoader.capture`, followed by Vue `renderComponentRoot/flushJobs`. These four
errors are **not** the intentionally caught late-import or stale-editor rejections.
The one expected stale-editor warning is listed separately in evidence.

`--precompute-urls` changes **only fixture code behaviour**: URLs are obtained in the
factory before render, so the render no longer reads runtime guards. All 45 assertions
then pass, with zero console errors/unexpected warnings. This isolates the trigger;
it does not repair production, justify excluding the strict fixture, or establish a
restriction that plugins may not call URL helpers in render. The original strict command stayed
red until VS-44 corrected production lifecycle. URL helpers remain supported in render.

## VS-45 independent acceptance after VS-44 (2026-11-13)

Strict `--run-native --record`, **without** `--precompute-urls`, passed **45/45**
with exit 0 at `2026-11-13T13:41:27.360Z`. The separate diagnostic control was
re-recorded at `2026-11-13T13:41:50.215Z` and also passed 45/45; it is not the
acceptance substitute. Both used Electron 45.1.0 / Chromium 156.0.8078.49 and the
current production source hashes listed in their JSON evidence.

Both records contain zero console errors, zero unexpected warnings, zero uncaught
exceptions/unhandled rejections and zero request violations. Each contains exactly
one expected `setNodeParams(same-node) ignored: pipeline editor is no longer mounted`
warning, retained visibly in evidence. A/B isolation, unchanged B during A lifecycle
changes, immediate stale HTTP/node-write rejection and late-import guards all pass.
VS-44's non-reactive capability capture plus host-owned registration render boundary
prevents the old plugin render from being scheduled on withdrawal; stale capabilities
remain immediately revoked. No production correction was made during VS-45.

The console assertion classifies messages before consumer cleanup, and renderer
diagnostics are captured before cleanup; it is not a teardown-error guarantee.
The final saved console arrays also contain only the single expected warning.
The warning allowlist is message-based; 45/45 and exactly one warning describe these
recorded runs, not a newly tightened assertion or broader plugin certification.

Both helpers exited (PIDs 43672 / 4324), with no surviving helper processes.
Backend PID 15152, creation identity and command were identical before/after both
runs. All 205 production static files retained aggregate SHA-256
`9baa2e0881071c1702d334c5b9339f49da6a0ea0b92184727e653d194381d7d7`.
Builds/profile/logs stayed in TASK-45; no server was launched, switched or restarted.
The evidence `task: VS-43` field names the original harness, not the acceptance task.
VS-27 remains on review for Producer; this accepts only the lifetime/Chromium stage.

## Evidence and safety

- [Strict recorded run](evidence.json): assertion results, request/body audit, source
  SHA-256s, DOM/style/pixel/token snapshots, console stages,
  Electron/Chromium versions and process/static evidence.
- [Precomputed-URL control](evidence-precomputed.json): separately re-recorded control.
- [A snapshot](observed-A.png), [B snapshot](observed-B.png): final DOM after A3 recovery;
  historical VS-43 screenshots, not regenerated by VS-45; screenshots alone do not
  prove the intermediate assertions. Current run screenshots are temporary.

Detailed per-run build log, sourcemaps, screenshots, Electron log and isolated profile
are temporary. Only the two recorded JSON files and screenshots above are durable.
The parent compares production static content hashes and the working :8765 backend's
PID/creation/command identity before/after; the helper is awaited and its renderer/
GPU/utility processes checked absent. A watchdog kills **only the launched helper
PID tree**, never a process by image name. No second server is created; Vite output
never targets `backend/voxarium_studio/static`.
