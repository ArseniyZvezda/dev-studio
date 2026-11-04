/**
 * Plastic SCM settings — where the `cm` client and the workspace are, plus whether the
 * Studio currently recognises this project as a Plastic workspace at all.
 *
 * Saving re-runs plugin loading on the backend, which re-runs version control detection,
 * so the status block below is accurate again the moment a save returns.
 *
 * The fields here and the ones on the Studio's own Version control page write to two
 * different places — this plugin's settings, and `config.vcs.settings.plastic` — but the
 * backend merges them into one dict with the VCS page on top (`AppContext.vcs_settings`),
 * so there is exactly one answer to "where is cm". The Status card shows that answer, and
 * says so when the other page is the one supplying it; otherwise this form would look
 * inert while a path set elsewhere was in use.
 *
 * Plain ES module loaded at runtime: `vue` and the API client come through the factory,
 * and it is one file because the host appends `?token=` to this URL. Styling goes through
 * the `--vxs-*` properties rather than utility classes.
 */

const PLUGIN_ID = 'plastic'
const BACKEND_ID = 'plastic'

/**
 * The page's own words (§6.4), English first. A plugin cannot add keys to the Studio's locale
 * files, so they live here and `i18n.translator` follows the Studio's language. What the
 * backend reports — the client's reason, the detected system's name, node descriptions — is
 * shown as written.
 */
