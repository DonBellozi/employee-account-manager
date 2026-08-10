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

  const recipientStorageKey = 'mail-template-test-recipient';
  let savedRecipient = '';
  try {
    savedRecipient = window.localStorage.getItem(recipientStorageKey) || '';
  } catch (_) {
    savedRecipient = '';
  }

  const setResult = (node, state, message) => {
    if (!node) return;
    node.className = `mail-template-test-result ${state || ''}`.trim();
    node.textContent = message || '';
  };

  workspace.querySelectorAll('[data-mail-template-form]').forEach(form => {
    const recipient = form.querySelector('[data-test-recipient]');
    const testButton = form.querySelector('[data-mail-template-test]');
    const result = form.querySelector('[data-mail-template-test-result]');
    if (!recipient || !testButton || !result) return;

    if (savedRecipient && !recipient.value) {
      recipient.value = savedRecipient;
    }

    recipient.addEventListener('change', () => {
      const value = recipient.value.trim();
      if (!value) return;
      try {
        window.localStorage.setItem(recipientStorageKey, value);
      } catch (_) {
        // LocalStorage is only a convenience. Test sending works without it.
      }
    });

    testButton.addEventListener('click', async () => {
      const testUrl = form.dataset.testUrl || '';
      const target = recipient.value.trim();
      if (!testUrl) return;

      if (!target) {
        setResult(result, 'error', 'Укажите тестовый адрес.');
        recipient.focus();
        return;
      }

      const sender = form.querySelector('[name="sender_email"]');
      const subject = form.querySelector('[name="subject"]');
      const body = form.querySelector('[name="body_html"]');
      if (
        !sender?.checkValidity()
        || !subject?.checkValidity()
        || !body?.checkValidity()
        || !recipient.checkValidity()
      ) {
        form.reportValidity();
        return;
      }

      testButton.disabled = true;
      const originalText = testButton.textContent;
      testButton.textContent = 'Отправляю...';
      setResult(result, 'pending', 'Формирую и отправляю тестовое письмо...');

      try {
        const response = await fetch(testUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Accept': 'application/json'},
          body: new FormData(form),
        });
        const payload = await response.json();
        if (!response.ok || payload.ok === false) {
          throw new Error(payload.error || 'Не удалось отправить тестовое письмо');
        }

        try {
          window.localStorage.setItem(recipientStorageKey, target);
        } catch (_) {}

        setResult(
          result,
          payload.sent === false ? 'warning' : 'success',
          payload.message || 'Тестовое письмо отправлено.'
        );
      } catch (error) {
        setResult(
          result,
          'error',
          error?.message || 'Не удалось отправить тестовое письмо.'
        );
      } finally {
        testButton.disabled = false;
        testButton.textContent = originalText;
      }
    });
  });
})();
