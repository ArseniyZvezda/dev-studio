/** PDF preview through the file-preview extension point (dev spec §6.2).
 * Fetch through the host so token mode works; the native viewer only sees a local
 * PDF Blob, never credentials or a raw endpoint with attachment disposition.
 */
export const MESSAGES = {
  en: {
    loading: 'Loading PDF…',
    failed: 'Could not load PDF: {reason}',
    unsupported: 'This browser cannot display PDFs inline. Download the file to open it.',
    download: 'Download PDF',
    title: 'PDF preview: {name}',
  },
  ru: {
    loading: 'Загрузка PDF…',
    failed: 'Не удалось загрузить PDF: {reason}',
    unsupported: 'Этот браузер не поддерживает просмотр PDF. Скачайте файл, чтобы открыть его.',
    download: 'Скачать PDF',
    title: 'Просмотр PDF: {name}',
  },
}

export default function (runtime) {
  const { defineComponent, h, onMounted, onBeforeUnmount, ref } = runtime.vue
  const t = runtime.i18n.translator(MESSAGES)
  return defineComponent({
    props: {
      path: { type: String, required: true },
      preview: { type: Object, default: null },
    },
    setup(props) {
      const url = ref('')
      const error = ref('')
      const supported = navigator.pdfViewerEnabled !== false
      let disposed = false
      onMounted(async () => {
        try {
          const bytes = await runtime.rawFileBytes(props.path)
          // Switching files while a request is in flight must not retain its Blob.
          if (disposed) return
          url.value = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }))
        } catch (cause) {
          if (!disposed) error.value = cause instanceof Error ? cause.message : String(cause)
        }
      })
      onBeforeUnmount(() => {
        disposed = true
        if (url.value) URL.revokeObjectURL(url.value)
      })
      return () => {
        const name = props.path.split('/').pop()
        const note = error.value
          ? t('failed', { reason: error.value })
          : !url.value ? t('loading') : t('unsupported')
        return h('div', {
          style: { height: '100%', display: 'flex', flexDirection: 'column', minHeight: '0' },
        }, [
          url.value && supported
            ? h('iframe', {
                src: url.value,
                title: t('title', { name }),
                style: { width: '100%', flex: '1', minHeight: '0', border: '0' },
              })
            : h('p', { role: error.value ? 'alert' : 'status', class: 'p-pane text-muted' }, note),
          url.value ? h('a', {
            href: url.value,
            download: name,
            class: 'border-t border-line px-pane py-2 text-small underline',
          }, t('download')) : null,
        ])
      }
    },
  })
}
