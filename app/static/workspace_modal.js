(() => {
  const dialog = document.getElementById('workspace-modal');
  const frame = document.getElementById('workspace-modal-frame');
  const title = document.getElementById('workspace-modal-title');
  const closeButton = document.getElementById('workspace-modal-close');

  if (!dialog || !frame || !title) return;

  let refreshOnClose = false;
  let changed = false;

  function modalUrl(href) {
    const url = new URL(href, window.location.origin);
    url.searchParams.set('modal', '1');
    return url.pathname + url.search + url.hash;
  }

  function openWorkspace(href, heading, shouldRefresh = false) {
    refreshOnClose = Boolean(shouldRefresh);
    changed = false;
    title.textContent = heading || 'Окно';
    frame.src = modalUrl(href);

    if (!dialog.open) {
      dialog.showModal();
    }
  }

  function closeWorkspace() {
    if (dialog.open) dialog.close();
  }

  document.addEventListener('click', event => {
    const trigger = event.target.closest('a[data-workspace-modal]');
    if (!trigger) return;

    const href = trigger.getAttribute('href');
    if (!href || href.startsWith('http')) return;

    event.preventDefault();
    openWorkspace(
      href,
      trigger.dataset.workspaceTitle || trigger.textContent.trim(),
      trigger.dataset.workspaceRefresh === 'true'
    );
  });

  closeButton?.addEventListener('click', closeWorkspace);

  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeWorkspace();
  });

  frame.addEventListener('load', () => {
    if (!frame.src || frame.src === 'about:blank') return;
    try {
      const childDocument = frame.contentDocument;
      if (!childDocument?.body) return;
      childDocument.body.classList.add('workspace-embedded');
    } catch (_) {
      // Same-origin pages are expected. Keep the modal usable if the browser
      // temporarily denies access while the iframe is navigating.
    }
  });

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin) return;
    if (event.data?.type !== 'workspace-modal-saved') return;
    changed = true;
    closeWorkspace();
  });

  dialog.addEventListener('close', () => {
    frame.src = 'about:blank';
    if (changed || refreshOnClose) {
      window.location.reload();
    }
  });
})();
