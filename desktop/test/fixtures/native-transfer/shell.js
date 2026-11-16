'use strict';

(() => {
  const dictionary = {
    ru: { tab: 'Живой проект', detach: 'Отделить', return: 'Вернуть', snapshot: 'Снимок', 'fail-target': 'Ошибка цели', 'close-target': 'Закрыть цель', quit: 'Выход', window: 'Окно', dragging: 'Перенос', idle: 'Ожидание' },
    en: { tab: 'Live project', detach: 'Detach', return: 'Return', snapshot: 'Snapshot', 'fail-target': 'Fail target', 'close-target': 'Close target', quit: 'Quit', window: 'Window', dragging: 'Dragging', idle: 'Idle' },
  };
  const tab = document.querySelector('#tab');
  const status = document.querySelector('#status');
  let locale = 'ru';
  let state = {};
  let pointerId = null;

  function render() {
    const messages = dictionary[locale];
    document.documentElement.lang = locale;
    document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = messages[node.dataset.i18n]; });
    tab.hidden = state.windowId === undefined || state.owner !== state.windowId;
    document.querySelector('#window-label').textContent = `${messages.window}: ${state.windowId ?? '—'}`;
    status.textContent = `${state.dragging ? messages.dragging : messages.idle} · ${state.message ?? ''}`;
  }

  window.spike.onState((next) => {
    state = next;
    if (next.locale === 'en' || next.locale === 'ru') locale = next.locale;
    render();
  });
  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', (event) => {
      if (!event.isTrusted) return;
      window.spike.action(button.dataset.action).catch((error) => { status.textContent = String(error); });
    });
  });
  document.querySelectorAll('[data-locale]').forEach((button) => {
    button.addEventListener('click', (event) => {
      if (!event.isTrusted) return;
      locale = button.dataset.locale;
      render();
      window.spike.action('locale', { locale }).catch((error) => { status.textContent = String(error); });
    });
  });
  document.addEventListener('dragstart', (event) => event.preventDefault());
  tab.addEventListener('pointerdown', (event) => {
    if (!event.isTrusted || !event.isPrimary || event.button !== 0 || pointerId !== null || tab.hidden) return;
    event.preventDefault();
    tab.setPointerCapture(event.pointerId);
    pointerId = event.pointerId;
    window.spike.send('pointer-begin', { pointerId, screenX: event.screenX, screenY: event.screenY });
  });
  tab.addEventListener('pointermove', (event) => {
    if (!event.isTrusted || event.pointerId !== pointerId) return;
    window.spike.send('pointer-move', { pointerId });
  });
  function finish(event, name) {
    if (!event.isTrusted || event.pointerId !== pointerId) return;
    const released = pointerId;
    pointerId = null; // Normal release's lostpointercapture must not cancel a completed gesture.
    window.spike.send(name, { pointerId: released, screenX: event.screenX, screenY: event.screenY });
    if (tab.hasPointerCapture(released)) tab.releasePointerCapture(released);
  }
  tab.addEventListener('pointerup', (event) => finish(event, 'pointer-release'));
  tab.addEventListener('pointercancel', (event) => finish(event, 'pointer-cancel'));
  tab.addEventListener('lostpointercapture', (event) => finish(event, 'pointer-cancel'));
  document.addEventListener('keydown', (event) => {
    if (!event.isTrusted || event.key !== 'Escape') return;
    const cancelled = pointerId;
    pointerId = null;
    window.spike.send('pointer-cancel', { pointerId: cancelled, reason: 'escape' });
    if (cancelled !== null && tab.hasPointerCapture(cancelled)) tab.releasePointerCapture(cancelled);
  });
  render();
})();
