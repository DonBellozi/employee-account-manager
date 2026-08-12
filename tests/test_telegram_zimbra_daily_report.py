from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import AuditLog
from app.models_dismissal_lifecycle import (
    FinalDismissalBlockRun,
    FinalDismissalBlockTarget,
)
from app.models_telegram import TelegramNotification, TelegramSettings
from app.models_zimbra_lifecycle import ZimbraLifecycleAction
from app.models_zimbra_observer import (
    ZimbraLifecycleState,
    ZimbraObservationEvent,
    ZimbraObserverSettings,
)
from app.services.telegram_zimbra_daily_report import (
    MAX_TELEGRAM_CHARS,
    REPORT_ACTION,
    TelegramZimbraDailyReportService,
)


def settings() -> Settings:
    return Settings(
        app_secret_key="x" * 32,
        app_timezone="Europe/Moscow",
    )


def db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def enable_telegram(db: Session) -> None:
    db.add(
        TelegramSettings(
            id=1,
            enabled=True,
            bot_token_encrypted="configured",
            chat_id="-1001234567890",
        )
    )
    db.add(
        ZimbraObserverSettings(
            id=1,
            enabled=True,
            inactive_months=6,
            retention_months=12,
            schedule_time="08:30",
            exclude_active_hr=True,
        )
    )
    db.commit()


def test_collects_actual_dismissal_inactive_and_delete_actions():
    db = db_session()
    enable_telegram(db)
    svc = TelegramZimbraDailyReportService(settings(), db)

    run = FinalDismissalBlockRun(
        worker_key="worker-1",
        dismissal_date=date(2026, 8, 11),
        effective_block_date=date(2026, 8, 11),
        fio="Иванов Иван",
        status="success",
    )
    db.add(run)
    db.flush()
    db.add(
        FinalDismissalBlockTarget(
            run_id=run.id,
            system="zimbra",
            target_key="zimbra:1",
            target_identifier="dismissed@domain.ru",
            stable_id="z1",
            status="completed",
            last_result="closed",
            completed_at=datetime(2026, 8, 11, 16, 10, tzinfo=timezone.utc),
        )
    )

    db.add(
        ZimbraLifecycleState(
            account_key="z2",
            zimbra_id="z2",
            primary_email="inactive@domain.com",
            account_status="closed",
            last_logon_at=datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc),
        )
    )
    db.add(
        ZimbraObservationEvent(
            run_id=10,
            account_key="z2",
            zimbra_id="z2",
            primary_email="inactive@domain.com",
            previous_recommendation="none",
            recommendation="close",
            reason="Активность 10.01.2026. Неактивность ≥ 6 мес.",
            account_status="active",
            last_logon_at=datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc),
            hr_active=False,
            created_at=datetime(2026, 8, 11, 5, 30, tzinfo=timezone.utc),
        )
    )
    db.add(
        ZimbraLifecycleAction(
            run_id=20,
            account_key="z2",
            zimbra_id="z2",
            primary_email="inactive@domain.com",
            recommendation="close",
            action="close",
            status="success",
            message="Учетная запись Zimbra закрыта; статус closed подтвержден.",
            completed_at=datetime(2026, 8, 11, 5, 31, tzinfo=timezone.utc),
        )
    )
    db.add(
        ZimbraLifecycleAction(
            run_id=21,
            account_key="z3",
            zimbra_id="z3",
            primary_email="deleted@domain.com",
            recommendation="archive_delete",
            action="delete",
            status="success",
            message="Учетная запись удалена.",
            completed_at=datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc),
        )
    )
    db.commit()

    report = svc.collect(date(2026, 8, 11))
    assert [x.email for x in report.disabled] == [
        "dismissed@domain.ru",
        "inactive@domain.com",
    ]
    assert report.disabled[0].reason == "увольнение 11.08.2026"
    assert report.disabled[1].reason == "неактивна с 10.01.2026"
    assert [x.email for x in report.deleted] == ["deleted@domain.com"]

    text = "\n".join(svc.render_messages(report))
    assert "Отключены учетные записи уволенных работников:" in text
    assert "Отключены почтовые учетные записи по неактивности:" in text
    assert "увольнение 11.08.2026" in text
    assert "неактивна с 10.01.2026" in text
    assert "не используются более 12 месяцев" in text
    assert "удалено: 11.08.2026" in text


def test_already_closed_success_is_not_reported_as_new_close():
    db = db_session()
    enable_telegram(db)
    db.add(
        ZimbraLifecycleAction(
            run_id=1,
            account_key="z1",
            zimbra_id="z1",
            primary_email="old@domain.com",
            recommendation="close",
            action="close",
            status="success",
            message="На момент выполнения уже была закрыта.",
            completed_at=datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc),
        )
    )
    db.commit()

    report = TelegramZimbraDailyReportService(settings(), db).collect(date(2026, 8, 11))
    assert report.disabled == ()


