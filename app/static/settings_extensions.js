(() => {
  if (window.location.pathname !== '/settings') return;

  const grid = document.querySelector('.settings-web-grid');
  if (!grid || grid.querySelector('[data-settings-telegram]')) return;

  const link = document.createElement('a');
  link.className = 'settings-card-link';
  link.href = '/settings/telegram';
  link.dataset.settingsTelegram = 'true';

  const title = document.createElement('span');
  title.className = 'settings-card-title';
  title.textContent = 'Telegram';

  const description = document.createElement('span');
  description.className = 'settings-card-description';
  description.textContent = 'Служебные отчеты, бот, чат и очередь доставки.';

  const action = document.createElement('span');
  action.className = 'settings-card-action';
  action.textContent = 'Открыть →';

  link.append(title, description, action);
  grid.append(link);
})();
