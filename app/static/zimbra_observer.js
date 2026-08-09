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

  function td(text, className = '') {
    const cell = document.createElement('td');
    if (className) cell.className = className;
    cell.textContent = text || '–';
    return cell;
  }

  function recommendationClass(value) {
    if (value === 'protected_hr' || value === 'none') return 'success';
    if (value === 'close' || value === 'archive_delete') return 'running';
    if (value === 'manual_review' || value === 'missing') return 'failed';
    return 'running';
  }

  async function addDashboardJournal() {
    if (path !== '/') return;
    const journal = document.querySelector('.journal-card');
    if (!journal || document.querySelector('[data-zimbra-observer-journal]')) return;

    const section = document.createElement('section');
    section.className = 'card table-wrap zimbra-observer-journal-card';
    section.dataset.zimbraObserverJournal = 'true';

    const heading = document.createElement('div');
    heading.className = 'blocking-results-heading';
    const headingText = document.createElement('div');
    const h2 = document.createElement('h2');
    h2.textContent = 'Наблюдение Zimbra';
    const intro = document.createElement('p');
    intro.className = 'muted';
    intro.textContent = 'Только рекомендации. Никаких изменений в Zimbra/AD, удаления или Telegram этот модуль не выполняет.';
    headingText.append(h2, intro);
    heading.append(headingText);
    section.append(heading);
    journal.parentNode.insertBefore(section, journal);

    try {
      const response = await fetch('/zimbra-observer/journal?limit=30', {
        headers: {'Accept': 'application/json'},
        cache: 'no-store',
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || 'Ошибка загрузки');

      if (payload.latest_run) {
        const summary = document.createElement('div');
        summary.className = 'observer-dashboard-summary';
        const parts = [
          `Последняя проверка: ${payload.latest_run.completed_at || payload.latest_run.started_at || '–'}`,
          `закрыть: ${payload.latest_run.close_candidates || 0}`,
          `backup + удалить: ${payload.latest_run.archive_candidates || 0}`,
          `защищены 1С: ${payload.latest_run.protected_by_hr || 0}`,
          `проверить: ${payload.latest_run.manual_review || 0}`,
        ];
        if (payload.latest_run.error) parts.push(`предупреждение: ${payload.latest_run.error}`);
        summary.textContent = parts.join(' · ');
        section.append(summary);
      }

      if (!payload.events || payload.events.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'upcoming-empty';
        empty.textContent = payload.latest_run
          ? 'Новых изменений вывода наблюдателя пока нет.'
          : 'Проверок Zimbra пока не было.';
        section.append(empty);
        return;
      }

      const table = document.createElement('table');
      table.className = 'journal-table observer-dashboard-table';
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      ['Дата', 'Рекомендация', 'Объект', 'Статус', 'Оператор', 'Подробности'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.append(th);
      });
      thead.append(headRow);
      table.append(thead);

      const tbody = document.createElement('tbody');
      payload.events.forEach((item) => {
        const row = document.createElement('tr');
        row.append(td(item.created_at, 'journal-date'));

        const actionCell = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `action-badge ${item.recommendation === 'protected_hr' ? 'provision' : 'blocking'}`;
        badge.textContent = item.recommendation_label || item.recommendation;
        actionCell.append(badge);
        row.append(actionCell);

        const objectCell = document.createElement('td');
        const strong = document.createElement('strong');
        strong.textContent = item.email || '–';
        objectCell.append(strong);
        if (item.account_status) {
          const meta = document.createElement('div');
          meta.className = 'journal-object-meta';
          const code = document.createElement('code');
          code.textContent = `Zimbra: ${item.account_status}`;
          meta.append(code);
          objectCell.append(meta);
        }
        row.append(objectCell);

        const statusCell = document.createElement('td');
        const status = document.createElement('span');
        status.className = `status ${recommendationClass(item.recommendation)}`;
        status.textContent = 'Наблюдение';
        statusCell.append(status);
        row.append(statusCell);

        row.append(td('Система'));
        row.append(td(item.reason, 'observer-journal-reason'));
        tbody.append(row);
      });
      table.append(tbody);
      section.append(table);
    } catch (error) {
      const alert = document.createElement('div');
      alert.className = 'alert error';
      alert.textContent = `Не удалось загрузить журнал наблюдения Zimbra: ${error.message || String(error)}`;
      section.append(alert);
    }
  }

  addSettingsCard();
  addDashboardJournal();
})();
