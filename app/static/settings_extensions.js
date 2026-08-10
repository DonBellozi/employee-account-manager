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
  if (!onecCard) return;

  // Автоимпорт теперь является круглосуточным read-only IMAP polling.
  // Старое ONEC_AUTO_IMPORT_TIME оставлено только для совместимости ENV.
  onecCard.querySelectorAll('dt').forEach(dt => {
    const label = (dt.textContent || '').trim();
    const dd = dt.nextElementSibling;
    if (!dd || dd.tagName !== 'DD') return;

    if (label === 'Автоматический импорт') {
      dt.textContent = 'Автоматический забор XLSX';
    } else if (label === 'Время автоимпорта') {
      dt.textContent = 'Проверка IMAP';
      dd.textContent = 'каждые 5 минут, круглосуточно';
    } else if (label === 'Catch-up после запуска') {
      dt.textContent = 'Проверка сразу после запуска';
    } else if (label === 'Следующий плановый запуск') {
      dt.textContent = 'Следующая проверка IMAP';
    }
  });

  const details = onecCard.querySelector('.integration-details');
  if (details && !details.querySelector('[data-onec-control-export]')) {
    const controlDt = document.createElement('dt');
    controlDt.textContent = 'Контрольная выгрузка';
    controlDt.setAttribute('data-onec-control-export', 'true');
    const controlDd = document.createElement('dd');
    controlDd.textContent = 'после 19:00; автоблокировка не ранее 19:10';
    details.append(controlDt, controlDd);
  }

  const safetyNote = onecCard.querySelector('.onec-safety-note');
  if (safetyNote) {
    safetyNote.textContent =
      'Почта читается через IMAP только в режиме read-only. Новый XLSX применяется ' +
      'только после проверки структуры и защитных проверок. Автоблокировка AD/Zimbra ' +
      'выполняется отдельным контуром не ранее 19:10 и только после контрольной ' +
      'выгрузки всех включенных источников после 19:00.';
  }

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

  const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

  const resultNode = document.getElementById('onec-result');

  let summaryHost = document.getElementById('onec-multisource-summary');
  if (!summaryHost) {
    summaryHost = document.createElement('div');
    summaryHost.id = 'onec-multisource-summary';
    summaryHost.className = 'onec-registry-summary';

    if (resultNode?.parentNode) {
      resultNode.parentNode.insertBefore(summaryHost, resultNode);
    } else {
      onecCard.append(summaryHost);
    }
  }

  const hideLegacySummaries = () => {
    onecCard.querySelectorAll('.onec-registry-summary').forEach(node => {
      if (node.id !== 'onec-multisource-summary') {
        node.hidden = true;
      }
    });

    resultNode?.querySelectorAll('.onec-report-section').forEach(section => {
      const heading = section.querySelector('strong');
      if (
        (heading?.textContent || '').trim()
        === 'Сверка 1С / AD / Zimbra'
      ) {
        section.hidden = true;
      }
    });
  };

  const renderSummary = payload => {
    const summary = payload.summary || {};
    const organizations = Array.isArray(payload.organizations)
      ? payload.organizations
      : [];

    const rows = organizations.map(item => `
      <tr>
        <td>
          <strong>${escapeHtml(item.source_name || item.source_id)}</strong>
          <div class="journal-object-meta">
            <code>${escapeHtml(item.source_id)}</code>
          </div>
        </td>
        <td>${item.total ?? 0}</td>
        <td>${item.ok ?? 0}</td>
        <td>${item.checked ?? 0}</td>
        <td>${item.issues ?? 0}</td>
        <td>${item.errors ?? 0}</td>
        <td>${item.not_checked ?? 0}</td>
      </tr>
    `).join('');

    summaryHost.hidden = false;
    summaryHost.innerHTML = `
      <div class="onec-report-heading">
        <strong>Сверка 1С / AD / Zimbra – все организации</strong>
        <span class="muted">
          Организаций: ${summary.organizations ?? organizations.length}
        </span>
      </div>

      <div class="onec-diff-grid">
        <div><span>В реестре</span><strong>${summary.total ?? 0}</strong></div>
        <div><span>Соответствует</span><strong>${summary.ok ?? 0}</strong></div>
        <div><span>Проверены</span><strong>${summary.checked ?? 0}</strong></div>
        <div><span>Требует проверки</span><strong>${summary.issues ?? 0}</strong></div>
        <div><span>Ошибки</span><strong>${summary.errors ?? 0}</strong></div>
        <div><span>Не проверено</span><strong>${summary.not_checked ?? 0}</strong></div>
      </div>

      <div class="table-wrap">
        <table class="journal-table">
          <thead>
            <tr>
              <th>Организация</th>
              <th>В реестре</th>
              <th>Соответствует</th>
              <th>Проверены</th>
              <th>Требует проверки</th>
              <th>Ошибки</th>
              <th>Не проверено</th>
            </tr>
          </thead>
          <tbody>
            ${rows || '<tr><td colspan="7" class="muted">Кадровых источников пока нет.</td></tr>'}
          </tbody>
        </table>
      </div>
    `;

    hideLegacySummaries();
  };

  let refreshTimer = 0;
  let requestNumber = 0;

  const refreshMultiSourceSummary = () => {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(async () => {
      const currentRequest = ++requestNumber;
      try {
        const response = await fetch(
          '/settings/onec-sources/summary',
          {
            method: 'GET',
            credentials: 'same-origin',
            cache: 'no-store',
            headers: {'Accept': 'application/json'},
          }
        );
        const payload = await response.json();
        if (
          currentRequest !== requestNumber
          || !response.ok
          || payload.ok === false
        ) {
          return;
        }
        renderSummary(payload);
      } catch (_) {
        // Старый блок оставляем как запасной вариант при локальной ошибке.
      }
    }, 80);
  };

  hideLegacySummaries();
  refreshMultiSourceSummary();

  if (resultNode) {
    const observer = new MutationObserver(() => {
      hideLegacySummaries();
      refreshMultiSourceSummary();
    });
    observer.observe(resultNode, {
      childList: true,
      subtree: true,
    });
  }
})();
