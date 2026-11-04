/**
 * Custom canvas node for `preview3d.view` — the Explorer's preview pane, inside a node.
 *
 * This module renders no 3D itself. `filePreviewer()` hands back the `/api/explorer/file`
 * payload for a path together with the component a plugin registered for that file type
 * (§6.2), and the node mounts that component — the very one the Explorer would mount. So
 * `.vox` arrives through `vox_preview`, `.fbx` through `fbx_preview`, and any format a
 * later plugin claims works here the day it is installed, with nothing to change in this
 * file and no 3D dependency anywhere in the Studio core.
 *
 * Same rules as any plugin module: no imports at all. `vue` *and* `vueFlow` come through
 * the factory — handles registered from a bundled copy of Vue Flow would talk to a
 * different store and never connect.
 *
 * The chrome is the host's `.vxs-node` family rather than styles of its own, so a custom
 * node stays indistinguishable from a generic one: border for selection, top strip for run
 * status, header tint for the category. Only the viewport and its caption are styled here.
 */

/**
 * What the node says, in the Studio's interface languages (§6.4). The label, port ids, file
 * names and whatever the backend says about a file are data and are shown as they arrive.
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
    connect: 'Connect a file, or type a path in the config panel.',
    loading: 'Loading preview…',
    noViewerFor: 'No viewer is installed for {suffix} — install a plugin that previews it.',
    noViewer: 'No viewer is installed for this file type.',
    noFile: 'no file',
    units: {
      bytes: '{value} B',
      kilobytes: '{value} KB',
      megabytes: '{value} MB',
      gigabytes: '{value} GB',
    },
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
    connect: 'Подключите файл или укажите путь в панели настроек.',
    loading: 'Загрузка предпросмотра…',
    noViewerFor:
      'Для {suffix} не установлен модуль предпросмотра — установите плагин, который умеет его показывать.',
    noViewer: 'Для файлов этого типа не установлен модуль предпросмотра.',
    noFile: 'нет файла',
    units: {
      bytes: '{value} Б',
      kilobytes: '{value} КБ',
      megabytes: '{value} МБ',
      gigabytes: '{value} ГБ',
    },
  },
}

export default function ({ vue, vueFlow, filePreviewer, i18n }) {
  const { computed, defineComponent, h, ref, shallowRef, watch } = vue
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

  /** Message keys under `units`, smallest first. */
  const UNITS = ['bytes', 'kilobytes', 'megabytes', 'gigabytes']

  /**
   * Bytes as the Explorer's header writes them — the node should agree with the pane, and
   * that includes the units and the decimal separator of the language on screen.
   */
  function formatBytes(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return ''
    let value = bytes
    let unit = 0
    while (value >= 1024 && unit < UNITS.length - 1) {
      value /= 1024
      unit += 1
    }
    const digits = value < 10 ? 1 : 0
    const amount =
      unit === 0
        ? value
        : value.toLocaleString(i18n.locale.value, {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
            useGrouping: false,
          })
    return t(`units.${UNITS[unit]}`, { value: amount })
  }

  return defineComponent({
    name: 'Preview3DNode',
    props: {
      id: { type: String, required: true },
      data: { type: Object, required: true },
      selected: { type: Boolean, default: false },
    },
    setup(props) {
      const status = computed(() => props.data.status ?? null)

      // Node statuses and the `--vxs-status-*` tokens share their names, so the plugin can
      // resolve a status colour without the host's lookup table (§7).
      const statusTone = computed(() =>
        status.value ? `var(--vxs-status-${status.value}, var(--vxs-text-faint))` : null,
      )
      const statusText = computed(() => (status.value ? statusLabel(status.value) : ''))

      /**
       * Which file the node is showing.
       *
       * What the last run emitted wins, because that is the artifact the pipeline just
       * produced; a path typed into the config panel is the fallback, and it is what makes
       * this node useful before anything has run at all. Both are painted onto `data` by
       * the canvas while a run streams — exactly as `status` is — so they are read through
       * a computed and never captured in `setup` (see `types/voxarium_studio-plugin.d.ts`).
       */
      const path = computed(() => {
        const produced = props.data.outputs?.file
        const configured = props.data.params?.file
        return String(produced ?? configured ?? '').trim()
      })

      const name = computed(() => path.value.split(/[\\/]/).pop() ?? '')
      const suffix = computed(() => {
        const dot = name.value.lastIndexOf('.')
        return dot > 0 ? name.value.slice(dot).toLowerCase() : ''
      })

      const preview = shallowRef(null)
      // `shallowRef`, not `ref`: this holds a *component definition*, and making one deeply
      // reactive is both pointless and expensive.
      const viewer = shallowRef(null)
      const loading = ref(false)
      const failure = ref('')

      /**
       * Load the previewer for a path, discarding a stale answer.
       *
       * Two lookups are in flight whenever a run rewrites the node's output while the
       * previous file is still resolving, and network order is not request order. The
       * ticket is what stops the older answer from painting over the newer one.
       */
      let ticket = 0
      async function load(target) {
        const mine = ++ticket
        preview.value = null
        viewer.value = null
        failure.value = ''
        if (!target) {
          loading.value = false
          return
        }
        loading.value = true
        try {
          const resolved = await filePreviewer(target)
          if (mine !== ticket) return
          preview.value = resolved.preview
          viewer.value = resolved.component
        } catch (cause) {
          if (mine !== ticket) return
          failure.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          if (mine === ticket) loading.value = false
        }
      }

      watch(path, load, { immediate: true })

      const note = (text, tone) =>
        h(
          'div',
          {
            style: {
              position: 'absolute',
              inset: '0',
              display: 'grid',
              placeItems: 'center',
              padding: '10px',
              textAlign: 'center',
              fontSize: '11px',
              lineHeight: '1.4',
              color: tone ?? 'var(--vxs-text-faint)',
            },
          },
          text,
        )

      /**
       * What sits in the viewport. Every outcome says something: a blank rectangle leaves
       * the user guessing whether the node is broken, still loading, or looking at a format
       * nothing on this machine can draw.
       */
      const body = () => {
        if (!path.value) return note(t('connect'))
        if (loading.value) return note(t('loading'))
        if (failure.value) return note(failure.value, 'var(--vxs-danger)')
        if (!viewer.value) {
          return note(
            suffix.value ? t('noViewerFor', { suffix: suffix.value }) : t('noViewer'),
          )
        }
        return h(viewer.value, {
          // Keyed on mtime as well as path, for the reason `FilePreview.vue` gives: a
          // previewer reads the file once when it mounts, so a node regenerating the
          // artifact underneath it must force a remount or the old mesh stays on screen.
          key: `${preview.value?.path ?? path.value}@${preview.value?.modified ?? ''}`,
          // The payload's own path, not the port's: the backend answers with the
          // project-relative form, which is what `rawFileBytes()` expects.
          path: preview.value?.path ?? path.value,
          preview: preview.value,
        })
      }

      /**
       * One full-bleed port row. Vue Flow positions a handle absolutely, so a row with
       * `position: relative` becomes its containing block and the handle centres itself on
       * that row with no pixel offsets to keep in sync with the layout above it.
       */
      const portRow = (type, position, portId, label) =>
        h(
          'div',
          {
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
            // Node accent and status colour are handed down by the canvas; reading them as
            // custom properties keeps the plugin out of the token vocabulary (§7).
            style: {
              '--node-accent': props.data.accent ?? 'var(--vxs-accent)',
              '--node-status': statusTone.value,
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
              h('span', { style: { fontWeight: 500 } }, props.data.label ?? 'Preview 3D'),
              status.value
                ? h(
                    'span',
                    {
                      class: 'vxs-node-chip',
                      // The chip is the only place a failure can surface on this node: a
                      // custom component has no schema-driven body to print `data.error`
                      // into, so it spends the tooltip on it exactly as `GenericNode`
                      // does. Without it a refused path is a red "Error" and no reason.
                      title: props.data.error ?? undefined,
                    },
                    statusText.value,
                  )
                : null,
            ]),

            h('div', { style: { padding: '8px 10px' } }, [
              h(
                'div',
                {
                  /*
                   * The load-bearing line of this component. Vue Flow walks up from the
                   * event target looking for these classes: `nodrag` stops a drag inside the
                   * viewport from dragging the node across the canvas, `nowheel` stops a
                   * scroll-to-zoom from zooming the canvas instead of the model, and `nopan`
                   * stops a drag on empty space from panning it. Without all three, the
                   * viewer never receives the gestures it exists for.
                   */
                  class: 'nodrag nowheel nopan',
                  style: {
                    position: 'relative',
                    width: '280px',
                    height: '220px',
                    overflow: 'hidden',
                    borderRadius: 'var(--vxs-radius-md)',
                    border: '1px solid var(--vxs-border)',
                    background: 'var(--vxs-bg-inset)',
                  },
                },
                [body()],
              ),

              // What is on screen, in words. A viewport alone does not say which of three
              // candidate meshes the node ended up with.
              h(
                'div',
                {
                  style: {
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: '6px',
                    margin: '6px 0 0',
                    fontSize: '10px',
                    fontFamily: 'var(--vxs-font-mono)',
                    color: 'var(--vxs-text-faint)',
                  },
                },
                [
                  h(
                    'span',
                    {
                      style: {
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      },
                    },
                    name.value || t('noFile'),
                  ),
                  preview.value && typeof preview.value.size === 'number'
                    ? h('span', { style: { flex: 'none' } }, formatBytes(preview.value.size))
                    : null,
                ],
              ),
            ]),

            h('div', { style: { paddingBottom: '6px' } }, [
              portRow('target', Position.Left, 'file', 'file'),
              portRow('source', Position.Right, 'file', 'file'),
            ]),
          ],
        )
    },
  })
}
