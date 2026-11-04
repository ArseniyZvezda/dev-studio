/**
 * Custom canvas node for `preview2d.view` — the Explorer's image pane, inside a node.
 *
 * Three sources, tried in that order, and the order is the design. A plugin that claimed the
 * suffix wins, because it knows something about the format this file does not (§6.2). Failing
 * that the `/api/explorer/file` payload's `data_url` is used, which is what the Explorer's own
 * pane paints. Failing *that* — the backend declines to inline an image over four times
 * `explorer.max_preview_bytes`, and a baked 2048² texture passes that easily — the bytes are
 * fetched through `rawFileBytes` and wrapped in a blob URL.
 *
 * That last step is not the obvious one, and `rawFileUrl` is the reason it exists: it builds a
 * URL with no `Authorization` header on it, so an `<img src>` pointed at it 401s the moment the
 * Studio is served with `--token`. `rawFileBytes` carries the header, which is what makes the
 * large-image path work in the configuration where it is least likely to be tested.
 *
 * Same rules as any plugin module: no imports at all. `vue` *and* `vueFlow` come through the
 * factory — handles registered from a bundled copy of Vue Flow would talk to a different store
 * and never connect.
 *
 * The chrome is the host's `.vxs-node` family rather than styles of its own, so a custom node
 * stays indistinguishable from a generic one: border for selection, top strip for run status,
 * accent for the category. Only the viewport and its caption are styled here.
 */

