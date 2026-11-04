/**
 * Custom canvas node for both Hunyuan3D generation nodes — the worked example of the
 * custom-node half of the §9.3 frontend contract.
 *
 * The generic renderer already draws ports and form fields from the schema, so a custom
 * node is only worth it when the node has something of its own to show. Here that is which
 * picture (or which views) will be generated from and what will come back, both visible
 * without opening the config panel — these nodes have thirteen inputs and the ones that
 * decide the result are the images.
 *
 * **One component, two node types.** `hunyuan3d.image_to_mesh` has a single `image` port
 * and `hunyuan3d.multiview_to_mesh` has `front`/`left`/`back`/`right`; everything else
 * about them is identical, so the component reads its schema rather than knowing which one
 * it is. That is not only tidiness — a plugin module is loaded with a `?token=` appended,
 * which a relative `import './shared.js'` would drop and 401 on, so two components would
 * mean two copies free to drift apart on the canvas.
 *
 * Same rules as a plugin page: no imports at all. `vue` *and* `vueFlow` come through the
 * factory — handles registered from a bundled copy of Vue Flow would talk to a different
 * store and never connect.
 *
 * The chrome comes from the host's `.vxs-node` classes rather than from inline styles of
 * its own, which is what keeps a custom node indistinguishable from a generic one: border
 * for selection, top strip for run status, header tint for the category, and the progress
 * bar and stage line while it runs. Painting any of that here again is how the two
 * renderers drifted apart the first time. Only what is genuinely this node's own — the
 * width cap, the image line, the summary — is styled inline.
 */

/**
 * What the node says, in the Studio's interface languages (§6.4). Only the node's own words
 * are here: the label, port ids, file names and a progress frame's stage are data and are
 * shown as they arrive.
 */
const MESSAGES = {
  en: {
    status: {
      pending: 'Pending',
      running: 'Running',
      paused: 'Paused',
      success: 'Success',
      error: 'Error',
      cancelled: 'Cancelled',
      skipped: 'Skipped',
      cached: 'Cached',
    },
    subject: {
      views: { one: '{name} +{count} view', other: '{name} +{count} views' },
      fromNodes: 'views come from the connected nodes',
      fromNode: 'image comes from the connected node',
    },
    summary: {
      seed: 'seed {seed}',
      steps: { one: '{count} step', other: '{count} steps' },
      octree: 'octree {resolution}',
      texture: 'texture {resolution}',
      views: { one: '{count} view', other: '{count} views' },
      autoViews: 'auto views',
    },
    eta: '~{seconds}s',
    mesh: 'mesh · {format}',
  },
  ru: {
    status: {
      pending: 'Ожидает',
      running: 'Выполняется',
      paused: 'На паузе',
      success: 'Успешно',
      error: 'Ошибка',
      cancelled: 'Отменено',
      skipped: 'Пропущено',
      cached: 'Из кэша',
    },
    subject: {
      views: {
        one: '{name} +{count} ракурс',
        few: '{name} +{count} ракурса',
        many: '{name} +{count} ракурсов',
        other: '{name} +{count} ракурса',
      },
      fromNodes: 'ракурсы приходят от подключённых узлов',
      fromNode: 'изображение приходит от подключённого узла',
    },
    summary: {
      seed: 'сид {seed}',
      steps: { one: '{count} шаг', few: '{count} шага', many: '{count} шагов', other: '{count} шага' },
      octree: 'октодерево {resolution}',
      texture: 'текстура {resolution}',
      views: {
        one: '{count} ракурс',
        few: '{count} ракурса',
        many: '{count} ракурсов',
        other: '{count} ракурса',
      },
      autoViews: 'ракурсы: авто',
    },
    eta: '~{seconds} с',
    mesh: 'меш · {format}',
  },
}

const IMAGE_PORTS = ['image', 'front', 'left', 'back', 'right']
/** The input ports that are this node's subject, in the order the model reads them. */

