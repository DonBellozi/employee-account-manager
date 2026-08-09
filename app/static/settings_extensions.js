(() => {
  if (window.location.pathname !== '/settings') return;

  const grid = document.querySelector('.settings-web-grid');
  if (!grid) return;

  const addCard = ({key, href, titleText, descriptionText}) => {
    if (grid.querySelector(`[data-settings-${key}]`)) return;

    const link = document.createElement('a');
    link.className = 'settings-card-link';
    link.href = href;
    link.setAttribute(`data-settings-${key}`, 'true');

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
  };

  addCard({
    key: 'telegram',
    href: '/settings/telegram',
    titleText: 'Telegram',
    descriptionText: 'Служебные отчеты, бот, чат и очередь доставки.',
  });

  addCard({
    key: 'zimbra-protection',
    href: '/settings/zimbra-protection',
    titleText: 'Защищенные учетные записи Zimbra',
    descriptionText: 'Исключения из закрытия, архивации и удаления.',
  });

  addCard({
    key: 'onec-sources',
    href: '/settings/onec-sources',
    titleText: 'Источники 1С',
    descriptionText: 'Организации, папки, отправители, домены и имена вложений.',
  });

  const onecCard = document.getElementById('onec-card');
  if (onecCard) {
    const moved = new Set([
      'Папка',
      'Фильтр отправителя',
      'Домен выгрузки 1С',
      'Имя вложения',
    ]);
    onecCard.querySelectorAll('dt').forEach(dt => {
      if (!moved.has((dt.textContent || '').trim())) return;
      const dd = dt.nextElementSibling;
      if (dd && dd.tagName === 'DD') dd.remove();
      dt.remove();
    });
  }
})();