/**
 * What the node says, in the Studio's interface languages (§6.4). The label, port ids, file
 * names and whatever the backend or a previewer says about a file are data and are shown as
 * they arrive.
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
    smoothing: {
      on: 'Interpolation on — click to draw pixels sharp, for pixel art',
      off: 'Interpolation off — pixels are drawn sharp. Click to smooth the image',
    },
    connect: 'Connect an image, or type a path in the config panel.',
    loading: 'Loading preview…',
    notDrawable: '{suffix} is not an image the Studio can draw.',
    notImage: 'This file is not an image.',
    noImage: 'no image',
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
    smoothing: {
      on: 'Интерполяция включена — нажмите, чтобы рисовать пиксели чётко, для пиксель-арта',
      off: 'Интерполяция выключена — пиксели рисуются чётко. Нажмите, чтобы сгладить изображение',
    },
    connect: 'Подключите изображение или укажите путь в панели настроек.',
    loading: 'Загрузка предпросмотра…',
    notDrawable: 'Studio не умеет показывать {suffix} как изображение.',
    notImage: 'Этот файл — не изображение.',
    noImage: 'нет изображения',
    units: {
      bytes: '{value} Б',
      kilobytes: '{value} КБ',
      megabytes: '{value} МБ',
      gigabytes: '{value} ГБ',
    },
  },
}

export default function ({ vue, vueFlow, filePreviewer, rawFileBytes, i18n }) {
  const { computed, defineComponent, h, onBeforeUnmount, ref, shallowRef, watch } = vue
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

  /*
   * Interpolation, shared by every 2D preview in the Studio.
   *
   * The key and the event name are a **contract between plugins**, held by three components
   * that cannot import one another (this node and `image_ops`'s two): a plugin module is
   * loaded with `?token=` appended, so a relative import 401s. Duplicating fifteen lines is
   * the documented trade across plugins; what must not drift is the two strings.
   *
   * It is a *view* preference and deliberately not a node param — a param is what the §8.2
   * cache key hashes, so toggling it would re-run the node to produce the identical file.
   * `localStorage` is what makes it outlive a reload, since a pixel-art project wants it off
   * every time rather than once, and the event is what makes the other previews on the canvas
   * follow the one that was clicked.
   *
   * It reaches a **mounted previewer** too, and that is why the property goes on the viewport
   * rather than on the `<img>`: `image-rendering` inherits, so a plugin's own viewer — which
   * this node knows nothing about beyond its component — is sharpened without being asked.
   */
  const SMOOTHING_KEY = 'voxarium_studio.preview2d.smoothing'
  const SMOOTHING_EVENT = 'voxarium_studio:preview-smoothing'

  function readSmoothing() {
    try {
      return window.localStorage.getItem(SMOOTHING_KEY) !== 'off'
    } catch {
      // Private mode, or storage blocked outright. Smooth is the browser's own default, so
      // an unreadable preference is the same as an unset one.
      return true
    }
  }

  function writeSmoothing(on) {
    try {
      window.localStorage.setItem(SMOOTHING_KEY, on ? 'on' : 'off')
    } catch {
      // Not being able to remember the choice must not stop the user making it.
    }
    window.dispatchEvent(new CustomEvent(SMOOTHING_EVENT, { detail: on }))
  }

  /** Four blocks — the glyph for "show me the pixels". */
  function pixels() {
    return h(
      'svg',
      { width: '11', height: '11', viewBox: '0 0 24 24', fill: 'currentColor' },
      [
        h('rect', { x: '3', y: '3', width: '8', height: '8', rx: '1' }),
        h('rect', { x: '13', y: '3', width: '8', height: '8', rx: '1' }),
        h('rect', { x: '3', y: '13', width: '8', height: '8', rx: '1' }),
        h('rect', { x: '13', y: '13', width: '8', height: '8', rx: '1' }),
      ],
    )
  }

  /** The toggle itself, top-left of the viewport. */
  function smoothingButton(smooth, onToggle) {
    return h(
      'button',
      {
        type: 'button',
        class: 'nodrag',
        style: {
          position: 'absolute',
          top: '4px',
          left: '4px',
          zIndex: '1',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: '18px',
          height: '18px',
          padding: '0',
          borderRadius: 'var(--vxs-radius-sm, 4px)',
          border: '1px solid rgba(255,255,255,0.25)',
          background: smooth ? 'rgba(0,0,0,0.55)' : 'var(--node-accent)',
          color: '#fff',
          opacity: smooth ? 0.65 : 1,
          cursor: 'pointer',
          font: 'inherit',
          lineHeight: '0',
        },
        title: smooth ? t('smoothing.on') : t('smoothing.off'),
        onClick: (event) => {
          // A mounted previewer takes its own gestures in this viewport, so the toggle must
          // not hand it one of ours.
          event.stopPropagation()
          onToggle()
        },
      },
      [pixels()],
    )
  }

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
    name: 'Preview2DNode',
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
       * this node useful before anything has run at all. Both are painted onto `data` by the
       * canvas while a run streams — exactly as `status` is — so they are read through a
       * computed and never captured in `setup`.
       */
      const path = computed(() => {
        const produced = props.data.outputs?.image
        const configured = props.data.params?.image
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
      const source = ref('')
      const loading = ref(false)
      const failure = ref('')

      const smooth = ref(readSmoothing())
      const followSmoothing = (event) => (smooth.value = Boolean(event.detail))
      window.addEventListener(SMOOTHING_EVENT, followSmoothing)
      onBeforeUnmount(() => window.removeEventListener(SMOOTHING_EVENT, followSmoothing))

      /**
       * Whether anything is actually being drawn — a note has no pixels to sharpen. Both
       * branches of `body()` that show something count, the plugin previewer included.
       */
      const showsPicture = computed(
        () =>
          Boolean(path.value) &&
          !loading.value &&
          !failure.value &&
          Boolean(viewer.value || source.value),
      )

      // A blob URL is a live handle into the document, not a string: dropped without this it
      // pins the image's bytes for the life of the page, and a node re-running every few
      // seconds does that once per run.
      let objectUrl = ''
      function releaseObjectUrl() {
        if (!objectUrl) return
        URL.revokeObjectURL(objectUrl)
        objectUrl = ''
      }
      onBeforeUnmount(releaseObjectUrl)

      /**
       * Load the image for a path, discarding a stale answer.
       *
       * Two lookups are in flight whenever a run rewrites the node's output while the previous
       * file is still resolving, and network order is not request order. The ticket is what
       * stops the older answer from painting over the newer one — and, for the blob branch,
       * from revoking a URL the newer one is already showing.
       */
      let ticket = 0
      async function load(target) {
        const mine = ++ticket
        preview.value = null
        viewer.value = null
        source.value = ''
        failure.value = ''
        releaseObjectUrl()
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
          if (!resolved.component) source.value = await imageSource(resolved.preview, mine)
        } catch (cause) {
          if (mine !== ticket) return
          failure.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          if (mine === ticket) loading.value = false
        }
      }

      /** The `<img src>` for a payload: inline if the backend inlined it, blob if not. */
      async function imageSource(payload, mine) {
        if (payload?.kind !== 'image') return ''
        if (payload.data_url) return payload.data_url
        const bytes = await rawFileBytes(payload.path)
        if (mine !== ticket) return ''
        const url = URL.createObjectURL(
          new Blob([bytes], { type: payload.mime || 'application/octet-stream' }),
        )
        objectUrl = url
        return url
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
       * What sits in the viewport. Every outcome says something: a blank rectangle leaves the
       * user guessing whether the node is broken, still loading, or looking at a file that is
       * not an image at all.
       */
      const body = () => {
        if (!path.value) return note(t('connect'))
        if (loading.value) return note(t('loading'))
        if (failure.value) return note(failure.value, 'var(--vxs-danger)')
        if (viewer.value) {
          return h(viewer.value, {
            // Keyed on mtime as well as path, for the reason `FilePreview.vue` gives: a
            // previewer reads the file once when it mounts, so a node regenerating the
            // artifact underneath it must force a remount or the old image stays on screen.
            key: `${preview.value?.path ?? path.value}@${preview.value?.modified ?? ''}`,
            // The payload's own path, not the port's: the backend answers with the
            // project-relative form, which is what `rawFileBytes()` expects.
            path: preview.value?.path ?? path.value,
            preview: preview.value,
          })
        }
        if (source.value) {
          return h('img', {
            src: source.value,
            alt: name.value,
            // The checkerboard is not decoration: a texture with an alpha channel over a flat
            // background is indistinguishable from one that is simply white in that region.
            style: {
              width: '100%',
              height: '100%',
              objectFit: 'contain',
              display: 'block',
            },
          })
        }
        return note(
          preview.value?.message ||
            (suffix.value ? t('notDrawable', { suffix: suffix.value }) : t('notImage')),
        )
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
              h('span', { style: { fontWeight: 500 } }, props.data.label ?? 'Preview 2D'),
              status.value
                ? h(
                    'span',
                    {
                      class: 'vxs-node-chip',
                      // The chip is the only place a failure can surface on this node: a
                      // custom component has no schema-driven body to print `data.error`
                      // into, so it spends the tooltip on it exactly as `GenericNode` does.
                      // Without it a refused path is a red "Error" and no reason.
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
                   * Vue Flow walks up from the event target looking for these classes.
                   * `nodrag` stops a drag inside the viewport from dragging the node across
                   * the canvas, `nowheel` stops a scroll from zooming the canvas, and `nopan`
                   * stops a drag on empty space from panning it. A plugin previewer mounted
                   * here expects to receive those gestures itself.
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
                    // A transparent PNG needs something behind it that is not flat, or a
                    // fully-alpha region reads as a white one.
                    backgroundImage:
                      'linear-gradient(45deg, var(--vxs-bg-surface) 25%, transparent 25%),' +
                      'linear-gradient(-45deg, var(--vxs-bg-surface) 25%, transparent 25%),' +
                      'linear-gradient(45deg, transparent 75%, var(--vxs-bg-surface) 75%),' +
                      'linear-gradient(-45deg, transparent 75%, var(--vxs-bg-surface) 75%)',
                    backgroundSize: '16px 16px',
                    backgroundPosition: '0 0, 0 8px, 8px -8px, -8px 0',
                    // `image-rendering` is an inherited property, which is the whole reason
                    // it goes here: a previewer mounted below is sharpened without this node
                    // knowing anything about it, and without a contract to keep in step.
                    imageRendering: smooth.value ? 'auto' : 'pixelated',
                  },
                },
                [
                  body(),
                  showsPicture.value
                    ? smoothingButton(smooth.value, () => writeSmoothing(!smooth.value))
                    : null,
                ],
              ),

              // What is on screen, in words. A viewport alone does not say which of three
              // candidate textures the node ended up with.
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
                    name.value || t('noImage'),
                  ),
                  preview.value && typeof preview.value.size === 'number'
                    ? h('span', { style: { flex: 'none' } }, formatBytes(preview.value.size))
                    : null,
                ],
              ),
            ]),

            h('div', { style: { paddingBottom: '6px' } }, [
              portRow('target', Position.Left, 'image', 'image'),
              portRow('source', Position.Right, 'image', 'image'),
            ]),
          ],
        )
    },
  })
}
