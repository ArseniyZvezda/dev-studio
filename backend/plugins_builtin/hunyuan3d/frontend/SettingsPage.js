/**
 * Settings page for the Hunyuan3D plugin — the worked example of the §9.3 page contract.
 *
 * Loaded at runtime as a plain ES module from `/api/plugins/hunyuan3d/assets/…`, so it has
 * no imports at all: `vue` and the API client arrive through the factory. A second Vue
 * instance would share no reactivity with the app, and a bare `vue` specifier cannot
 * resolve in the browser.
 *
 * Single file on purpose — the host appends `?token=` to this URL, and a relative
 * `import './helper.js'` from here would resolve without it and 401 in server mode.
 *
 * Every setting here is real. The two groups are not interchangeable:
 *
 *   `default_*`  becomes a port default in plugin.py, so it lands in a node's params and
 *                is therefore part of the §8.2 cache key — changing one re-runs the node.
 *   `hunyuan_*`  is transport: where and how Hunyuan3D is reached. Read by the node script
 *                at run time and deliberately outside the cache key, because the same
 *                model reached over a different URL is the same model.
 *
 * `face_count` moved across that line: it decimates the mesh, so it is a node default now.
 * The old `hunyuan_face_count` is still read as the fallback in plugin.py.
 */

const PLUGIN_ID = 'hunyuan3d'

/**
 * The page's own words (§6.4), English first. A plugin cannot add keys to the Studio's locale
 * files, so they live here and `i18n.translator` follows the Studio's language. What the
 * server's /health and the environment build report is shown as written, and so are option
 * values (`server`, `glb`, `cuda`), which are what the settings store.
 */