const MESSAGES = {
  en: {
    intro: 'Unity Version Control — where the client is, and which workspace pipelines check into.',
    loading: 'Loading…',
    fields: {
      executable: { label: 'cm executable', hint: 'Leave empty if `cm` is already on PATH' },
      workspace: { label: 'Workspace root', hint: 'Only if the workspace root is not the managed project' },
    },
    status: {
      title: 'Status',
      client: 'cm client',
      found: 'found',
      notFound: 'not found',
      resolvesTo: 'cm resolves to',
      overridden:
        'The Version control page also sets a path for cm, and that one wins — the field below stays as the ' +
        'fallback. Clear it there to use this one.',
      project: 'This project',
      under: 'under {name}',
      detectedAs: 'detected as {name}, not Plastic',
      none: 'no version control detected',
      notActive:
        'The checkin nodes still work on any workspace they can reach — detection only decides what the Studio ' +
        'reports and how team tasks are isolated.',
    },
    connection: {
      title: 'Connection',
      save: 'Save',
      saving: 'Saving…',
      savedAt: 'Saved at {time}',
    },
    checkin: {
      title: 'How the checkin node behaves',
      all:
        'It runs {command}. Plastic only checks in items that were checked out; a pipeline writes files straight ' +
        'to disk, so without --all a regenerated asset commits nothing.',
      untracked: 'Untracked (private) files are included by default — turn that off per node.',
      unchanged: 'An unchanged workspace passes through with committed = false rather than failing the run.',
      isolation:
        'Team task isolation stays on path locks here: a per-task Plastic workspace would mean a server round trip.',
    },
    nodes: 'Nodes',
  },
  ru: {
    intro: 'Unity Version Control — где находится клиент и в какое рабочее пространство пайплайны делают checkin.',
    loading: 'Загрузка…',
    fields: {
      executable: { label: 'Исполняемый файл cm', hint: 'Оставьте пустым, если `cm` уже есть в PATH' },
      workspace: {
        label: 'Корень рабочего пространства',
        hint: 'Только если корень рабочего пространства не совпадает с корнем проекта',
      },
    },
    status: {
      title: 'Состояние',
      client: 'Клиент cm',
      found: 'найден',
      notFound: 'не найден',
      resolvesTo: 'Итоговый путь к cm',
      overridden:
        'Путь к cm задан и на странице «Контроль версий», и действует именно он — поле ниже остаётся запасным. ' +
        'Очистите его там, чтобы использовался этот.',
      project: 'Этот проект',
      under: 'под управлением {name}',
      detectedAs: 'определён как {name}, а не Plastic',
      none: 'контроль версий не обнаружен',
      notActive:
        'Узлы checkin всё равно работают с любым доступным им рабочим пространством — обнаружение влияет только ' +
        'на то, что показывает Studio и как изолируются задачи команды.',
    },
    connection: {
      title: 'Подключение',
      save: 'Сохранить',
      saving: 'Сохранение…',
      savedAt: 'Сохранено в {time}',
    },
    checkin: {
      title: 'Как работает узел checkin',
      all:
        'Он выполняет {command}. Plastic делает checkin только для элементов, взятых на checkout; пайплайн пишет ' +
        'файлы прямо на диск, поэтому без --all перегенерированный ассет ничего не закоммитит.',
      untracked: 'Неотслеживаемые (private) файлы по умолчанию включаются — это отключается на каждом узле отдельно.',
      unchanged: 'Рабочее пространство без изменений не проваливает запуск, а проходит дальше с committed = false.',
      isolation:
        'Изоляция задач команды здесь остаётся на блокировках путей: отдельное рабочее пространство Plastic для ' +
        'каждой задачи означало бы обращение к серверу.',
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
    key: 'executable',
    label: 'fields.executable.label',
    hint: 'fields.executable.hint',
    placeholder: 'C:/Program Files/PlasticSCM5/client/cm.exe',
  },
  {
    key: 'workspace',
    label: 'fields.workspace.label',
    hint: 'fields.workspace.hint',
    placeholder: 'D:/Workspaces/MyGame',
  },
]

export default function ({ vue, api, settings, i18n }) {
  const { defineComponent, h, computed, onMounted, ref } = vue
  const t = i18n.translator(MESSAGES)

  return defineComponent({
    name: 'PlasticSettingsPage',
    setup() {
      const values = ref({ executable: '', workspace: '' })
      const loading = ref(true)
      const saving = ref(false)
      /** When the last save landed — formatted while rendering, in the language on screen. */
      const savedAt = ref(null)
      const error = ref(null)
      const detected = ref(null)
      const client = ref(null)
      const nodeTypes = ref([])
      const effective = ref('')
      const overrides = ref({})

      async function refresh() {
        const [project, status] = await Promise.all([api.project(), api.status()])
        detected.value = project.vcs ?? null
        client.value = (status.vcs_backends ?? {})[BACKEND_ID] ?? null
        overrides.value = project.config?.vcs?.settings?.[BACKEND_ID] ?? {}

        // What `cm` actually resolves to once both settings surfaces are merged. Guarded
        // rather than assumed: a host without the endpoint should cost this one row, not
        // the page.
        effective.value = ''
        try {
          if (typeof api.vcs === 'function') {
            const overview = await api.vcs()
            const option = (overview.backends ?? []).find((entry) => entry.id === BACKEND_ID)
            effective.value = option?.executable ?? ''
          }
        } catch {
          effective.value = ''
        }
      }

      onMounted(async () => {
        try {
          values.value = { ...values.value, ...(await settings.get()) }
          nodeTypes.value = (await api.nodeTypes()).node_types.filter(
            (schema) => schema.plugin_id === PLUGIN_ID,
          )
          await refresh()
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
          await refresh()
          savedAt.value = new Date()
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          saving.value = false
        }
      }

      const active = computed(() => detected.value?.id === BACKEND_ID)

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

      const row = (label, value, tone) =>
        h('div', { style: { display: 'flex', gap: 'var(--vxs-space-4)', padding: '3px 0' } }, [
          h('div', { style: { width: '11rem', flexShrink: '0', color: 'var(--vxs-text-muted)' } }, label),
          h('div', { style: { color: tone ?? 'var(--vxs-text-primary)' } }, value),
        ])

      return () =>
        h('div', { style: { height: '100%', overflow: 'auto', padding: 'var(--vxs-pane-pad)' } }, [
          h('header', { style: { marginBottom: 'var(--vxs-space-5)' } }, [
            h('h1', { style: { fontSize: '15px', fontWeight: 600 } }, 'Plastic SCM'),
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
            : card(t('status.title'), [
                row(
                  t('status.client'),
                  client.value?.available ? t('status.found') : client.value?.reason || t('status.notFound'),
                  client.value?.available ? 'var(--vxs-success)' : 'var(--vxs-warning)',
                ),
                effective.value ? row(t('status.resolvesTo'), effective.value) : null,
                overrides.value.executable
                  ? h(
                      'p',
                      { style: { marginTop: 'var(--vxs-space-2)', color: 'var(--vxs-text-faint)', fontSize: '11px' } },
                      t('status.overridden'),
                    )
                  : null,
                row(
                  t('status.project'),
                  active.value
                    ? t('status.under', { name: detected.value.name })
                    : detected.value
                      ? t('status.detectedAs', { name: detected.value.name })
                      : t('status.none'),
                  active.value ? 'var(--vxs-success)' : 'var(--vxs-text-muted)',
                ),
                !active.value
                  ? h(
                      'p',
                      { style: { marginTop: 'var(--vxs-space-3)', color: 'var(--vxs-text-faint)', fontSize: '11px' } },
                      t('status.notActive'),
                    )
                  : null,
              ]),

          loading.value
            ? null
            : card(t('connection.title'), [
                ...FIELDS.map(field),
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
                ]),
              ]),

          card(t('checkin.title'), [
            h(
              'ul',
              // The host's CSS reset clears list-style, and a plugin's inline styles are
              // all it can rely on — utility classes are not emitted for it (§7).
              { style: { margin: 0, paddingLeft: '1.1rem', listStyle: 'disc', color: 'var(--vxs-text-muted)', lineHeight: 1.7 } },
              [
                h(
                  'li',
                  rich(t('checkin.all'), {
                    command: h('code', { style: { fontFamily: 'var(--vxs-font-mono)' } }, 'cm checkin --all'),
                  }),
                ),
                h('li', t('checkin.untracked')),
                h('li', t('checkin.unchanged')),
                h('li', t('checkin.isolation')),
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
