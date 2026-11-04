/**
 * Unreal Engine settings — where the project and the editor executable live, and how the
 * nodes should reach the editor.
 *
 * Plain ES module loaded at runtime: `vue` and the API client come through the factory,
 * and it is one file because the host appends `?token=` to this URL (a relative import
 * from here would resolve without it). Styling goes through the `--vxs-*` properties
 * rather than utility classes, which is what a plugin outside the wheel would have to do.
 */

const PLUGIN_ID = 'unreal'

/**
 * The page's own words (§6.4), English first. A plugin cannot add keys to the Studio's locale
 * files, so they live here and `i18n.translator` follows the Studio's language. Unreal's own
 * names — its plugins, its settings, its menu paths — stay as the editor spells them, and so
 * do node descriptions, which come from the manifest.
 */
const MESSAGES = {
  en: {
    intro: 'Where the project lives and how pipeline nodes reach the editor.',
    loading: 'Loading…',
    fields: {
      project: { label: 'Project file', hint: 'Absolute path to your .uproject' },
      editorCmd: { label: 'Editor executable', hint: 'UnrealEditor-Cmd — only needed for headless runs' },
      commandHost: { label: 'Callback address', hint: 'Address the editor dials back on for remote execution' },
    },
    connection: {
      title: 'Connection',
      transport: 'Transport',
      transportHint: 'How the script reaches the editor',
      auto: 'auto — running editor, else headless',
      remote: 'remote — require a running editor',
      commandlet: 'commandlet — always headless',
      save: 'Save',
      saving: 'Saving…',
      savedAt: 'Saved at {time}',
      noProject: 'No project set — the nodes will fail until one is',
    },
    firstRun: {
      title: 'Before the first run',
      enablePlugin: 'Enable the {plugin} in your Unreal project.',
      remoteExecution:
        'For the {transport} transport, also tick {setting} under Project Settings → Plugins → Python. It is off by default.',
      formats:
        'Unreal imports FBX, OBJ, glTF, Alembic, USD, textures and audio — not the {vox}/{stl} the sample pipeline ' +
        'produces. Convert first.',
    },
    nodes: 'Nodes',
  },
  ru: {
    intro: 'Где находится проект и как узлы пайплайнов связываются с редактором.',
    loading: 'Загрузка…',
    fields: {
      project: { label: 'Файл проекта', hint: 'Абсолютный путь к файлу .uproject' },
      editorCmd: {
        label: 'Исполняемый файл редактора',
        hint: 'UnrealEditor-Cmd — нужен только для запусков без интерфейса',
      },
      commandHost: {
        label: 'Адрес обратного подключения',
        hint: 'Адрес, по которому редактор подключается обратно при удалённом выполнении',
      },
    },
    connection: {
      title: 'Подключение',
      transport: 'Транспорт',
      transportHint: 'Как скрипт связывается с редактором',
      auto: 'auto — запущенный редактор, иначе без интерфейса',
      remote: 'remote — только запущенный редактор',
      commandlet: 'commandlet — всегда без интерфейса',
      save: 'Сохранить',
      saving: 'Сохранение…',
      savedAt: 'Сохранено в {time}',
      noProject: 'Проект не задан — пока его нет, узлы будут завершаться с ошибкой',
    },
    firstRun: {
      title: 'Перед первым запуском',
      enablePlugin: 'Включите {plugin} в своём проекте Unreal.',
      remoteExecution:
        'Для транспорта {transport} также отметьте {setting} в Project Settings → Plugins → Python. По умолчанию эта ' +
        'опция выключена.',
      formats:
        'Unreal импортирует FBX, OBJ, glTF, Alembic, USD, текстуры и звук, но не {vox}/{stl}, которые создаёт ' +
        'пайплайн из примера. Сначала сконвертируйте их.',
    },
    nodes: 'Узлы',
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

// `label` and `hint` are message keys, resolved while rendering so they follow the language.
const FIELDS = [
  {
    key: 'project',
    label: 'fields.project.label',
    hint: 'fields.project.hint',
    placeholder: 'D:/Projects/MyGame/MyGame.uproject',
  },
  {
    key: 'editor_cmd',
    label: 'fields.editorCmd.label',
    hint: 'fields.editorCmd.hint',
    placeholder: 'C:/Program Files/Epic Games/UE_5.4/Engine/Binaries/Win64/UnrealEditor-Cmd.exe',
  },
  {
    key: 'command_host',
    label: 'fields.commandHost.label',
    hint: 'fields.commandHost.hint',
    placeholder: '127.0.0.1',
  },
]

export default function ({ vue, api, settings, i18n }) {
  const { defineComponent, h, computed, onMounted, ref } = vue
  const t = i18n.translator(MESSAGES)

  return defineComponent({
    name: 'UnrealSettingsPage',
    setup() {
      const values = ref({ project: '', editor_cmd: '', command_host: '', transport: 'auto' })
      const loading = ref(true)
      const saving = ref(false)
      /** When the last save landed — formatted while rendering, in the language on screen. */
      const savedAt = ref(null)
      const error = ref(null)
      const nodeTypes = ref([])

      onMounted(async () => {
        try {
          const stored = await settings.get()
          values.value = { ...values.value, ...stored }
          nodeTypes.value = (await api.nodeTypes()).node_types.filter(
            (schema) => schema.plugin_id === PLUGIN_ID,
          )
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
          const payload = {}
          for (const [key, value] of Object.entries(values.value)) {
            if (String(value ?? '').trim()) payload[key] = value
          }
          await settings.save(payload)
          savedAt.value = new Date()
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          saving.value = false
        }
      }

      const configured = computed(() => Boolean(String(values.value.project ?? '').trim()))

      const card = (title, children) =>
        h(
          'section',
          {
            style: {
              maxWidth: '46rem',
              marginBottom: 'var(--vxs-space-5)',
              padding: 'var(--vxs-space-4)',
              borderRadius: 'var(--vxs-radius-lg)',
              border: '1px solid var(--vxs-border)',
              background: 'var(--vxs-bg-surface)',
            },
          },
          [h('h2', { style: { fontWeight: 500, marginBottom: 'var(--vxs-space-3)' } }, title), ...children],
        )

      const input = (key, placeholder) =>
        h('input', {
          value: values.value[key] ?? '',
          placeholder,
          spellcheck: 'false',
          onInput: (event) => (values.value = { ...values.value, [key]: event.target.value }),
          style: {
            flex: '1',
            minWidth: '0',
            padding: '4px 8px',
            borderRadius: 'var(--vxs-radius-md)',
            border: '1px solid var(--vxs-border)',
            background: 'var(--vxs-bg-inset)',
            color: 'var(--vxs-text-primary)',
            fontFamily: 'var(--vxs-font-mono)',
            fontSize: '12px',
          },
        })

      const field = (spec) =>
        h('label', { style: { display: 'flex', gap: 'var(--vxs-space-4)', padding: '6px 0' } }, [
          h('div', { style: { width: '11rem', flexShrink: '0' } }, [
            h('div', t(spec.label)),
            h('div', { style: { fontSize: '11px', color: 'var(--vxs-text-faint)' } }, t(spec.hint)),
          ]),
          input(spec.key, spec.placeholder),
        ])

      const code = (text) => h('code', { style: { fontFamily: 'var(--vxs-font-mono)' } }, text)

      return () =>
        h('div', { style: { height: '100%', overflow: 'auto', padding: 'var(--vxs-pane-pad)' } }, [
          h('header', { style: { marginBottom: 'var(--vxs-space-5)' } }, [
            h('h1', { style: { fontSize: '15px', fontWeight: 600 } }, 'Unreal Engine'),
            h('p', { style: { color: 'var(--vxs-text-muted)' } }, t('intro')),
          ]),

          error.value
            ? h(
                'p',
                {
                  style: {
                    maxWidth: '46rem',
                    marginBottom: 'var(--vxs-space-4)',
                    padding: '8px 12px',
                    borderRadius: 'var(--vxs-radius-md)',
                    border: '1px solid var(--vxs-border)',
                    color: 'var(--vxs-danger)',
                  },
                },
                error.value,
              )
            : null,

          loading.value
            ? h('p', { style: { color: 'var(--vxs-text-muted)' } }, t('loading'))
            : card(t('connection.title'), [
                ...FIELDS.map(field),
                h('label', { style: { display: 'flex', gap: 'var(--vxs-space-4)', padding: '6px 0' } }, [
                  h('div', { style: { width: '11rem', flexShrink: '0' } }, [
                    h('div', t('connection.transport')),
                    h(
                      'div',
                      { style: { fontSize: '11px', color: 'var(--vxs-text-faint)' } },
                      t('connection.transportHint'),
                    ),
                  ]),
                  h(
                    'select',
                    {
                      value: values.value.transport ?? 'auto',
                      onChange: (event) =>
                        (values.value = { ...values.value, transport: event.target.value }),
                      style: {
                        padding: '4px 8px',
                        borderRadius: 'var(--vxs-radius-md)',
                        border: '1px solid var(--vxs-border)',
                        background: 'var(--vxs-bg-inset)',
                        color: 'var(--vxs-text-primary)',
                      },
                    },
                    [
                      h('option', { value: 'auto' }, t('connection.auto')),
                      h('option', { value: 'remote' }, t('connection.remote')),
                      h('option', { value: 'commandlet' }, t('connection.commandlet')),
                    ],
                  ),
                ]),
                h('div', { style: { display: 'flex', alignItems: 'center', gap: 'var(--vxs-space-3)', marginTop: 'var(--vxs-space-3)' } }, [
                  h(
                    'button',
                    {
                      disabled: saving.value,
                      onClick: save,
                      // The app's primary button (docs/plugins.md): its ground, edge and
                      // lettering are the host's, so only the size is said here.
                      class: 'vxs-btn-primary',
                      style: {
                        padding: '6px 12px',
                        cursor: saving.value ? 'default' : 'pointer',
                      },
                    },
                    saving.value ? t('connection.saving') : t('connection.save'),
                  ),
                  savedAt.value
                    ? h('span', { style: { fontSize: '11px', color: 'var(--vxs-text-faint)' } },
                        t('connection.savedAt', { time: savedAt.value.toLocaleTimeString(i18n.locale.value) }))
                    : null,
                  !configured.value
                    ? h('span', { style: { fontSize: '11px', color: 'var(--vxs-warning)' } },
                        t('connection.noProject'))
                    : null,
                ]),
              ]),

          card(t('firstRun.title'), [
            h(
              'ul',
              { style: { margin: 0, paddingLeft: '1.1rem', color: 'var(--vxs-text-muted)', lineHeight: 1.7 } },
              [
                h('li', rich(t('firstRun.enablePlugin'), { plugin: h('b', 'Python Editor Script Plugin') })),
                h(
                  'li',
                  rich(t('firstRun.remoteExecution'), {
                    transport: code('remote'),
                    setting: h('b', 'Enable Remote Execution'),
                  }),
                ),
                h('li', rich(t('firstRun.formats'), { vox: code('.vox'), stl: code('.stl') })),
              ],
            ),
          ]),

          nodeTypes.value.length
            ? card(
                t('nodes'),
                nodeTypes.value.map((schema) =>
                  h('p', { style: { margin: '4px 0' } }, [
                    h('code', { style: { fontFamily: 'var(--vxs-font-mono)' } }, schema.id),
                    h('span', { style: { color: 'var(--vxs-text-muted)' } }, ` — ${schema.description}`),
                  ]),
                ),
              )
            : null,
        ])
    },
  })
}