const MESSAGES = {
  en: {
    title: 'Hunyuan3D Settings',
    intro: 'Project defaults for the generation nodes, how Hunyuan3D is reached, and the plugin’s isolated environment.',
    loading: 'Loading…',
    save: 'Save',
    saving: 'Saving…',
    savedAt: 'Saved at {time}',
    defaults: {
      title: 'Node defaults',
      blurb:
        'Used when a node leaves the field unset. A value on the node always wins, and changing any of these re-runs cached nodes.',
      outputFormat: { label: 'Output format', hint: 'The server exports glb and obj; stl is converted from glb' },
      resolution: { label: 'Voxel resolution', hint: 'Voxelizer grid size, 8–200' },
      checkpoint: { label: 'Checkpoint', hint: 'Image → Mesh, local mode only; in server mode the server chooses' },
      mvCheckpoint: {
        label: 'Multi-view checkpoint',
        hint: 'Multiview → Mesh, local mode only. Its own repository — 2mv was never folded into 2.1',
      },
      steps: { label: 'Steps', hint: 'Flow-matching steps (5 draft, 30 normal)' },
      guidance: { label: 'Guidance', hint: 'Conditioning strength, 0–20' },
      octree: { label: 'Octree resolution', hint: 'Marching-cubes grid, 64–512. Lower it on an OOM' },
      numChunks: { label: 'Decode chunk size', hint: 'Volume-decoding batch. Lower it on an OOM' },
      faceCount: { label: 'Face count', hint: 'Decimation target before UV unwrapping' },
      removeBackground: { label: 'Remove background', hint: 'rembg first — the shape model segments by alpha' },
      texture: { label: 'Generate texture', hint: 'The PBR pass, 5–8x slower' },
      textureResolution: { label: 'Texture resolution', hint: 'Per-view diffusion resolution' },
      bakeViews: { label: 'Bake views', hint: '0 lets the server choose by card size. On 12 GB, do not set 6' },
    },
    connection: {
      blurb:
        'Where the model runs. These are not part of the result cache — the same checkpoint reached over a different URL is the same model.',
      mode: { label: 'Mode', hint: 'server calls the API server; local imports the model here' },
      serverUrl: {
        label: 'Server URL',
        hint: 'Base URL of the Hunyuan3D API server (run_server.bat defaults to 8081)',
      },
      transport: { label: 'Transport', hint: 'tasks queues and polls; sync holds one request open; auto probes' },
      pollInterval: { label: 'Poll interval', hint: 'Seconds between task status checks' },
      imagePath: {
        label: 'Send image by path',
        hint: 'auto sends a path when the server is on this machine, base64 otherwise',
      },
      apiKeyEnv: { label: 'API key env var', hint: 'Name of an env var to send as a bearer token; blank for none' },
      timeout: {
        label: 'Silence timeout',
        hint:
          'Seconds the server may say nothing before the node gives up. Not a limit on how long a generation may ' +
          'take — every answered status poll resets it.',
      },
      check: 'Check server',
      checking: 'Checking…',
      device: { label: 'Device', hint: 'torch device for both pipelines' },
      subfolder: { label: 'Checkpoint subfolder', hint: 'e.g. hunyuan3d-dit-v2-1; blank for the default' },
      mvSubfolder: { label: 'Multi-view subfolder', hint: 'e.g. hunyuan3d-dit-v2-mv; blank for the default' },
      texgenPath: { label: 'Texture checkpoint', hint: 'Blank reuses the shape checkpoint' },
    },
    health: {
      unknownDevice: 'unknown device',
      bakeViews: { one: 'picks {count} bake view', other: 'picks {count} bake views' },
      vram: 'VRAM {used}/{total} GB used — {server} GB this server, {others} GB other processes',
      texturesAvailable: {
        one: 'Textures available · {count} job queued',
        other: 'Textures available · {count} jobs queued',
      },
      texturesUnavailable: 'Textures unavailable: {reason}',
      mvUnavailable: 'Multi-view model unavailable: {reason}',
      mvAvailable: 'Multi-view model available · single-image runs use {model}',
      onCard: '{model} on the card',
      noReason: 'no reason given',
    },
    environment: {
      title: 'Isolated environment',
      blurb:
        'The generation nodes run out of process. Without a venv they fall back to the Studio’s interpreter, which ' +
        'works but skips numpy. Hunyuan3D in server mode needs nothing installed here; local mode needs torch and ' +
        'the model packages added to requirements.txt first — see the plugin’s README.',
      status: 'Status: {state}',
      unknown: 'unknown',
      provisioned: 'provisioned',
      notBuilt: 'not built',
      build: 'Build environment',
      building: 'Building…',
      provisioning: 'Provisioning the plugin environment with uv…',
      done: 'Done.',
    },
  },
  ru: {
    title: 'Настройки Hunyuan3D',
    intro: 'Значения по умолчанию для узлов генерации в этом проекте, способ подключения к Hunyuan3D и изолированное окружение плагина.',
    loading: 'Загрузка…',
    save: 'Сохранить',
    saving: 'Сохранение…',
    savedAt: 'Сохранено в {time}',
    defaults: {
      title: 'Параметры узлов по умолчанию',
      blurb:
        'Применяются, когда поле на узле не заполнено. Значение, заданное на самом узле, всегда важнее, а после ' +
        'изменения любого из этих параметров закэшированные узлы выполнятся заново.',
      outputFormat: { label: 'Выходной формат', hint: 'Сервер экспортирует glb и obj; stl получается конвертацией из glb' },
      resolution: { label: 'Разрешение вокселей', hint: 'Размер сетки вокселизатора, 8–200' },
      checkpoint: {
        label: 'Чекпоинт',
        hint: 'Для Image → Mesh, только в локальном режиме; в серверном режиме его выбирает сервер',
      },
      mvCheckpoint: {
        label: 'Многоракурсный чекпоинт',
        hint: 'Для Multiview → Mesh, только в локальном режиме. Это отдельный репозиторий — 2mv так и не влили в 2.1',
      },
      steps: { label: 'Шаги', hint: 'Шаги flow matching (5 — черновик, 30 — обычное качество)' },
      guidance: { label: 'Guidance', hint: 'Сила обусловливания, 0–20' },
      octree: { label: 'Разрешение октодерева', hint: 'Сетка marching cubes, 64–512. Уменьшите при OOM' },
      numChunks: { label: 'Размер чанка декодирования', hint: 'Размер пакета при декодировании объёма. Уменьшите при OOM' },
      faceCount: { label: 'Число граней', hint: 'Сколько граней оставить при децимации перед UV-развёрткой' },
      removeBackground: { label: 'Удалять фон', hint: 'Сначала rembg — модель формы сегментирует по альфа-каналу' },
      texture: { label: 'Генерировать текстуру', hint: 'Проход PBR, в 5–8 раз медленнее' },
      textureResolution: { label: 'Разрешение текстуры', hint: 'Разрешение диффузии для каждого ракурса' },
      bakeViews: {
        label: 'Ракурсы запекания',
        hint: '0 — сервер выбирает сам, по объёму видеопамяти. На 12 ГБ не ставьте 6',
      },
    },
    connection: {
      blurb:
        'Где работает модель. Эти параметры не входят в кэш результатов: тот же чекпоинт, доступный по другому URL, — это та же модель.',
      mode: { label: 'Режим', hint: 'server обращается к API-серверу; local импортирует модель прямо здесь' },
      serverUrl: {
        label: 'URL сервера',
        hint: 'Базовый URL API-сервера Hunyuan3D (run_server.bat по умолчанию слушает порт 8081)',
      },
      transport: {
        label: 'Транспорт',
        hint: 'tasks ставит в очередь и опрашивает; sync держит открытым один запрос; auto проверяет сервер и выбирает сам',
      },
      pollInterval: { label: 'Интервал опроса', hint: 'Пауза между проверками статуса задачи, в секундах' },
      imagePath: {
        label: 'Передавать путь к изображению',
        hint: 'auto передаёт путь, если сервер на этой машине, иначе base64',
      },
      apiKeyEnv: {
        label: 'Переменная с ключом API',
        hint: 'Имя переменной окружения, значение которой отправляется как bearer-токен; пусто — без ключа',
      },
      timeout: {
        label: 'Тайм-аут молчания',
        hint:
          'Сколько секунд сервер может молчать, прежде чем узел сдастся. Это не ограничение длительности ' +
          'генерации — каждый отвеченный опрос статуса сбрасывает отсчёт.',
      },
      check: 'Проверить сервер',
      checking: 'Проверка…',
      device: { label: 'Устройство', hint: 'Устройство torch для обоих пайплайнов' },
      subfolder: { label: 'Подпапка чекпоинта', hint: 'Например, hunyuan3d-dit-v2-1; пусто — по умолчанию' },
      mvSubfolder: { label: 'Подпапка многоракурсной модели', hint: 'Например, hunyuan3d-dit-v2-mv; пусто — по умолчанию' },
      texgenPath: { label: 'Чекпоинт текстур', hint: 'Пусто — используется чекпоинт формы' },
    },
    health: {
      unknownDevice: 'неизвестное устройство',
      bakeViews: {
        one: 'по умолчанию {count} ракурс запекания',
        few: 'по умолчанию {count} ракурса запекания',
        many: 'по умолчанию {count} ракурсов запекания',
        other: 'по умолчанию {count} ракурса запекания',
      },
      vram: 'VRAM: занято {used}/{total} ГБ — {server} ГБ у этого сервера, {others} ГБ у других процессов',
      texturesAvailable: {
        one: 'Текстуры доступны · в очереди {count} задание',
        few: 'Текстуры доступны · в очереди {count} задания',
        many: 'Текстуры доступны · в очереди {count} заданий',
        other: 'Текстуры доступны · в очереди {count} задания',
      },
      texturesUnavailable: 'Текстуры недоступны: {reason}',
      mvUnavailable: 'Многоракурсная модель недоступна: {reason}',
      mvAvailable: 'Многоракурсная модель доступна · запуски по одному изображению используют {model}',
      onCard: '{model} на видеокарте',
      noReason: 'причина не указана',
    },
    environment: {
      title: 'Изолированное окружение',
      blurb:
        'Узлы генерации выполняются в отдельном процессе. Без venv они используют интерпретатор Studio — это ' +
        'работает, но без numpy. Hunyuan3D в серверном режиме не требует ничего устанавливать здесь; для локального ' +
        'режима сначала нужно добавить torch и пакеты модели в requirements.txt — см. README плагина.',
      status: 'Состояние: {state}',
      unknown: 'неизвестно',
      provisioned: 'собрано',
      notBuilt: 'не собрано',
      build: 'Собрать окружение',
      building: 'Сборка…',
      provisioning: 'Подготовка окружения плагина через uv…',
      done: 'Готово.',
    },
  },
}

