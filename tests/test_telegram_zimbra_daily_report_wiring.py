from pathlib import Path


def test_telegram_worker_runs_daily_report_before_queue_delivery():
    text = Path("app/services/telegram_worker.py").read_text(encoding="utf-8")
    assert "TelegramZimbraDailyReportService" in text
    report_pos = text.index(").enqueue_due()")
    queue_pos = text.index(").process_due()")
    assert report_pos < queue_pos


def test_daily_report_uses_existing_audit_and_telegram_queue():
    text = Path("app/services/telegram_zimbra_daily_report.py").read_text(encoding="utf-8")
    assert "AuditLog" in text
    assert "TelegramService" in text
    assert 'event_type=EVENT_TYPE' in text
    assert 'parse_mode="HTML"' in text
    assert "MAX_TELEGRAM_CHARS = 3900" in text