export default function ({ vue, vueFlow, i18n }) {
  const { defineComponent, h, computed } = vue
  const { Handle, Position } = vueFlow
  const t = i18n.translator(MESSAGES)

  /**
   * A run status as the Studio's own nodes word it. One this module does not know — a newer
   * backend's — is still shown, capitalised, rather than as a key.
   */
  const statusLabel = (value) =>
    Object.hasOwn(MESSAGES.en.status, value)
      ? t(`status.${value}`)
      : value.charAt(0).toUpperCase() + value.slice(1)

  return defineComponent({
    name: 'GenerateMeshNode',
    props: {
      id: { type: String, required: true },
      data: { type: Object, required: true },
      selected: { type: Boolean, default: false },
    },
    setup(props) {
      const schema = computed(() => props.data.schema ?? null)
      const params = computed(() => props.data.params ?? {})
      const status = computed(() => props.data.status ?? null)
      // Painted on by the canvas while the node runs, exactly as `status` is — so it has
      // to be read through a computed and never captured here in `setup`.
      const progress = computed(() =>
        status.value === 'running' ? (props.data.progress ?? null) : null,
      )

      // Node statuses and the `--vxs-status-*` tokens share their names, so the plugin can
      // resolve a status colour without the host's lookup table (§7).
      const statusTone = computed(() =>
        status.value ? `var(--vxs-status-${status.value}, var(--vxs-text-faint))` : null,
      )
      const statusText = computed(() => (status.value ? statusLabel(status.value) : ''))

      const inputs = computed(() => schema.value?.inputs ?? [])
      const port = (list, id) => (list ?? []).find((entry) => entry.id === id)

      /**
       * What this node will actually run with: its own param, or the port default.
       *
       * `data.params` holds only what the node overrides, while the engine fills the
       * rest in from the schema — and those defaults are project settings (§9.4), so a
       * hardcoded fallback here would let the node claim 30 steps on a project whose
       * default is 5. Reading `data.schema` keeps the two in step.
       */
      const configured = (id) => {
        const own = params.value[id]
        if (own !== undefined && own !== null && own !== '') return own
        return port(inputs.value, id)?.default
      }

      const seed = computed(() => configured('seed') ?? 0)
      const format = computed(() => {
        const chosen = String(configured('output_format') ?? 'stl')
        // What lands on disk, not what was asked for: the server bundles a textured OBJ
        // with its .mtl and PBR maps, and the node saves that under a .zip.
        return chosen === 'obj' && configured('texture') ? 'obj zip' : chosen
      })

      /** The image ports this node type actually has, in `IMAGE_PORTS` order. */
      const imagePorts = computed(() =>
        IMAGE_PORTS.map((id) => port(inputs.value, id)).filter(Boolean),
      )
      const filename = (id) => {
        const raw = String(params.value[id] ?? '').trim()
        return raw ? raw.split(/[\\/]/).pop() : ''
      }
      /** The picture this run is named after: the single image, or the front view. */
      const subject = computed(() => {
        const named = imagePorts.value.find((entry) => filename(entry.id))
        return named ? filename(named.id) : ''
      })
      /** How many views are set, counted only where there is more than one to count. */
      const views = computed(() =>
        imagePorts.value.length > 1
          ? imagePorts.value.filter(
              (entry) => filename(entry.id) || (props.data.connected ?? []).includes(entry.id),
            ).length
          : 0,
      )

      // Everything the summary line says, so the node reads as one sentence about what
      // this run will actually do rather than a dump of thirteen params.
      const summary = computed(() => {
        const parts = [t('summary.seed', { seed: seed.value })]
        parts.push(t('summary.steps', { count: Number(configured('steps') ?? 30) }))
        parts.push(t('summary.octree', { resolution: configured('octree_resolution') ?? 256 }))
        if (configured('texture')) {
          // The bake is the slow half and these two are what decide how slow, so they
          // belong on the node rather than three clicks into the config panel.
          const bake = Number(configured('max_num_view') ?? 0)
          parts.push(t('summary.texture', { resolution: configured('texture_resolution') ?? 512 }))
          parts.push(bake ? t('summary.views', { count: bake }) : t('summary.autoViews'))
        }
        parts.push(format.value)
        return parts.join(' · ')
      })

      /**
       * The progress block, drawn out of the host's own classes.
       *
       * Same reasoning as the status strip: `.vxs-node-progress` is in `base.css`, so a
       * bar here and a bar on a generic node are the same bar and cannot drift. What the
       * plugin owns is only the wording — a null `percent` means the stage is known and
       * the position in it is not, and the `-unknown` class is what turns the fill into
       * the indeterminate sweep.
       */
      const progressBlock = () => {
        const frame = progress.value
        if (!frame) return null
        const stage = [frame.label, frame.detail].filter(Boolean).join(' · ')
        const amount = [
          frame.percent === null || frame.percent === undefined
            ? ''
            : `${Math.round(frame.percent)}%`,
          frame.eta_s ? t('eta', { seconds: Math.round(frame.eta_s) }) : '',
        ]
          .filter(Boolean)
          .join(' · ')
        const known = frame.percent !== null && frame.percent !== undefined
        return h(
          'div',
          {
            class: ['vxs-node-progress', known ? null : 'vxs-node-progress-unknown'],
            title: stage || undefined,
          },
          [
            h('div', { class: 'vxs-node-progress-track' }, [
              h('div', {
                class: 'vxs-node-progress-fill',
                style: known ? { width: `${frame.percent}%` } : undefined,
              }),
            ]),
            stage || amount
              ? h('p', { class: 'vxs-node-progress-text' }, [
                  h('span', {}, stage),
                  h('span', {}, amount),
                ])
              : null,
          ],
        )
      }

      /**
       * The input rows that get a handle: the images, plus anything this node pins or has
       * an edge into.
       *
       * `connectable` is only the schema's default — `data.pins` is the node instance's
       * own answer and the config panel edits it, so a fixed row per port would be a node
       * whose pins that panel cannot change. `connected` is the other half: Vue Flow
       * anchors an edge to a mounted handle or to nothing at all, so a hand-edited
       * pipeline must still get a handle wherever it drew an edge.
       */
      const inputRows = computed(() => {
        const connected = new Set(props.data.connected ?? [])
        const pins = props.data.pins ?? {}
        return inputs.value.filter((entry) => {
          if (connected.has(entry.id)) return true
          return pins[entry.id] === undefined ? entry.connectable : pins[entry.id]
        })
      })

      // The fallback matters only before the schema arrives: an edge whose handle has not
      // mounted is an edge Vue Flow drops, and `mesh_file` is on both node types.
      const outputRows = computed(() => schema.value?.outputs ?? [{ id: 'mesh_file' }])

      /**
       * One full-bleed port row. Vue Flow positions a handle absolutely, so a row with
       * `position: relative` becomes its containing block and the handle centres itself
       * on that row with no pixel offsets to keep in sync with the layout above it.
       */
      const portRow = (type, position, portId, label) =>
        h(
          'div',
          {
            key: `${type}:${portId}`,
            style: {
              position: 'relative',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              minHeight: '20px',
              padding: '1px 10px',
              justifyContent: position === Position.Right ? 'flex-end' : 'flex-start',
            },
          },
          [
            h(Handle, {
              type,
              position,
              id: portId,
              style: { background: 'var(--node-accent)' },
            }),
            h('span', { style: { fontSize: '11px', color: 'var(--vxs-text-secondary)' } }, label),
          ],
        )

      return () =>
        h(
          'div',
          {
            class: [
              'vxs-node',
              props.selected ? 'vxs-node-selected' : null,
              status.value === 'running' ? 'vxs-node-running' : null,
              props.data.disabled ? 'vxs-node-disabled' : null,
            ],
            // Node accent and status colour are handed down by the canvas; reading them
            // as custom properties keeps the plugin out of the token vocabulary (§7).
            style: {
              '--node-accent': props.data.accent ?? 'var(--vxs-accent)',
              '--node-status': statusTone.value,
              minWidth: '210px',
              // Capped so the summary wraps instead of growing the node into its
              // neighbour — the reference pipeline spaces these 320px apart.
              maxWidth: '260px',
              fontSize: 'var(--vxs-text-size)',
            },
          },
          [
            status.value ? h('div', { class: 'vxs-node-status' }) : null,

            h('div', { class: 'vxs-node-header' }, [
              h('span', {
                style: {
                  width: '7px',
                  height: '7px',
                  borderRadius: '999px',
                  flex: 'none',
                  background: 'var(--node-accent)',
                },
              }),
              h(
                'span',
                { style: { fontWeight: 500 } },
                props.data.label ?? schema.value?.name ?? 'Image → Mesh',
              ),
              status.value ? h('span', { class: 'vxs-node-chip' }, statusText.value) : null,
            ]),

            h('div', { style: { padding: '8px 10px' } }, [
              h(
                'p',
                {
                  style: {
                    margin: 0,
                    color: subject.value ? 'var(--vxs-text-secondary)' : 'var(--vxs-text-faint)',
                    fontStyle: subject.value ? 'normal' : 'italic',
                    fontFamily: subject.value ? 'var(--vxs-font-mono)' : 'inherit',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  },
                },
                subject.value
                  ? views.value > 1
                    ? t('subject.views', { name: subject.value, count: views.value - 1 })
                    : subject.value
                  : imagePorts.value.length > 1
                    ? t('subject.fromNodes')
                    : t('subject.fromNode'),
              ),
              h(
                'p',
                {
                  style: {
                    margin: '8px 0 0',
                    fontSize: '11px',
                    color: 'var(--vxs-text-faint)',
                    fontFamily: 'var(--vxs-font-mono)',
                  },
                },
                summary.value,
              ),
            ]),

            progressBlock(),

            h('div', { style: { paddingBottom: '6px' } }, [
              ...inputRows.value.map((entry) =>
                portRow('target', Position.Left, entry.id, entry.id),
              ),
              // Every output, unlike the inputs: an output is not pinnable, so the row is
              // the only handle an edge out of `triangle_count` or `views_used` can find.
              // Only the mesh is worth spelling out — the rest are their own names.
              ...outputRows.value.map((entry) =>
                portRow(
                  'source',
                  Position.Right,
                  entry.id,
                  entry.id === 'mesh_file' ? t('mesh', { format: format.value }) : entry.id,
                ),
              ),
            ]),
          ],
        )
    },
  })
}
