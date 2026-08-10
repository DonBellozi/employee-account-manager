(() => {
  const workspace = document.querySelector('[data-mail-template-workspace]');
  if (!workspace) return;

  const buttons = Array.from(workspace.querySelectorAll('[data-template-target]'));
  const panels = Array.from(workspace.querySelectorAll('[data-template-panel]'));
  if (!buttons.length || !panels.length) return;

  const activate = key => {
    let found = false;
    buttons.forEach(button => {
      const active = button.dataset.templateTarget === key;
      button.classList.toggle('active', active);
      if (active) found = true;
    });
    panels.forEach(panel => {
      panel.classList.toggle('active', panel.dataset.templatePanel === key);
    });
    return found;
  };

  buttons.forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      const key = button.dataset.templateTarget || '';
      if (!activate(key)) return;
      const url = new URL(window.location.href);
      url.searchParams.set('selected', key);
      url.searchParams.delete('message');
      url.searchParams.delete('error');
      window.history.replaceState({}, '', url);
    });
  });

  const selected = workspace.dataset.selectedTemplate || '';
  if (!activate(selected)) {
    activate(buttons[0].dataset.templateTarget || '');
  }
})();
