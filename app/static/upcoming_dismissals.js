(() => {
  const host = document.getElementById('upcoming-dismissals-dynamic');
  if (!host) return;

  const fragmentUrl = host.dataset.fragmentUrl || '/dismissals/upcoming/fragment';
  const refreshIntervalMs = 30000;
  let requestNumber = 0;
  let timer = 0;

  async function refreshFragment(message = '', error = '') {
    const currentRequest = ++requestNumber;
    const url = new URL(fragmentUrl, window.location.origin);
    if (message) url.searchParams.set('message', message);
    if (error) url.searchParams.set('error', error);

    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'text/html'},
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const html = await response.text();
      if (currentRequest !== requestNumber) return;
      host.innerHTML = html;
    } catch (_) {
      // Сохраняем текущий список: временная ошибка фонового обновления
      // не должна очищать уже показанные увольнения.
    }
  }

  function scheduleRefresh() {
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      if (document.visibilityState === 'visible') {
        await refreshFragment();
      }
      scheduleRefresh();
    }, refreshIntervalMs);
  }

  host.addEventListener('submit', async event => {
    const form = event.target.closest('form[data-upcoming-dismissal-defer]');
    if (!form) return;

    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const originalText = button?.textContent || '';

    if (button) {
      button.disabled = true;
      button.textContent = 'Сохраняю...';
    }

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        body: new FormData(form),
        headers: {'Accept': 'application/json'},
      });
      const payload = await response.json().catch(() => ({}));

      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }

      await refreshFragment(payload.message || '');
    } catch (exc) {
      await refreshFragment('', exc?.message || 'Не удалось сохранить отсрочку');
    } finally {
      if (button?.isConnected) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      refreshFragment();
    }
  });

  scheduleRefresh();
})();
