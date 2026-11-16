'use strict';

(() => {
  // Reload creates a different sentinel/timeOrigin; executing twice in one document never reseeds it.
  if (window.spikeSnapshot) return;
  window.spikeBootstrap = (window.spikeBootstrap || 0) + 1;
  window.spikeSentinel = crypto.randomUUID();
  const timeOrigin = performance.timeOrigin;
  const draft = document.querySelector('#draft');
  const input = document.querySelector('#file');
  const selectionNode = document.querySelector('#selection').firstChild;
  const retainedNode = draft;
  const retainedFile = new File([`live file ${window.spikeSentinel}\n`], 'unsaved-fixture.txt', { type: 'text/plain', lastModified: 1700000000000 });
  const transfer = new DataTransfer();
  transfer.items.add(retainedFile);
  input.files = transfer.files;
  draft.value = `Unsaved draft ${window.spikeSentinel}\n` + Array.from({ length: 40 }, (_, index) => `Retained line ${index + 1}`).join('\n');
  draft.focus();
  draft.setSelectionRange(8, 25, 'forward');
  draft.scrollTop = 48;
  const range = document.createRange();
  range.setStart(selectionNode, 0);
  range.setEnd(selectionNode, 15);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  // Keep a non-collapsed control selection in addition to the document range.
  draft.setSelectionRange(8, 25, 'forward');
  document.querySelector('#sentinel').textContent = window.spikeSentinel;

  window.spikeSnapshot = async () => {
    const currentDraft = document.querySelector('#draft');
    const file = document.querySelector('#file').files[0];
    const currentSelection = window.getSelection();
    const result = {
      sentinel: window.spikeSentinel,
      timeOrigin,
      bootstrap: window.spikeBootstrap,
      nodeSame: currentDraft === retainedNode,
      fileSame: file === retainedFile,
      fileName: file?.name ?? null,
      fileSize: file?.size ?? null,
      draft: currentDraft.value,
      selectionStart: currentDraft.selectionStart,
      selectionEnd: currentDraft.selectionEnd,
      scrollTop: currentDraft.scrollTop,
      activeElement: document.activeElement?.id || document.activeElement?.tagName || null,
      documentFocused: document.hasFocus(),
      domSelection: {
        text: currentSelection.toString(),
        anchorNodeSame: currentSelection.anchorNode === selectionNode,
        focusNodeSame: currentSelection.focusNode === selectionNode,
        anchorOffset: currentSelection.anchorOffset,
        focusOffset: currentSelection.focusOffset,
        rangeCount: currentSelection.rangeCount,
      },
    };
    return { ...result, fileText: file ? await file.text() : null };
  };
  window.spike.send('bootstrap', { sentinel: window.spikeSentinel, timeOrigin, bootstrap: window.spikeBootstrap });
})();
