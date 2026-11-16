// Imports are the current production sources, bundled by Vite without test aliases.
import { createApp, h, shallowRef, computed, nextTick } from 'vue';
import { createMemoryHistory, createRouter, RouterView } from 'vue-router';
import { setToken } from '@/api/auth';
import { ProjectSession } from '@/session/project-session';
import { createPluginHost, createRuntime, pluginResourceUrl } from '@/plugin-runtime';
import { pluginLoader } from '@/plugin-runtime/loader';
import { provideNodeParamWriter, captureNodeParamBinding } from '@/plugin-runtime/node-params';
import { highlightFile } from '@/lib/highlight/highlighter';
import { pluginGrammarGeneration } from '@/lib/highlight/grammars';

const id = location.pathname.split('/')[2];
window.fixtureDiagnostics = { consoleErrors: [], uncaught: [], rejections: [] };
const originalError = console.error;
console.error = (...args) => {
  window.fixtureDiagnostics.consoleErrors.push(args.map(arg => arg?.stack || String(arg)));
  originalError.apply(console, args);
};
window.addEventListener('error', event => window.fixtureDiagnostics.uncaught.push(event.error?.stack || event.message));
window.addEventListener('unhandledrejection', event => window.fixtureDiagnostics.rejections.push(event.reason?.stack || String(event.reason)));
setToken('fixture secret /&');
window.fixtureCaptures = [];
window.fixturePrecompute = new URL(location.href).searchParams.has('precompute');
const session = new ProjectSession(id);
session.bind({ project_id: id, epoch: 'epoch-' + id });
const router = createRouter({ history: createMemoryHistory('/p/' + id), routes: [
  { path: '/', component: { render: () => h('div', 'Fixture home') } },
  { path: '/pipeline', component: { render: () => h('div', 'Fixture pipeline consumer') } },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: { render: () => h('div', 'No plugin page') } },
] });
const host = createPluginHost(session, { router });
const loader = pluginLoader(host); // default production native importer
const stop = loader.start();
const writes = [];
let editor = 0;
const editorSignal = shallowRef(0);
let releaseWriter;
function replaceWriter() {
  releaseWriter?.();
  const owner = ++editor;
  releaseWriter = provideNodeParamWriter((node, patch) => writes.push({ editor: owner, node, patch }), session);
  editorSignal.value = owner;
  return owner;
}
replaceWriter();
const url = name => `/api/projects/${id}/plugins/same-plugin/assets/${name}.js?v=same-stamp&project_id=${id}&epoch=epoch-${id}`;
const node = computed(() => { void editorSignal.value; return loader.component(url('node'), 'same-plugin'); });
// Explorer consumers deliberately have no editor capability, even while a pipeline is mounted.
const preview = computed(() => loader.component(url('previewer'), 'same-plugin', 'Previewer', captureNodeParamBinding(session, false)));
const showConsumers = shallowRef(true);
const app = createApp({ render: () => h('main', [h(RouterView), showConsumers.value
  ? h('aside', [h(node.value), h(preview.value)]) : null]) }).use(router);
const initialization = router.push('/').then(() => {
  app.mount('#app');
  return loader.loadPages();
});
let seq = 0;
let pendingPreview;
const saved = new Map();
const latest = kind => [...window.fixtureCaptures].reverse().find(x => x.kind === kind);
window.harness = {
  initialization,
  async page() { await initialization; await router.push('/same-page'); await nextTick(); },
  async pipeline() { await router.push('/pipeline'); replaceWriter(); await nextTick(); },
  replaceWriter,
  async refresh() { await loader.loadPages(); await nextTick(); },
  beginLocalRefresh() { window.localRefresh = loader.loadPages().then(() => ({ ok: true }), error => ({ ok: false, error: error.message })); },
  finishLocalRefresh() { return window.localRefresh; },
  async event(type, data, foreign = false) {
    // Fixture envelope transport, NOT a native WebSocket handshake. The production
    // WsConnection parser/scope/replay dispatch and PluginLoader subscriptions run.
    host.events.dispatch(JSON.stringify({ type, data, seq: ++seq,
      project_id: foreign ? (id === 'A' ? 'B' : 'A') : id, epoch: 'epoch-' + (foreign ? (id === 'A' ? 'B' : 'A') : id) }));
    await nextTick();
  },
  async settled() { await loader.waitForRefresh(); await nextTick(); },
  async settledResult() {
    try { await loader.waitForRefresh(); await nextTick(); return { ok: true }; }
    catch (error) { return { ok: false, error: error.message }; }
  },
  save(key, kind) { saved.set(key, latest(kind)); return latest(kind)?.evaluation; },
  async retained(key, operation) {
    try { const value = await saved.get(key)[operation](); return { ok: true, value }; }
    catch (error) { return { ok: false, error: error.message }; }
  },
  async call(kind, operation, arg) { return latest(kind)[operation](arg); },
  async clickNode() { document.querySelector('.fixture-node .write').click(); await nextTick(); },
  beginPreview(kind, path) {
    pendingPreview = latest(kind).open(path).then(value => ({ ok: true, value }), error => ({ ok: false, error: error.message }));
  },
  finishPreview() { return pendingPreview; },
  async lateImport(name) {
    const component = loader.component(url(name), 'same-plugin');
    window.lateImport = component.__asyncLoader().then(() => ({ ok: true }), error => ({ ok: false, error: error.message }));
  },
  finishImport() { return window.lateImport; },
  async nativeProbe(source) {
    const one = await import(/* @vite-ignore */ source);
    const two = await import(/* @vite-ignore */ source);
    return { same: one === two, label: one.label, evaluation: one.evaluation };
  },
  async highlight() {
    const result = await highlightFile('same.fixture', 'ALPHA BETA', 'dark', session);
    document.querySelector('#highlight').replaceChildren(...(result?.lines.flat() ?? []).map(token => {
      const span = document.createElement('span'); span.textContent = token.text; span.style.color = token.color; return span;
    }));
    return result;
  },
  invalidUrls() {
    return ['/api/plugins/same-plugin/assets/page.js', url('page').replace(`/projects/${id}/`, '/projects/foreign/')].map(source => {
      try { pluginResourceUrl(source, session, 'same-plugin'); return false; } catch { return true; }
    });
  },
  snapshot() {
    const elements = [...document.querySelectorAll('[data-label]')].map(el => ({
      kind: el.className, label: el.dataset.label, evaluation: el.dataset.evaluation,
      color: getComputedStyle(el).color, background: getComputedStyle(el).backgroundColor,
      images: [...el.querySelectorAll('img')].map(img => {
        const loaded = img.complete && img.naturalWidth > 0;
        let pixel = null;
        if (loaded) {
          const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
          const context = canvas.getContext('2d'); context.drawImage(img, 0, 0, 1, 1);
          pixel = [...context.getImageData(0, 0, 1, 1).data];
        }
        return { url: img.src, loaded, pixel };
      }),
    }));
    return { id, elements, writes: [...writes], grammarGeneration: pluginGrammarGeneration(session),
      nav: loader.navItems.map(x => x.id), loading: loader.state.loading, error: loader.state.error,
      route: router.currentRoute.value.fullPath, captures: window.fixtureCaptures.map(x => ({ kind: x.kind, label: x.label,
        evaluation: x.evaluation, source: x.source, mounts: x.mounts })),
      highlighted: [...document.querySelectorAll('#highlight span')].map(el => ({ text: el.textContent, color: getComputedStyle(el).color })),
    };
  },
  cleanup() { app.unmount(); stop(); releaseWriter?.(); host.events.disconnect(); session.invalidate(); },
};
