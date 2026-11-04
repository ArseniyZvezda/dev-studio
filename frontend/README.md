# Voxarium Studio — frontend

Vue 3 + TypeScript SPA for the Studio: Explorer, the AI Pipelines canvas, Teams and
plugin-contributed pages.

## Development

Use the repository launcher to start the backend and Vite with HMR (spec §2.5):

```bash
# From the repository root on Windows:
run.cmd -Dev -Project .scratch\demo-project
```

The dev server proxies `/api` and `/ws` to `http://127.0.0.1:8765`, so dev and production
use identical URLs. The launcher owns the fixed ports; use the running instance for
verification instead of starting another backend.

If the backend was started with `--token`, open the URL the CLI printed — the token is
read from `?token=`, stored, and stripped from the address bar.

## Production

```bash
npm run build        # type-checks, then writes ../backend/voxarium_studio/static
```

That output directory is what `voxarium_studio.main.static_dir()` looks for first and what the
wheel ships, so a plain `voxarium_studio run <project>` then serves the UI and the API from one
process on one port.

## Layout

| Path | What lives there |
|---|---|
| `src/api/` | Typed client, hand-written schema mirrors, token handling |
| `src/ws/` | The single `/ws` connection, event taxonomy, client-side demuxing |
| `src/stores/` | Pinia stores, one per feature area |
| `src/styles/` | Design tokens and the Tailwind v4 `@theme` bridge (spec §7) |
| `src/node-renderer/` | Schema-driven node and config-panel rendering |
| `src/plugin-runtime/` | Runtime loading of plugin pages |

## Writing a plugin page

Plugin pages are separate ES modules loaded at runtime from
`/api/plugins/{id}/assets/…`. Export a factory that receives the host runtime rather
than importing `vue` directly — a second Vue instance would not share reactivity with
the app, and a bare `vue` specifier cannot resolve in the browser:

```js
export default function ({ vue, api, onEvent, settings, action }) {
  const { defineComponent, h, ref } = vue
  return defineComponent({
    setup() {
      const value = ref('')
      settings.get().then((stored) => { value.value = stored.endpoint ?? '' })
      return () => h('div', { class: 'p-pane' }, value.value)
    },
  })
}
```

Build it with Vite in library mode (`format: 'es'`) and no external imports. A module
that instead default-exports a ready-made component also works.

`action(id, payload?)` calls one of the plugin's own backend actions
(`registry.add_action()` in its `plugin.py`, served as
`POST /api/plugins/{id}/actions/{action}`) — the one way a page reaches its plugin's
Python; long work goes to the background there and comes back as `plugin.event` through
`onEvent`. See "Hooks, actions and search" in `docs/plugins.md`.

**One file, not several.** A dynamic `import()` cannot set an `Authorization` header, so
the host appends `?token=` to the module URL. A relative `import './helper.js'` from
inside your module resolves without that token and 401s in server mode — bundle to a
single file.

## Types for plugin authors

`types/voxarium_studio-plugin.d.ts` declares the runtime object and the props of all three module
kinds (`PluginRuntime`, `PluginNodeProps`, `PluginPreviewerProps`). Copy it into your plugin
project; `vue`, `vue-router` and `@vue-flow/core` are dev dependencies for types only and
must not be bundled. The Studio type-checks that file against the runtime it really passes
(`src/plugin-runtime/contract.ts`), so it cannot quietly drift from what a plugin receives.

`docs/plugins.md` is the full author guide — manifest, execution kinds, node components and
previewers. This section is only about where the types live.

## Writing a file previewer

A plugin can claim a file type in the Explorer preview pane (spec §6.2) — that is how
`.vox` and `.fbx` get a viewer without the Studio depending on a 3D library. Declare it
in `plugin.yaml`; no backend code is needed:

```yaml
frontend:
  previewers:
    - id: my-mesh
      label: Mesh
      extensions: ['.ply']
      component: frontend/PlyPreview.js
```

The module follows the same factory contract as a page and receives two props, `path`
and `preview` (the `/api/explorer/file` payload). Fetch the bytes through
`rawFileBytes`, which carries auth, rather than building a URL yourself:

```js
export default function ({ vue, rawFileBytes }) {
  const { defineComponent, h, onMounted, ref } = vue
  return defineComponent({
    props: { path: { type: String, required: true }, preview: { type: Object, default: null } },
    setup(props) {
      const summary = ref('')
      onMounted(async () => {
        summary.value = `${(await rawFileBytes(props.path)).byteLength} bytes`
      })
      return () => h('div', { class: 'p-pane' }, summary.value)
    },
  })
}
```

The component owns the whole pane, so give the root `height: 100%`. Highest `priority`
wins if two plugins claim the same extension. Anything not claimed falls back to the
built-in text/image/metadata panel.

## Mounting a previewer somewhere other than the Explorer

`runtime.filePreviewer(path)` answers with the `/api/explorer/file` payload and the
component whichever plugin claimed that suffix — the same one the Explorer mounts:

```js
const { preview, component } = await runtime.filePreviewer(path)
```

`component` is `null` when nothing claimed the type, and saying so ("no viewer is
installed for `.glb`") is better than an empty box. Mount it with `path` and `preview`,
inside a container that has a size, and key it on the file's `modified` as well as its
path: a previewer reads the file once, when it mounts, so an artifact regenerated
underneath it needs a remount.

`preview3d.view` is the worked example. It is a canvas node that shows the file on its
input port and knows no formats at all — which is what lets one node cover `.vox` and
`.fbx` today and every type a previewer is added for later. Inside a Vue Flow node the
viewport wrapper needs `class="nodrag nowheel nopan"`, or dragging to orbit drags the
node and scrolling to zoom the model zooms the canvas.