/**
 * A message with markup inside it, as render children: each `{name}` becomes `slots[name]`
 * and the text around it stays text — what the app's `I18nT` does, for a render function.
 */
const rich = (text, slots) =>
  text
    .split(/\{(\w+)\}/)
    .map((part, index) =>
      index % 2 ? (Object.prototype.hasOwnProperty.call(slots, part) ? slots[part] : `{${part}}`) : part,
    )
    .filter((part) => part !== '')

const FORMATS = ['stl', 'glb', 'obj']
const MODES = ['server', 'local']
const DEVICES = ['cuda', 'cpu', 'mps']
const TRANSPORTS = ['auto', 'tasks', 'sync']
const IMAGE_PATH_MODES = ['auto', 'always', 'never']
const TEXTURE_RESOLUTIONS = ['512', '768']

const DEFAULT_SERVER_URL = 'http://127.0.0.1:8081'

export default function ({ vue, api, settings, i18n }) {
  const { defineComponent, h, computed, onMounted, ref } = vue
  const t = i18n.translator(MESSAGES)

  return defineComponent({
    name: 'Hunyuan3dSettingsPage',
    setup() {
      // Node defaults (cache-visible).
      const resolution = ref(64)
      const modelPath = ref('tencent/Hunyuan3D-2.1')
      const mvModelPath = ref('tencent/Hunyuan3D-2mv')
      const steps = ref(30)
      const guidance = ref(5)
      const octree = ref(256)
      const numChunks = ref(8000)
      const texture = ref(false)
      const textureResolution = ref('512')
      const maxNumView = ref(0)
      const faceCount = ref(40000)
      const removeBackground = ref(true)
      const outputFormat = ref('stl')

      // Hunyuan3D transport.
      const mode = ref('server')
      const serverUrl = ref(DEFAULT_SERVER_URL)
      const apiKeyEnv = ref('')
      const timeout = ref(1800)
      const transport = ref('auto')
      const pollInterval = ref(2)
      const imagePathMode = ref('auto')
      const subfolder = ref('')
      const mvSubfolder = ref('')
      const texgenPath = ref('')
      const device = ref('cuda')

      const loading = ref(true)
      const saving = ref(false)
      /** When the last save landed — formatted while rendering, in the language on screen. */
      const savedAt = ref(null)
      const error = ref(null)

      // Settings are stored as one document and PUT replaces it wholesale, so anything a
      // future version of this page does not know about is carried through rather than
      // silently dropped.
      const stored = ref({})

      const plugin = ref(null)
      const building = ref(false)
      const buildLog = ref([])

      // The server's own /health, fetched straight from the browser — it sends
      // `Access-Control-Allow-Origin: *`, and the Studio has no business proxying a
      // machine the node scripts talk to directly.
      const checking = ref(false)
      const health = ref(null)
      const healthError = ref(null)

      async function refreshPlugin() {
        const all = await api.plugins()
        plugin.value = all.find((entry) => entry.id === PLUGIN_ID) ?? null
      }

      const adopt = (value, target, guard) => {
        if (guard(value)) target.value = value
      }
      const isNumber = (value) => Number.isFinite(value)
      const oneOf = (options) => (value) => typeof value === 'string' && options.includes(value)
      const isText = (value) => typeof value === 'string'
      const isBool = (value) => typeof value === 'boolean'

      onMounted(async () => {
        try {
          const saved = await settings.get()
          stored.value = saved ?? {}

          adopt(saved.default_resolution, resolution, isNumber)
          adopt(saved.default_model_path, modelPath, isText)
          adopt(saved.default_mv_model_path, mvModelPath, isText)
          adopt(saved.default_steps, steps, isNumber)
          adopt(saved.default_guidance_scale, guidance, isNumber)
          adopt(saved.default_octree_resolution, octree, isNumber)
          adopt(saved.default_num_chunks, numChunks, isNumber)
          adopt(saved.default_texture, texture, isBool)
          adopt(String(saved.default_texture_resolution ?? ''), textureResolution, oneOf(TEXTURE_RESOLUTIONS))
          adopt(saved.default_max_num_view, maxNumView, isNumber)
          // Falls back to where this setting lived before it became a node default.
          adopt(saved.default_face_count ?? saved.hunyuan_face_count, faceCount, isNumber)
          adopt(saved.default_remove_background ?? saved.hunyuan_remove_background, removeBackground, isBool)
          adopt(saved.default_output_format, outputFormat, oneOf(FORMATS))

          adopt(saved.hunyuan_mode, mode, oneOf(MODES))
          adopt(saved.hunyuan_server_url, serverUrl, isText)
          adopt(saved.hunyuan_api_key_env, apiKeyEnv, isText)
          adopt(saved.hunyuan_timeout, timeout, isNumber)
          adopt(saved.hunyuan_transport, transport, oneOf(TRANSPORTS))
          adopt(saved.hunyuan_poll_interval, pollInterval, isNumber)
          adopt(saved.hunyuan_image_path, imagePathMode, oneOf(IMAGE_PATH_MODES))
          adopt(saved.hunyuan_subfolder, subfolder, isText)
          adopt(saved.hunyuan_mv_subfolder, mvSubfolder, isText)
          adopt(saved.hunyuan_texgen_path, texgenPath, isText)
          adopt(saved.hunyuan_device, device, oneOf(DEVICES))

          await refreshPlugin()
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          loading.value = false
        }
      })

      async function save() {
        saving.value = true
        error.value = null
        try {
          await settings.save({
            ...stored.value,
            default_resolution: Number(resolution.value),
            default_model_path: String(modelPath.value).trim(),
            default_mv_model_path: String(mvModelPath.value).trim(),
            default_steps: Number(steps.value),
            default_guidance_scale: Number(guidance.value),
            default_octree_resolution: Number(octree.value),
            default_num_chunks: Number(numChunks.value),
            default_texture: Boolean(texture.value),
            default_texture_resolution: String(textureResolution.value),
            default_max_num_view: Number(maxNumView.value),
            default_face_count: Number(faceCount.value),
            default_remove_background: Boolean(removeBackground.value),
            default_output_format: outputFormat.value,

            hunyuan_mode: mode.value,
            hunyuan_server_url: String(serverUrl.value).trim(),
            hunyuan_api_key_env: String(apiKeyEnv.value).trim(),
            hunyuan_timeout: Number(timeout.value),
            hunyuan_transport: transport.value,
            hunyuan_poll_interval: Number(pollInterval.value),
            hunyuan_image_path: imagePathMode.value,
            hunyuan_subfolder: String(subfolder.value).trim(),
            hunyuan_mv_subfolder: String(mvSubfolder.value).trim(),
            hunyuan_texgen_path: String(texgenPath.value).trim(),
            hunyuan_device: device.value,
          })
          savedAt.value = new Date()
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          saving.value = false
        }
      }

      async function checkServer() {
        checking.value = true
        health.value = null
        healthError.value = null
        try {
          const base = String(serverUrl.value).trim().replace(/\/+$/, '')
          const response = await fetch(`${base}/health`, { method: 'GET' })
          if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
          health.value = await response.json()
        } catch (cause) {
          healthError.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          checking.value = false
        }
      }

      const envLabel = computed(() => {
        if (!plugin.value) return t('environment.unknown')
        return plugin.value.has_venv ? t('environment.provisioned') : t('environment.notBuilt')
      })

      /** A figure in gigabytes, one decimal, in the language on screen (6.9 / 6,9). */
      const gb = (value) =>
        value.toLocaleString(i18n.locale.value, { minimumFractionDigits: 1, maximumFractionDigits: 1 })

      /**
       * What /health said, as the two lines that actually decide a run: whether texturing
       * is possible at all, and how much of the card somebody else is holding. The server's
       * own README makes that gap the first thing to look at when a bake crawls.
       */
      const healthLines = computed(() => {
        const data = health.value
        if (!data) return []
        const lines = []
        const gpu = `${data.gpu_name ?? data.device ?? t('health.unknownDevice')} · vram_mode ${data.vram_mode ?? '?'}`
        lines.push(
          data.default_max_num_view ? `${gpu} · ${t('health.bakeViews', { count: data.default_max_num_view })}` : gpu,
        )
        if (Number.isFinite(data.vram_used_gb) && Number.isFinite(data.vram_total_gb)) {
          const others = Math.max(0, (data.vram_used_gb ?? 0) - (data.torch_reserved_gb ?? 0))
          lines.push(
            t('health.vram', {
              used: gb(data.vram_used_gb),
              total: gb(data.vram_total_gb),
              server: gb(data.torch_reserved_gb ?? 0),
              others: gb(others),
            }),
          )
        }
        lines.push(
          data.texture_available
            ? t('health.texturesAvailable', { count: data.queue_length ?? 0 })
            : t('health.texturesUnavailable', { reason: data.texture_unavailable_reason ?? t('health.noReason') }),
        )
        // What the Multiview → Mesh node depends on, and the one thing /health says that
        // this page cannot infer: the multi-view checkpoint is a separate ~4.6 GB download,
        // so a server without it refuses that node with a 409 rather than falling back.
        if (data.mv_available === false) {
          lines.push(t('health.mvUnavailable', { reason: data.mv_unavailable_reason ?? t('health.noReason') }))
        } else if (data.mv_available === true) {
          const available = t('health.mvAvailable', { model: data.default_shape_model ?? 'single' })
          lines.push(
            data.shape_model_on_gpu
              ? `${available} · ${t('health.onCard', { model: data.shape_model_on_gpu })}`
              : available,
          )
        }
        return lines
      })

      // The app's own utility classes, so the page rethemes with everything else (§7).
      const field = (label, hint, control) =>
        h('label', { class: 'flex items-start gap-4 py-2' }, [
          h('div', { class: 'w-56 shrink-0' }, [
            h('div', { class: 'font-medium' }, label),
            h('div', { class: 'text-[11px] text-faint' }, hint),
          ]),
          control,
        ])

      const controlClass =
        'rounded-md border border-line bg-inset px-2 py-1 font-mono ' +
        'focus:border-accent focus:outline-none'

      const number = (model, min, max, step) =>
        h('input', {
          type: 'number',
          min,
          max,
          step,
          value: model.value,
          onInput: (event) => (model.value = event.target.value),
          class: `w-24 ${controlClass}`,
        })

      const text = (model, placeholder) =>
        h('input', {
          type: 'text',
          placeholder,
          value: model.value,
          onInput: (event) => (model.value = event.target.value),
          class: `w-full max-w-md ${controlClass}`,
        })

      const select = (model, options) =>
        h(
          'select',
          {
            value: model.value,
            onChange: (event) => (model.value = event.target.value),
            class: `w-40 ${controlClass}`,
          },
          options.map((option) => h('option', { value: option }, option)),
        )

      const checkbox = (model) =>
        h('input', {
          type: 'checkbox',
          checked: model.value,
          onChange: (event) => (model.value = event.target.checked),
          class: 'mt-1 size-4 accent-[var(--vxs-accent)]',
        })

      const section = (title, blurb, children) =>
        h('section', { class: 'mb-6 max-w-2xl rounded-lg border border-line bg-surface p-4' }, [
          h('h2', { class: 'mb-2 font-medium' }, title),
          h('p', { class: 'mb-3 text-[11px] text-faint' }, blurb),
          ...children,
        ])

      const saveRow = () =>
        h('div', { class: 'mt-3 flex items-center gap-3' }, [
          h(
            'button',
            {
              class: 'vxs-btn-primary px-3 py-1.5 disabled:opacity-50',
              disabled: saving.value,
              onClick: save,
            },
            saving.value ? t('saving') : t('save'),
          ),
          savedAt.value
            ? h(
                'span',
                { class: 'text-[11px] text-faint' },
                t('savedAt', { time: savedAt.value.toLocaleTimeString(i18n.locale.value) }),
              )
            : null,
        ])

      return () =>
        h('div', { class: 'h-full overflow-auto p-pane' }, [
          h('header', { class: 'mb-5' }, [
            h('h1', { class: 'text-base font-semibold' }, t('title')),
            h('p', { class: 'text-muted' }, t('intro')),
          ]),

          error.value
            ? h('p', { class: 'mb-4 rounded-md border border-line px-3 py-2 text-danger' }, error.value)
            : null,

          loading.value
            ? h('p', { class: 'text-muted' }, t('loading'))
            : h('div', {}, [
                section(
                  t('defaults.title'),
                  t('defaults.blurb'),
                  [
                    field(
                      t('defaults.outputFormat.label'),
                      t('defaults.outputFormat.hint'),
                      select(outputFormat, FORMATS),
                    ),
                    field(
                      t('defaults.resolution.label'),
                      t('defaults.resolution.hint'),
                      number(resolution, 8, 200, 8),
                    ),
                    field(
                      t('defaults.checkpoint.label'),
                      t('defaults.checkpoint.hint'),
                      text(modelPath, 'tencent/Hunyuan3D-2.1'),
                    ),
                    field(
                      t('defaults.mvCheckpoint.label'),
                      t('defaults.mvCheckpoint.hint'),
                      text(mvModelPath, 'tencent/Hunyuan3D-2mv'),
                    ),
                    field(t('defaults.steps.label'), t('defaults.steps.hint'), number(steps, 1, 100, 1)),
                    field(t('defaults.guidance.label'), t('defaults.guidance.hint'), number(guidance, 0, 20, 0.5)),
                    field(
                      t('defaults.octree.label'),
                      t('defaults.octree.hint'),
                      number(octree, 64, 512, 64),
                    ),
                    field(
                      t('defaults.numChunks.label'),
                      t('defaults.numChunks.hint'),
                      number(numChunks, 1000, 200000, 1000),
                    ),
                    field(
                      t('defaults.faceCount.label'),
                      t('defaults.faceCount.hint'),
                      number(faceCount, 1000, 500000, 1000),
                    ),
                    field(
                      t('defaults.removeBackground.label'),
                      t('defaults.removeBackground.hint'),
                      checkbox(removeBackground),
                    ),
                    field(t('defaults.texture.label'), t('defaults.texture.hint'), checkbox(texture)),
                    field(
                      t('defaults.textureResolution.label'),
                      t('defaults.textureResolution.hint'),
                      select(textureResolution, TEXTURE_RESOLUTIONS),
                    ),
                    field(
                      t('defaults.bakeViews.label'),
                      t('defaults.bakeViews.hint'),
                      number(maxNumView, 0, 9, 1),
                    ),
                    saveRow(),
                  ],
                ),

                section(
                  'Hunyuan3D',
                  t('connection.blurb'),
                  [
                    field(t('connection.mode.label'), t('connection.mode.hint'), select(mode, MODES)),

                    mode.value === 'server'
                      ? h('div', {}, [
                          field(
                            t('connection.serverUrl.label'),
                            t('connection.serverUrl.hint'),
                            text(serverUrl, DEFAULT_SERVER_URL),
                          ),
                          field(
                            t('connection.transport.label'),
                            t('connection.transport.hint'),
                            select(transport, TRANSPORTS),
                          ),
                          field(
                            t('connection.pollInterval.label'),
                            t('connection.pollInterval.hint'),
                            number(pollInterval, 0.5, 60, 0.5),
                          ),
                          field(
                            t('connection.imagePath.label'),
                            t('connection.imagePath.hint'),
                            select(imagePathMode, IMAGE_PATH_MODES),
                          ),
                          field(
                            t('connection.apiKeyEnv.label'),
                            t('connection.apiKeyEnv.hint'),
                            text(apiKeyEnv, 'HUNYUAN3D_API_KEY'),
                          ),
                          field(
                            t('connection.timeout.label'),
                            t('connection.timeout.hint'),
                            number(timeout, 30, 7200, 30),
                          ),

                          h('div', { class: 'mt-3 flex items-center gap-3' }, [
                            h(
                              'button',
                              {
                                class:
                                  'rounded-md border border-line px-3 py-1.5 hover:bg-hover ' +
                                  'disabled:opacity-50',
                                disabled: checking.value,
                                onClick: checkServer,
                              },
                              checking.value ? t('connection.checking') : t('connection.check'),
                            ),
                            healthError.value
                              ? h('span', { class: 'text-[11px] text-danger' }, healthError.value)
                              : null,
                          ]),
                          healthLines.value.length
                            ? h(
                                'pre',
                                {
                                  class:
                                    'mt-2 overflow-auto rounded-md bg-inset p-3 font-mono ' +
                                    'text-[11px] whitespace-pre-wrap',
                                },
                                healthLines.value.join('\n'),
                              )
                            : null,
                        ])
                      : h('div', {}, [
                          field(t('connection.device.label'), t('connection.device.hint'), select(device, DEVICES)),
                          field(
                            t('connection.subfolder.label'),
                            t('connection.subfolder.hint'),
                            text(subfolder, ''),
                          ),
                          field(
                            t('connection.mvSubfolder.label'),
                            t('connection.mvSubfolder.hint'),
                            text(mvSubfolder, ''),
                          ),
                          field(
                            t('connection.texgenPath.label'),
                            t('connection.texgenPath.hint'),
                            text(texgenPath, ''),
                          ),
                        ]),

                    saveRow(),
                  ],
                ),
              ]),

          h('section', { class: 'max-w-2xl rounded-lg border border-line bg-surface p-4' }, [
            h('h2', { class: 'mb-2 font-medium' }, t('environment.title')),
            h('p', { class: 'mb-3 text-[11px] text-faint' }, t('environment.blurb')),
            h(
              'p',
              { class: 'mb-3' },
              rich(t('environment.status'), { state: h('span', { class: 'font-mono' }, envLabel.value) }),
            ),
            h(
              'button',
              {
                class:
                  'rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50',
                disabled: building.value,
                onClick: buildEnv,
              },
              building.value ? t('environment.building') : t('environment.build'),
            ),
            buildLog.value.length
              ? h(
                  'pre',
                  {
                    class:
                      'mt-3 max-h-64 overflow-auto rounded-md bg-inset p-3 font-mono ' +
                      'text-[11px] whitespace-pre-wrap',
                  },
                  buildLog.value.join('\n'),
                )
              : null,
          ]),
        ])

      async function buildEnv() {
        building.value = true
        error.value = null
        buildLog.value = [t('environment.provisioning')]
        try {
          const result = await api.installPluginEnv(PLUGIN_ID)
          buildLog.value = result.log?.length ? result.log : [result.detail ?? t('environment.done')]
          await refreshPlugin()
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          building.value = false
        }
      }
    },
  })
}
