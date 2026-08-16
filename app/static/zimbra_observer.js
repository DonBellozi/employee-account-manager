(() => {
  const path = window.location.pathname;

  function makeSettingsCard(href, marker, titleText, descriptionText) {
    const grid = document.querySelector('.settings-web-grid');
    if (!grid || grid.querySelector(`[${marker}]`)) return;

    const link = document.createElement('a');
    link.className = 'settings-card-link';
    link.href = href;
    link.setAttribute(marker, 'true');

    const title = document.createElement('span');
    title.className = 'settings-card-title';
    title.textContent = titleText;

    const description = document.createElement('span');
    description.className = 'settings-card-description';
    description.textContent = descriptionText;

    const action = document.createElement('span');
    action.className = 'settings-card-action';
    action.textContent = 'Открыть →';

    link.append(title, description, action);
    grid.append(link);
  }

  if (path === '/settings') {
    makeSettingsCard(
      '/settings/zimbra-observer',
      'data-settings-zimbra-observer',
      'Наблюдение Zimbra',
      'Неактивность и сроки хранения. Без изменений учетных записей.'
    );
    makeSettingsCard(
      '/settings/zimbra-lifecycle',
      'data-settings-zimbra-lifecycle',
      'Жизненный цикл Zimbra',
      'План и этапы закрытия, backup и удаления.'
    );
  }
})();
