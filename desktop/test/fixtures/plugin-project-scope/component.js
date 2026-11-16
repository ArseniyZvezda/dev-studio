// Served as different project-owned native ES modules, NEVER injected as factories.
export const label = '__LABEL__';
export const kind = '__KIND__';
export const evaluation = crypto.randomUUID();
export const source = import.meta.url;
export default function(runtime) {
  const { h, shallowRef, onMounted, onBeforeUnmount } = runtime.vue;
  const nested = shallowRef(null);
  // Diagnostic control only: --precompute-urls removes guarded calls from render.
  // Strict/default mode exercises the supported runtime helper inside Vue render.
  const pixelUrl = runtime.assetUrl('pixel.svg');
  const rawUrl = runtime.rawFileUrl('same.fixture');
  const record = { label, kind, evaluation, source, runtime, nested, mounts: 0,
    open: async path => {
      const result = await runtime.filePreviewer(path);
      nested.value = result.component;
      return result.preview.path;
    },
    write: () => runtime.setNodeParams('same-node', { origin: label }),
    save: () => runtime.settings.save({ origin: label }),
    action: () => runtime.action('record', { origin: label }),
    raw: () => runtime.rawFileBytes('same.fixture'),
  };
  window.fixtureCaptures.push(record);
  runtime.onEvent('team.doc.changed', () => {
    if (kind === 'node') runtime.setNodeParams('same-node', { event: label });
  });
  return { name: 'Fixture' + kind, setup() {
    let css;
    onMounted(() => {
      record.mounts++;
      css = document.createElement('link');
      css.rel = 'stylesheet'; css.href = runtime.assetUrl('style.css');
      document.head.append(css);
    });
    onBeforeUnmount(() => css?.remove());
    return () => h('section', { class: 'fixture-' + kind, 'data-label': label,
      'data-evaluation': evaluation }, [
      h('strong', `${kind}:${label}`),
      h('img', { alt: label, src: window.fixturePrecompute ? pixelUrl : runtime.assetUrl('pixel.svg'), width: 24, height: 24 }),
      h('img', { alt: 'raw:' + label, src: window.fixturePrecompute ? rawUrl : runtime.rawFileUrl('same.fixture'), width: 24, height: 24 }),
      h('button', { class: 'write', onClick: record.write }, 'Write node'),
      nested.value ? h(nested.value, { path: 'same.fixture' }) : null,
    ]);
  } };
}