def test_first_start_arms_without_historical_backfill_then_next_day_queues_once():
    db = db_session()
    enable_telegram(db)
    svc = TelegramZimbraDailyReportService(settings(), db)

    # Первый запуск 12 августа после 08:45 только вооружает отчет.
    result = svc.enqueue_due(
        now=datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    )
    assert result["status"] == "not_armed_for_date"
    assert db.scalar(select(TelegramNotification.id)) is None

    # Событие 12 августа должно уйти утром 13 августа.
    db.add(
        ZimbraLifecycleAction(
            run_id=2,
            account_key="z2",
            zimbra_id="z2",
            primary_email="deleted@domain.com",
            recommendation="archive_delete",
            action="delete",
            status="success",
            message="Удалена.",
            completed_at=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        )
    )
    db.commit()

    result = svc.enqueue_due(
        now=datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    )
    assert result == {"status": "queued", "queued": 1}
    assert len(list(db.scalars(select(TelegramNotification)).all())) == 1

    again = svc.enqueue_due(
        now=datetime(2026, 8, 13, 6, 1, tzinfo=timezone.utc)
    )
    assert again == {"status": "already_done", "queued": 0}
    assert len(list(db.scalars(select(TelegramNotification)).all())) == 1

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == REPORT_ACTION,
            AuditLog.target == "2026-08-12",
        )
    )
    assert audit is not None
    assert audit.result == "queued"


def test_report_is_split_without_exceeding_telegram_limit():
    db = db_session()
    enable_telegram(db)
    svc = TelegramZimbraDailyReportService(settings(), db)

    from app.services.telegram_zimbra_daily_report import (
        DisabledEntry,
        ZimbraDailyReport,
    )

    report = ZimbraDailyReport(
        report_date=date(2026, 8, 11),
        disabled=tuple(
            DisabledEntry(
                email=f"very.long.account.{i:03d}@example-domain.company",
                reason="неактивна с 01.01.2026",
                reason_kind="inactive",
            )
            for i in range(250)
        ),
        deleted=(),
        retention_months=12,
    )

    messages = svc.render_messages(report)
    assert len(messages) > 1
    assert all(len(item) <= MAX_TELEGRAM_CHARS for item in messages)
    assert all("Отключены почтовые учетные записи по неактивности" in item for item in messages)


def test_ad_only_change_still_reports_dismissal_once_with_zimbra_email():
    db = db_session()
    enable_telegram(db)
    svc = TelegramZimbraDailyReportService(settings(), db)

    run = FinalDismissalBlockRun(
        worker_key="worker-ad-only",
        dismissal_date=date(2026, 8, 11),
        effective_block_date=date(2026, 8, 11),
        fio="Петров Петр",
        status="partial",
    )
    db.add(run)
    db.flush()
    db.add_all(
        [
            FinalDismissalBlockTarget(
                run_id=run.id,
                system="ad",
                target_key="ad:guid-1",
                target_identifier="petrov.pp",
                stable_id="guid-1",
                status="completed",
                last_result="disabled",
                completed_at=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
            ),
            FinalDismissalBlockTarget(
                run_id=run.id,
                system="zimbra",
                target_key="zimbra:z1",
                target_identifier="petrov.pp@domain.ru",
                stable_id="z1",
                status="already_completed",
                last_result="already_closed",
                completed_at=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()

    report = svc.collect(date(2026, 8, 11))
    assert len(report.disabled) == 1
    assert report.disabled[0].email == "petrov.pp@domain.ru"
    assert report.disabled[0].reason == "увольнение 11.08.2026"
    assert report.dismissal_run_ids == (run.id,)


def test_deletion_header_uses_month_setting_without_year_conversion():
    db = db_session()
    enable_telegram(db)
    row = db.get(ZimbraObserverSettings, 1)
    row.retention_months = 18
    db.commit()

    db.add(
        ZimbraLifecycleAction(
            run_id=30,
            account_key="z18",
            zimbra_id="z18",
            primary_email="old18@domain.com",
            recommendation="archive_delete",
            action="delete",
            status="success",
            message="Удалена.",
            completed_at=datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc),
        )
    )
    db.commit()

    report = TelegramZimbraDailyReportService(settings(), db).collect(date(2026, 8, 11))
    text = "\n".join(TelegramZimbraDailyReportService(settings(), db).render_messages(report))
    assert "не используются более 18 месяцев" in text
    assert "года" not in text


def test_reported_dismissal_run_is_not_repeated_on_later_target_completion():
    db = db_session()
    enable_telegram(db)
    svc = TelegramZimbraDailyReportService(settings(), db)

    run = FinalDismissalBlockRun(
        worker_key="worker-repeat",
        dismissal_date=date(2026, 8, 11),
        effective_block_date=date(2026, 8, 11),
        fio="Повтор Повторов",
        status="partial",
    )
    db.add(run)
    db.flush()
    db.add(
        FinalDismissalBlockTarget(
            run_id=run.id,
            system="ad",
            target_key="ad:g2",
            target_identifier="repeat.rr",
            stable_id="g2",
            status="completed",
            last_result="disabled",
            completed_at=datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc),
        )
    )
    db.add(
        FinalDismissalBlockTarget(
            run_id=run.id,
            system="zimbra",
            target_key="zimbra:z2",
            target_identifier="repeat.rr@domain.ru",
            stable_id="z2",
            status="pending",
        )
    )
    db.commit()

    first = svc.collect(date(2026, 8, 11))
    assert first.dismissal_run_ids == (run.id,)
    svc._mark_done(first, result="queued", chunks=1)

    zimbra = db.scalar(
        select(FinalDismissalBlockTarget).where(
            FinalDismissalBlockTarget.run_id == run.id,
            FinalDismissalBlockTarget.system == "zimbra",
        )
    )
    zimbra.status = "completed"
    zimbra.last_result = "closed"
    zimbra.completed_at = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    db.commit()

    second = svc.collect(date(2026, 8, 12))
    assert all(item.reason_kind != "dismissal" for item in second.disabled)
