from pathlib import Path

from jinja2 import Environment


def test_scheduler_runs_autoexecutor_only_after_success():
    text = Path("app/services/zimbra_observer_scheduler.py").read_text(encoding="utf-8")
    assert 'if result.status == "success":' in text
    assert "execute_from_observation(result.id)" in text
    assert "ZimbraScheduledLifecycleExecutor" in text


def test_scheduler_does_not_call_manual_execute():
    text = Path("app/services/zimbra_observer_scheduler.py").read_text(encoding="utf-8")
    assert ".execute(" not in text
    assert "execute_from_observation" in text


def test_scheduled_executor_has_hr_and_import_interlocks():
    text = Path("app/services/zimbra_scheduled_lifecycle.py").read_text(encoding="utf-8")
    assert 'OneCImportRun.status == "running"' in text
    assert "_hr_snapshot()" in text
    assert "if not hr.fresh" in text
    assert "_is_web_protected" in text


def test_scheduled_executor_reuses_existing_observation_without_new_gaa():
    text = Path("app/services/zimbra_scheduled_lifecycle.py").read_text(encoding="utf-8")
    assert "_fresh_states" not in text
    assert "gaa -v" in text  # documented as intentionally not repeated
    assert "_current_status" not in text  # current status remains inside reused lifecycle methods
    assert "_execute_close" in text
    assert "_execute_archive" in text


def test_lifecycle_template_describes_automatic_execution():
    text = Path("app/templates/zimbra_lifecycle.html").read_text(encoding="utf-8")
    Environment().parse(text)
    assert "автоматический цикл" in text
    assert "Автоисполнение" in text
    assert "Backup + удаление автоматически" in text


def test_telegram_report_uses_compact_agreed_sections():
    text = Path("app/services/telegram_zimbra_daily_report.py").read_text(encoding="utf-8")
    assert "Отключены учетные записи уволенных работников:" in text
    assert "Отключены почтовые учетные записи по неактивности:" in text
    assert "Удалены почтовые учетные записи по сроку давности" in text
    assert "не используются более {retention_months} месяцев" in text
    assert "одного (1) года" not in text


def test_dismissal_report_is_based_on_ad_or_zimbra_actual_change():
    text = Path("app/services/telegram_zimbra_daily_report.py").read_text(encoding="utf-8")
    assert 'last_result.in_(("disabled", "closed"))' in text
    assert 'FinalDismissalBlockTarget.system == "zimbra"' not in text
    assert "DISMISSAL_REPORTED_ACTION" in text
