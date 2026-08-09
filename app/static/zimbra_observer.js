(() => {
  const path = window.location.pathname;

  function addSettingsCard() {
    if (path !== '/settings') return;
    const grid = document.querySelector('.settings-web-grid');
    if (!grid || grid.querySelector('[data-settings-zimbra-observer]')) return;

    const link = document.createElement('a');
    link.className = 'settings-card-link';
    link.href = '/settings/zimbra-observer';
    link.dataset.settingsZimbraObserver = 'true';

    const title = document.createElement('span');
    title.className = 'settings-card-title';
    title.textContent = 'Наблюдение Zimbra';

    const description = document.createElement('span');
    description.className = 'settings-card-description';
    description.textContent = 'Неактивность, сроки хранения и защита действующих работников по 1С. Только наблюдение.';

    const action = document.createElement('span');
    action.className = 'settings-card-action';
    action.textContent = 'Открыть →';

    link.append(title, description, action);
    grid.append(link);
  }

  addSettingsCard();
})();
