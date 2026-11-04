/**
 * Custom canvas node for `image_ops.crop` — the picture, and the rectangles it was cut on.
 *
 * The preview half is `RemoveBackgroundNode`'s and for the same reason: this node's whole
 * output is a picture, so a node that does not draw one is a node you have to open the
 * Explorer to evaluate. What is new here is the overlay. A crop is decided by two rectangles
 * the user never typed — the alpha bounding box the node *measured*, and the frame it placed
 * around that box — so the node draws both over the source, which is the only way to see why
 * the result is framed where it is. Switching to the source is therefore a real view rather
 * than a debug affordance, and the button is the only control on the node.
 *
 * It deliberately does **not** take `setNodeParams`. Every setting here is a number the
 * config panel already edits well; nothing on this node produces a value only the node could
 * know, which is what that seam is for.
 *
 * The bytes come through `rawFileBytes` rather than an `<img src>` pointed at `rawFileUrl`,
 * because a plain URL carries no `Authorization` header and 401s under `--token`.
 *
 * No imports, per the plugin contract — `vue` and `vueFlow` arrive through the factory. The
 * chrome is the host's `.vxs-node` family, so this node and a generic one are the same object
 * with different contents.
 */

/**
 * What the node says, in the Studio's interface languages (§6.4). The label, port names, file
 * names and a progress frame's stage are data and are shown as they arrive.
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
    loading: 'Loading…',
    undecodable: 'No preview for this format here — the node still crops it.',
    box: 'box {width}×{height}',
    out: 'out {width}×{height}',
    auto: 'auto',
    boxOnly: 'box',
    compare: {
      source: 'Source',
      result: 'Result',
      showSource: 'Show the source with the bounding box and the frame on it',
      runFirst: 'Run the node once to compare the source with the result',
    },
    caption: {
      result: 'result · {width}×{height}',
      shrunk: 'shrunk ×{scale}',
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
    loading: 'Загрузка…',
    undecodable: 'Здесь нет предпросмотра для этого формата — узел всё равно его обрежет.',
    box: 'рамка {width}×{height}',
    out: 'выход {width}×{height}',
    auto: 'авто',
    boxOnly: 'рамка',
    compare: {
      source: 'Оригинал',
      result: 'Результат',
      showSource: 'Показать оригинал с ограничивающей рамкой и кадром поверх',
      runFirst: 'Запустите узел хотя бы раз, чтобы сравнить оригинал с результатом',
    },
    caption: {
      result: 'результат · {width}×{height}',
      shrunk: 'уменьшено ×{scale}',
    },
  },
}

export default function ({ vue, vueFlow, rawFileBytes, i18n }) {
  const { computed, defineComponent, h, onBeforeUnmount, ref, watch } = vue
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

  /**
   * `failure` for a picture the browser cannot decode. A marker rather than the sentence, so
   * the note is worded when it renders — in the language on screen then, not at the error.
   */
  const UNDECODABLE = Symbol('undecodable')

  /** The shrink factor to two places, with the reader's own decimal separator. */
  const factor = (value) =>
    value.toLocaleString(i18n.locale.value, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

  /** The viewport's own size, in CSS pixels. The overlay arithmetic needs it as a number. */
  const VIEW = { width: 280, height: 200 }

  /*
   * Interpolation, shared by every 2D preview in the Studio.
   *
   * The key and the event name are a **contract between plugins**, held by three components
   * that cannot import one another (`image_ops`'s two nodes and `preview2d`'s): a plugin
   * module is loaded with `?token=` appended, so a relative import 401s. Duplicating fifteen
   * lines is the documented trade across plugins; what must not drift is the two strings.
   *
   * It is a *view* preference and deliberately not a node param — a param is what the §8.2
   * cache key hashes, so toggling it would re-run the node to produce the identical file.
   * `localStorage` is what makes it outlive a reload, since a pixel-art project wants it off
   * every time rather than once, and the event is what makes the other previews on the canvas
   * follow the one that was clicked.
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

  /** The toggle itself, top-left of whatever viewport it is dropped into. */
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
          // The viewport around this button is a click target of its own on some of these
          // nodes (the eyedropper), so the toggle must not also register as one.
          event.stopPropagation()
          onToggle()
        },
      },
      [pixels()],
    )
  }

  /** An array of exactly `length` finite numbers, or null — the node's rectangles and sizes
   * arrive as plain JSON from a node script, so nothing about their shape is guaranteed. */
  function numbers(value, length) {
    if (!Array.isArray(value) || value.length !== length) return null
    const parsed = value.map(Number)
    return parsed.every((n) => Number.isFinite(n)) ? parsed : null
  }

  /** Lucide's `crop`, inlined: a plugin bundle cannot import the host's icon set. */
  function crop() {
    return h(
      'svg',
      {
        width: '11',
        height: '11',
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        'stroke-width': '2',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
      },
      [
        h('path', { d: 'M6 2v14a2 2 0 0 0 2 2h14' }),
        h('path', { d: 'M18 22V8a2 2 0 0 0-2-2H2' }),
      ],
    )
  }

  return defineComponent({
    name: 'CropNode',
    props: {
      id: { type: String, required: true },
      data: { type: Object, required: true },
      selected: { type: Boolean, default: false },
    },
    setup(props) {
      const status = computed(() => props.data.status ?? null)
      const statusTone = computed(() =>
        status.value ? `var(--vxs-status-${status.value}, var(--vxs-text-faint))` : null,
      )
      const statusText = computed(() => (status.value ? statusLabel(status.value) : ''))
      const progress = computed(() =>
        status.value === 'running' ? (props.data.progress ?? null) : null,
      )

      const schema = computed(() => props.data.schema ?? null)
      const portDefault = (portId) =>
        schema.value?.inputs?.find((port) => port.id === portId)?.default

      const params = computed(() => props.data.params ?? {})
      const setting = (portId) => {
        const value = params.value[portId] ?? portDefault(portId)
        return Number.isFinite(Number(value)) ? Number(value) : 0
      }
      const width = computed(() => setting('width'))
      const height = computed(() => setting('height'))
      const threshold = computed(() => setting('alpha_threshold'))

      const outputs = computed(() => props.data.outputs ?? {})

      /**
       * The picture the box was measured on.
       *
       * `outputs.source` is what the last run actually resolved — the only place the path
       * exists when the image arrives on an edge rather than being typed into the config
       * panel. It is emitted by the node script and is deliberately not a port.
       */
      const sourcePath = computed(() =>
        String(outputs.value.source ?? params.value.image ?? '').trim(),
      )
      const resultPath = computed(() => String(outputs.value.image ?? '').trim())

      const bounds = computed(() => numbers(outputs.value.bounds, 4))
      /**
       * The region of the *source* that became the output — the output frame divided back
       * through the scale, so it is the output's own size only when nothing was shrunk.
       * `size` is the file that was written, and the two are deliberately separate keys.
       */
      const frame = computed(() => numbers(outputs.value.frame, 4))
      const size = computed(() => numbers(outputs.value.size, 2))
      const measuredSize = computed(() => numbers(outputs.value.source_size, 2))
      const scale = computed(() => {
        const value = Number(outputs.value.scale)
        return Number.isFinite(value) && value > 0 && value < 1 ? value : null
      })

      const showSource = ref(false)
      const canSwap = computed(() => Boolean(sourcePath.value && resultPath.value))
      const shownPath = computed(() =>
        showSource.value ? sourcePath.value : resultPath.value || sourcePath.value,
      )
      const showingResult = computed(
        () => Boolean(resultPath.value) && shownPath.value === resultPath.value,
      )

      const source = ref('')
      const loading = ref(false)
      const failure = ref('')
      const natural = ref(null)

      const smooth = ref(readSmoothing())
      const followSmoothing = (event) => (smooth.value = Boolean(event.detail))
      window.addEventListener(SMOOTHING_EVENT, followSmoothing)
      onBeforeUnmount(() => window.removeEventListener(SMOOTHING_EVENT, followSmoothing))

      /** Whether there is actually a picture to sharpen — a note has nothing to toggle. */
      const showsPicture = computed(
        () => Boolean(shownPath.value) && !loading.value && !failure.value && Boolean(source.value),
      )

      // A blob URL pins the bytes it wraps for the life of the document unless it is
      // revoked, and this node reloads on every run.
      let objectUrl = ''

      function release() {
        if (objectUrl) URL.revokeObjectURL(objectUrl)
        objectUrl = ''
      }
      onBeforeUnmount(release)

      /** Load a path, discarding an answer that arrives after a newer request. */
      let ticket = 0
      async function load(path) {
        const mine = ++ticket
        release()
        source.value = ''
        failure.value = ''
        natural.value = null
        if (!path) {
          loading.value = false
          return
        }
        loading.value = true
        try {
          const bytes = await rawFileBytes(path)
          if (mine !== ticket) return
          objectUrl = URL.createObjectURL(new Blob([bytes]))
          source.value = objectUrl
        } catch (cause) {
          if (mine !== ticket) return
          failure.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          if (mine === ticket) loading.value = false
        }
      }

      watch(shownPath, load, { immediate: true })

      /**
       * Screen geometry for a rectangle given in source pixels, through the letterbox
       * `object-fit: contain` leaves — or null when the overlay would be a lie.
       *
       * The size guard is the point of the function. The box was measured on the bytes the
       * *node* decoded, and the browser can legitimately disagree about what those bytes are
       * — an EXIF-rotated photo is the case that actually happens, since the decoder here
       * uprights it and browsers vary on whether `naturalWidth` reports the rotated size.
       * Two different pictures means the rectangle belongs to neither, so it is not drawn.
       */
      function place(rect) {
        const size = natural.value
        const measured = measuredSize.value
        if (!rect || !size || !measured) return null
        if (measured[0] !== size.width || measured[1] !== size.height) return null
        const scale = Math.min(VIEW.width / size.width, VIEW.height / size.height)
        return {
          position: 'absolute',
          boxSizing: 'border-box',
          pointerEvents: 'none',
          left: `${(VIEW.width - size.width * scale) / 2 + rect[0] * scale}px`,
          top: `${(VIEW.height - size.height * scale) / 2 + rect[1] * scale}px`,
          width: `${rect[2] * scale}px`,
          height: `${rect[3] * scale}px`,
        }
      }

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

      /** The two rectangles, drawn over the source only — on the result they are the frame. */
      const overlay = () => {
        if (showingResult.value) return null
        const box = place(bounds.value)
        const cut = place(frame.value)
        return [
          cut
            ? h('div', {
                key: 'frame',
                style: {
                  ...cut,
                  border: '1px dashed rgba(255,255,255,0.85)',
                  boxShadow: '0 0 0 1px rgba(0,0,0,0.45)',
                },
              })
            : null,
          box
            ? h('div', {
                key: 'bounds',
                style: {
                  ...box,
                  border: '1px solid var(--node-accent)',
                  background: 'color-mix(in oklab, var(--node-accent) 12%, transparent)',
                },
              })
            : null,
          box || cut
            ? h(
                'div',
                {
                  key: 'legend',
                  style: {
                    position: 'absolute',
                    left: '0',
                    right: '0',
                    bottom: '0',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '8px',
                    padding: '3px 6px',
                    fontSize: '10px',
                    fontFamily: 'var(--vxs-font-mono)',
                    color: '#fff',
                    background: 'rgba(0,0,0,0.55)',
                    pointerEvents: 'none',
                  },
                },
                [
                  bounds.value
                    ? h('span', t('box', { width: bounds.value[2], height: bounds.value[3] }))
                    : null,
                  size.value
                    ? h(
                        'span',
                        { style: { opacity: 0.75 } },
                        t('out', { width: size.value[0], height: size.value[1] }),
                      )
                    : null,
                  // Only when the subject had to be shrunk — the one case where the output
                  // is not the source's own pixels, which is worth saying on the picture.
                  scale.value
                    ? h(
                        'span',
                        { style: { color: 'var(--node-accent)', fontWeight: 600 } },
                        `×${factor(scale.value)}`,
                      )
                    : null,
                ],
              )
            : null,
        ]
      }

      const body = () => {
        if (!shownPath.value) {
          return note(t('connect'))
        }
        if (loading.value) return note(t('loading'))
        if (failure.value) {
          return note(
            failure.value === UNDECODABLE ? t('undecodable') : failure.value,
            'var(--vxs-danger)',
          )
        }
        return [
          h('img', {
            src: source.value,
            alt: '',
            draggable: false,
            style: {
              width: '100%',
              height: '100%',
              objectFit: 'contain',
              display: 'block',
            },
            onLoad: (event) => {
              natural.value = {
                width: event.target.naturalWidth,
                height: event.target.naturalHeight,
              }
            },
            // A format the browser cannot decode — .tga, .exr — is a real input for this
            // node, and an empty frame would look like a bug in the node instead.
            onError: () => {
              failure.value = UNDECODABLE
            },
          }),
          ...(overlay() ?? []),
        ]
      }

      /** One port row; the handle centres itself on it because the row is its containing block. */
      const portRow = (type, position, port) =>
        h(
          'div',
          {
            key: `${type}-${port.id}`,
            class: 'vxs-node-port',
            style: position === Position.Right ? { justifyContent: 'flex-end' } : null,
            title: `${port.label || port.id} (${port.type})`,
          },
          [
            h(Handle, { type, position, id: port.id, style: { background: 'var(--node-accent)' } }),
            h('span', { class: 'truncate' }, port.label || port.id),
          ],
        )

      /**
       * Which inputs get a handle: whatever this instance pins, plus anything already
       * connected. `data.pins` overrides the schema per node, so a hard-coded row per port
       * would be a node whose pins the config panel cannot change.
       */
      const inputRows = () => {
        const connected = new Set(props.data.connected ?? [])
        return (schema.value?.inputs ?? [])
          .filter((port) => connected.has(port.id) || (props.data.pins?.[port.id] ?? port.connectable))
          .map((port) => portRow('target', Position.Left, port))
      }

      /** What the *settings* ask for, which is not the same as what the last run produced. */
      const requested = computed(() =>
        width.value || height.value
          ? `${width.value || t('auto')} × ${height.value || t('auto')}`
          : t('boxOnly'),
      )

      const caption = () => {
        if (showingResult.value && size.value) {
          return [
            t('caption.result', { width: size.value[0], height: size.value[1] }),
            bounds.value ? t('box', { width: bounds.value[2], height: bounds.value[3] }) : null,
            scale.value ? t('caption.shrunk', { scale: factor(scale.value) }) : null,
          ]
            .filter(Boolean)
            .join(' · ')
        }
        return shownPath.value.split(/[\\/]/).pop() ?? ''
      }

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
              h('span', { style: { fontWeight: 500 } }, props.data.label ?? 'Crop'),
              status.value
                ? h(
                    'span',
                    { class: 'vxs-node-chip', title: props.data.error ?? undefined },
                    statusText.value,
                  )
                : null,
            ]),

            h('div', { style: { padding: '8px 10px' } }, [
              h(
                'div',
                {
                  // Vue Flow reads these off the event target's ancestors: without them a
                  // drag inside the viewport drags the node and a scroll zooms the canvas.
                  class: 'nodrag nowheel nopan',
                  style: {
                    position: 'relative',
                    // `content-box` explicitly, because the host's reset sets `border-box`
                    // globally and the overlay arithmetic is done against `VIEW`. Under
                    // `border-box` the 1px border comes out of the content area, so the
                    // picture is letterboxed inside 278x198 while the rectangles are placed
                    // as if it were 280x200 — every box drawn a percent too large and half a
                    // pixel off. Measuring instead would work; being the size we claim is
                    // simpler and cannot drift.
                    boxSizing: 'content-box',
                    width: `${VIEW.width}px`,
                    height: `${VIEW.height}px`,
                    overflow: 'hidden',
                    borderRadius: 'var(--vxs-radius-md)',
                    border: '1px solid var(--vxs-border)',
                    background: 'var(--vxs-bg-inset)',
                    // This node both reads alpha and writes it as padding, and alpha over a
                    // flat backdrop is indistinguishable from paint.
                    backgroundImage:
                      'linear-gradient(45deg, var(--vxs-bg-surface) 25%, transparent 25%),' +
                      'linear-gradient(-45deg, var(--vxs-bg-surface) 25%, transparent 25%),' +
                      'linear-gradient(45deg, transparent 75%, var(--vxs-bg-surface) 75%),' +
                      'linear-gradient(-45deg, transparent 75%, var(--vxs-bg-surface) 75%)',
                    backgroundSize: '16px 16px',
                    backgroundPosition: '0 0, 0 8px, 8px -8px, -8px 0',
                    // `image-rendering` is an inherited property, so setting it on the
                    // viewport reaches the picture without the picture having to know.
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

              h(
                'div',
                {
                  class: 'nodrag',
                  style: {
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    margin: '6px 0 0',
                    fontSize: '10px',
                    fontFamily: 'var(--vxs-font-mono)',
                    color: 'var(--vxs-text-faint)',
                  },
                },
                [
                  h('span', { style: { display: 'flex', flex: 'none' } }, [crop()]),
                  h('span', requested.value),
                  h('span', { style: { opacity: 0.7 } }, `α>${threshold.value}%`),
                  h(
                    'button',
                    {
                      type: 'button',
                      disabled: !canSwap.value,
                      style: {
                        marginLeft: 'auto',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        padding: '1px 6px',
                        borderRadius: 'var(--vxs-radius-sm, 4px)',
                        border: '1px solid var(--vxs-border)',
                        background: showSource.value ? 'var(--node-accent)' : 'transparent',
                        color: showSource.value ? '#fff' : 'var(--vxs-text-secondary)',
                        cursor: canSwap.value ? 'pointer' : 'not-allowed',
                        opacity: canSwap.value ? 1 : 0.5,
                        font: 'inherit',
                      },
                      title: canSwap.value ? t('compare.showSource') : t('compare.runFirst'),
                      onClick: (event) => {
                        event.stopPropagation()
                        showSource.value = !showSource.value
                      },
                    },
                    showSource.value ? t('compare.result') : t('compare.source'),
                  ),
                ],
              ),

              h(
                'div',
                {
                  style: {
                    marginTop: '2px',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    fontSize: '10px',
                    fontFamily: 'var(--vxs-font-mono)',
                    color: 'var(--vxs-text-faint)',
                  },
                },
                caption(),
              ),
            ]),

            progress.value
              ? h(
                  'div',
                  {
                    class: [
                      'vxs-node-progress',
                      progress.value.percent === null ? 'vxs-node-progress-unknown' : null,
                    ],
                  },
                  [
                    h('div', { class: 'vxs-node-progress-track' }, [
                      h('div', {
                        class: 'vxs-node-progress-fill',
                        style:
                          progress.value.percent === null
                            ? null
                            : { width: `${progress.value.percent}%` },
                      }),
                    ]),
                    progress.value.label
                      ? h('p', { class: 'vxs-node-progress-text' }, [
                          h('span', progress.value.label),
                          h('span', progress.value.detail ?? ''),
                        ])
                      : null,
                  ],
                )
              : null,

            h('div', { class: 'vxs-node-ports' }, [
              ...inputRows(),
              ...(schema.value?.outputs ?? []).map((port) =>
                h(
                  'div',
                  {
                    key: `out-${port.id}`,
                    class: 'vxs-node-port vxs-node-port-out',
                    title: `${port.label || port.id} (${port.type})`,
                  },
                  [
                    h(Handle, {
                      type: 'source',
                      position: Position.Right,
                      id: port.id,
                      style: { background: 'var(--node-accent)' },
                    }),
                    h('span', { class: 'truncate' }, port.label || port.id),
                  ],
                ),
              ),
            ]),
          ],
        )
    },
  })
}
