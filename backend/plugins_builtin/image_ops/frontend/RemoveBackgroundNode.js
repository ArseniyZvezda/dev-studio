/**
 * Custom canvas node for `image_ops.remove_background` — the cutout, and the eyedropper.
 *
 * The node exists as a component for one reason the generic renderer cannot cover: "that
 * colour, there". A key colour typed as six hex digits is a value nobody can aim, so the
 * node draws the picture it is about to key and samples the pixel under the cursor —
 * from the *decoded file*, at its own resolution, not from whatever the screen happens to
 * be showing it scaled to. The result goes back into the pipeline document through
 * `setNodeParams`, which is the host's seam for exactly this: writing `data.params`
 * directly mutates the instance without the editor noticing, and the edit is lost on save.
 *
 * The bytes come through `rawFileBytes` rather than an `<img src>` pointed at
 * `rawFileUrl`, and that is load-bearing twice over: a plain URL carries no
 * `Authorization` header, so it 401s under `--token`, and a canvas drawn from a
 * same-origin blob is one `getImageData` can read at all.
 *
 * No imports, per the plugin contract — `vue` and `vueFlow` arrive through the factory.
 * The chrome is the host's `.vxs-node` family, so this node and a generic one are the
 * same object with different contents.
 */

/**
 * What the node says, in the Studio's interface languages (§6.4). The label, port names, file
 * names, colour values and a progress frame's stage are data and are shown as they arrive.
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
    undecodable: 'No preview for this format here — type the colour instead.',
    keyColour: 'Key colour',
    pick: {
      hovered: '{colour} — click to key it out',
      prompt: 'Click a background pixel',
      start: 'Pick',
      cancel: 'Cancel',
      title: 'Take the key colour off the image',
      unavailable: 'Run the node once, or type an image path, to sample from it',
    },
    caption: {
      removed: 'result · {count} px out',
      removedAndEdge: 'result · {count} px out, {edge} on the edge',
      result: 'result · {name}',
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
    undecodable: 'Здесь нет предпросмотра для этого формата — введите цвет вручную.',
    keyColour: 'Ключевой цвет',
    pick: {
      hovered: '{colour} — нажмите, чтобы убрать',
      prompt: 'Нажмите на пиксель фона',
      start: 'Взять',
      cancel: 'Отмена',
      title: 'Взять ключевой цвет с изображения',
      unavailable: 'Запустите узел хотя бы раз или укажите путь к изображению, чтобы взять с него цвет',
    },
    caption: {
      removed: 'результат · убрано {count} px',
      removedAndEdge: 'результат · убрано {count} px, {edge} на краю',
      result: 'результат · {name}',
    },
  },
}

export default function ({ vue, vueFlow, rawFileBytes, setNodeParams, i18n }) {
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

  const DEFAULT_COLOR = '#ffffff'

  const hex = (r, g, b) =>
    '#' + [r, g, b].map((channel) => channel.toString(16).padStart(2, '0')).join('')

  /** `#rgb`, `#rrggbb` or nothing at all, as the swatch and the sampler both need it. */
  function normalise(value) {
    const raw = String(value ?? '')
      .trim()
      .toLowerCase()
    const digits = raw.startsWith('#') ? raw.slice(1) : raw
    const expanded =
      digits.length === 3
        ? digits
            .split('')
            .map((digit) => digit + digit)
            .join('')
        : digits.slice(0, 6)
    return /^[0-9a-f]{6}$/.test(expanded) ? `#${expanded}` : null
  }

  /*
   * Interpolation, shared by every 2D preview in the Studio.
   *
   * The key and the event name are a **contract between plugins**, held by three components
   * that cannot import one another (this node, `image_ops.crop` and `preview2d`'s): a plugin
   * module is loaded with `?token=` appended, so a relative import 401s. Duplicating fifteen
   * lines is the documented trade across plugins; what must not drift is the two strings.
   *
   * It is a *view* preference and deliberately not a node param — a param is what the §8.2
   * cache key hashes, so toggling it would re-run the node to produce the identical cutout.
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
          // This viewport *is* a click target — it is what the eyedropper samples on — so
          // the toggle has to stop the event before it reads as a pick.
          event.stopPropagation()
          onToggle()
        },
      },
      [pixels()],
    )
  }

  /** Lucide's `pipette`, inlined: a plugin bundle cannot import the host's icon set. */
  function pipette() {
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
        h('path', { d: 'm2 22 1-1h3l9-9' }),
        h('path', { d: 'M3 21v-3l9-9' }),
        h('path', {
          d: 'm15 6 3.4-3.4a2.1 2.1 0 1 1 3 3L18 9l.4.4a2.1 2.1 0 1 1-3 3l-3.8-3.8a2.1 2.1 0 1 1 3-3l.4.4Z',
        }),
      ],
    )
  }

  return defineComponent({
    name: 'RemoveBackgroundNode',
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
      const keyColor = computed(
        () => normalise(params.value.key_color ?? portDefault('key_color')) ?? DEFAULT_COLOR,
      )
      const tolerance = computed(() => {
        const value = params.value.tolerance ?? portDefault('tolerance')
        return Number.isFinite(Number(value)) ? Number(value) : 0
      })

      /**
       * The picture the eyedropper works on.
       *
       * `outputs.source` is what the last run actually resolved — the only place the path
       * exists when the image arrives on an edge rather than being typed into the config
       * panel. It is emitted by the node script and is deliberately not a port: a pin
       * nobody would ever wire is worse than an undeclared key.
       */
      const sourcePath = computed(() =>
        String(props.data.outputs?.source ?? params.value.image ?? '').trim(),
      )
      const resultPath = computed(() => String(props.data.outputs?.image ?? '').trim())

      const picking = ref(false)
      const hovered = ref(null)

      // While picking, the source is what has to be on screen: sampling the cutout would
      // read the colour that is already gone, or a transparent pixel.
      const shownPath = computed(() =>
        picking.value ? sourcePath.value : resultPath.value || sourcePath.value,
      )
      const showingResult = computed(
        () => !picking.value && Boolean(resultPath.value) && shownPath.value === resultPath.value,
      )

      const source = ref('')
      const loading = ref(false)
      const failure = ref('')
      const imageEl = ref(null)

      const smooth = ref(readSmoothing())
      const followSmoothing = (event) => (smooth.value = Boolean(event.detail))
      window.addEventListener(SMOOTHING_EVENT, followSmoothing)
      onBeforeUnmount(() => window.removeEventListener(SMOOTHING_EVENT, followSmoothing))

      /** Whether there is actually a picture to sharpen — a note has nothing to toggle. */
      const showsPicture = computed(
        () => Boolean(shownPath.value) && !loading.value && !failure.value,
      )

      // A blob URL pins the bytes it wraps for the life of the document unless it is
      // revoked, and this node reloads on every run.
      let objectUrl = ''
      let sampler = null

      function release() {
        if (objectUrl) URL.revokeObjectURL(objectUrl)
        objectUrl = ''
        sampler = null
      }
      onBeforeUnmount(release)

      /** Load a path, discarding an answer that arrives after a newer request. */
      let ticket = 0
      async function load(path) {
        const mine = ++ticket
        release()
        source.value = ''
        failure.value = ''
        hovered.value = null
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
      // The canvas below caches one decoded picture. A new blob means the `<img>` is about
      // to hold a different one, so the cache has to go with it — otherwise the eyedropper
      // keeps sampling the image before last.
      watch(source, () => {
        sampler = null
      })

      /**
       * The image drawn once at its own resolution, so a sample is the file's pixel rather
       * than the screen's. `getImageData` per sample rather than one big `ImageData`: a
       * 4096² source would be 64 MB held for the life of the node.
       */
      function context() {
        const element = imageEl.value
        if (!element || !element.naturalWidth) return null
        if (sampler) return sampler
        const canvas = document.createElement('canvas')
        canvas.width = element.naturalWidth
        canvas.height = element.naturalHeight
        const ctx = canvas.getContext('2d', { willReadFrequently: true })
        if (!ctx) return null
        ctx.drawImage(element, 0, 0)
        sampler = ctx
        return ctx
      }

      /**
       * Screen point -> file pixel, through the letterbox `object-fit: contain` leaves.
       *
       * Doing the arithmetic rather than sizing a canvas to the viewport is what keeps the
       * sample exact: the browser's downscale of a 2048² texture into 280 px has already
       * averaged four neighbouring colours into the one under the cursor, and keying
       * against an average of the edge is how a halo survives.
       */
      function sample(event) {
        const element = imageEl.value
        const ctx = context()
        if (!element || !ctx) return null
        const rect = element.getBoundingClientRect()
        const scale = Math.min(
          rect.width / element.naturalWidth,
          rect.height / element.naturalHeight,
        )
        const x = (event.clientX - rect.left - (rect.width - element.naturalWidth * scale) / 2) / scale
        const y = (event.clientY - rect.top - (rect.height - element.naturalHeight * scale) / 2) / scale
        if (x < 0 || y < 0 || x >= element.naturalWidth || y >= element.naturalHeight) return null
        const [r, g, b] = ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data
        return hex(r, g, b)
      }

      function onMove(event) {
        if (!picking.value) return
        hovered.value = sample(event)
      }

      function onPick(event) {
        if (!picking.value) return
        const colour = sample(event)
        picking.value = false
        hovered.value = null
        if (!colour) return
        // Through the host: assigning to `data.params` would edit the document behind the
        // editor's back and the colour would not survive Save.
        setNodeParams(props.id, { key_color: colour })
      }

      const swatch = (colour, size) =>
        h('span', {
          style: {
            width: size,
            height: size,
            flex: 'none',
            borderRadius: '3px',
            border: '1px solid var(--vxs-border)',
            background: colour,
            // The key colour can be white on a light theme, or transparent-looking on a
            // dark one; the border is what keeps the swatch a swatch.
            boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.15)',
          },
        })

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
            ref: imageEl,
            src: source.value,
            alt: '',
            draggable: false,
            style: {
              width: '100%',
              height: '100%',
              objectFit: 'contain',
              display: 'block',
              cursor: picking.value ? 'crosshair' : 'default',
            },
            // A format the browser cannot decode — .tga, .exr — is a real input for this
            // node, and an empty frame would look like a bug in the node instead.
            onError: () => {
              failure.value = UNDECODABLE
            },
          }),
          picking.value
            ? h(
                'div',
                {
                  style: {
                    position: 'absolute',
                    left: '0',
                    right: '0',
                    bottom: '0',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '6px',
                    padding: '3px 6px',
                    fontSize: '10px',
                    fontFamily: 'var(--vxs-font-mono)',
                    color: '#fff',
                    background: 'rgba(0,0,0,0.55)',
                  },
                },
                hovered.value
                  ? [swatch(hovered.value, '10px'), t('pick.hovered', { colour: hovered.value })]
                  : [t('pick.prompt')],
              )
            : null,
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

      const caption = () => {
        const removed = props.data.outputs?.removed_pixels
        const edge = props.data.outputs?.edge_pixels
        if (showingResult.value && typeof removed === 'number') {
          return typeof edge === 'number'
            ? t('caption.removedAndEdge', {
                count: removed,
                edge: edge.toLocaleString(i18n.locale.value),
              })
            : t('caption.removed', { count: removed })
        }
        const name = shownPath.value.split(/[\\/]/).pop() ?? ''
        return showingResult.value ? t('caption.result', { name }) : name
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
              h('span', { style: { fontWeight: 500 } }, props.data.label ?? 'Remove Background'),
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
                    width: '280px',
                    height: '200px',
                    overflow: 'hidden',
                    borderRadius: 'var(--vxs-radius-md)',
                    border: '1px solid var(--vxs-border)',
                    background: 'var(--vxs-bg-inset)',
                    // The point of this node is alpha, and alpha over a flat backdrop is
                    // indistinguishable from white paint.
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
                  onMousemove: onMove,
                  onMouseleave: () => (hovered.value = null),
                  onClick: onPick,
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
                  // A label wrapping a transparent colour input: clicking the swatch opens
                  // the platform picker, for the times the colour is known rather than seen.
                  h(
                    'label',
                    {
                      style: { position: 'relative', display: 'flex', cursor: 'pointer' },
                      title: t('keyColour'),
                    },
                    [
                      swatch(keyColor.value, '14px'),
                      h('input', {
                        type: 'color',
                        value: keyColor.value,
                        style: {
                          position: 'absolute',
                          inset: '0',
                          width: '100%',
                          height: '100%',
                          opacity: 0,
                          cursor: 'pointer',
                        },
                        onInput: (event) =>
                          setNodeParams(props.id, { key_color: event.target.value }),
                      }),
                    ],
                  ),
                  h('span', keyColor.value),
                  h('span', { style: { opacity: 0.7 } }, `±${tolerance.value}%`),
                  h(
                    'button',
                    {
                      type: 'button',
                      disabled: !sourcePath.value,
                      style: {
                        marginLeft: 'auto',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        padding: '1px 6px',
                        borderRadius: 'var(--vxs-radius-sm, 4px)',
                        border: '1px solid var(--vxs-border)',
                        background: picking.value ? 'var(--node-accent)' : 'transparent',
                        color: picking.value ? '#fff' : 'var(--vxs-text-secondary)',
                        cursor: sourcePath.value ? 'pointer' : 'not-allowed',
                        opacity: sourcePath.value ? 1 : 0.5,
                        font: 'inherit',
                      },
                      title: sourcePath.value ? t('pick.title') : t('pick.unavailable'),
                      onClick: (event) => {
                        event.stopPropagation()
                        picking.value = !picking.value
                        hovered.value = null
                      },
                    },
                    [pipette(), picking.value ? t('pick.cancel') : t('pick.start')],
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
