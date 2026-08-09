from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import HRSourceRecord, OneCImportRun
from app.models_zimbra_observer import (
    ZimbraLifecycleState,
    ZimbraObservationEvent,
    ZimbraObservationRun,
)
from app.services.zimbra_observer import (
    HRProtectionSnapshot,
    ObservedZimbraAccount,
    ZimbraObserverService,
    parse_dismissal_note_date,
    parse_zimbra_timestamp,
)
from app.services.zimbra_observer_scheduler import (
    ZimbraObserverScheduler,
    scheduled_datetime,
)


FIXED_NOW = datetime(2026, 8, 9, 5, 30, tzinfo=timezone.utc)


class ZimbraObserverTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = Settings(
            app_secret_key="test-secret-key-1234567890",
            zimbra_domains=["domain.com"],
            zimbra_primary_domain="domain.com",
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self) -> ZimbraObserverService:
        return ZimbraObserverService(self.settings, self.db)

    @staticmethod
    def account(
        email: str = "user@domain.com",
        *,
        status: str = "active",
        last_logon_at: datetime | None = None,
        created_at: datetime | None = None,
        note: str = "",
        aliases: tuple[str, ...] = (),
    ) -> ObservedZimbraAccount:
        return ObservedZimbraAccount(
            zimbra_id=f"id-{email}",
            primary_email=email,
            addresses=(email, *aliases),
            account_status=status,
            last_logon_at=last_logon_at,
            created_at=created_at,
            note=note,
        )

    def fresh_hr(self, *emails: str) -> HRProtectionSnapshot:
        return HRProtectionSnapshot(
            emails=frozenset(email.lower() for email in emails),
            snapshot_at=FIXED_NOW - timedelta(hours=2),
            age_minutes=120,
            fresh=True,
            records_count=max(1, len(emails)),
            source_count=1,
        )

    def test_archive_threshold_must_exceed_close_threshold(self):
        with self.assertRaisesRegex(ValueError, "больше порога закрытия"):
            self.service().save_settings(
                enabled=True,
                inactive_months=6,
                retention_months=6,
                schedule_time="08:30",
                exclude_active_hr=True,
                operator="admin",
            )

    def test_six_month_inactivity_is_close_candidate(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(last_logon_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("active.worker@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "close")
        self.assertIn("порог неактивности 6 мес.", evaluation.reason)

    def test_active_thirteen_month_account_is_still_close_not_delete(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(last_logon_at=datetime(2025, 7, 1, tzinfo=timezone.utc)),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("other@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "close")

    def test_closed_seven_month_account_is_not_archive_candidate(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                status="closed",
                last_logon_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                note="неактивна с 01.02.2020",
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("other@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "none")
        self.assertIn("12 мес. еще не достигнут", evaluation.reason)

    def test_closed_thirteen_month_account_is_archive_candidate(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                status="closed",
                last_logon_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("other@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "archive_delete")
        self.assertIn("резервная копия", evaluation.reason)
        self.assertIn("последнего входа", evaluation.reason)

    def test_closed_never_logged_in_uses_creation_date_for_archive(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                status="closed",
                last_logon_at=None,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("other@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "archive_delete")
        self.assertIn("даты создания", evaluation.reason)

    def test_active_hr_alias_suppresses_all_lifecycle_actions(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                last_logon_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                note="01.01.2026",
                aliases=("worker@domain.com",),
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("worker@domain.com"),
            dismissal_map={"user@domain.com": FIXED_NOW.date() - timedelta(days=30)},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "protected_hr")
        self.assertEqual(evaluation.matched_hr_email, "worker@domain.com")

    def test_closed_hr_account_is_protected_from_archive_delete(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                status="closed",
                last_logon_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("user@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "protected_hr")
        self.assertIn("удаление подавлены", evaluation.reason)

    def test_never_disable_has_priority_over_inactivity_and_dismissal(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                last_logon_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                note="служебная учетная запись; NEVER_DISABLE; Увольнение 01.01.2025",
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("someone.else@domain.com"),
            dismissal_map={"user@domain.com": FIXED_NOW.date() - timedelta(days=30)},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "protected_note")
        self.assertIn("never_disable", evaluation.reason)

    def test_never_disable_prevents_archive_delete_for_closed_account(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                status="closed",
                last_logon_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                note="never_disable\nнеактивна с 01.01.2020",
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("someone.else@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "protected_note")
        self.assertNotEqual(evaluation.recommendation, "archive_delete")

    def test_stale_hr_snapshot_prevents_close_recommendation(self):
        config = self.service().get_settings_record()
        hr = HRProtectionSnapshot(
            emails=frozenset(),
            snapshot_at=FIXED_NOW - timedelta(days=3),
            age_minutes=3 * 24 * 60,
            fresh=False,
            records_count=100,
            source_count=2,
            stale_sources=("org2",),
        )
        evaluation = self.service()._evaluate(
            self.account(last_logon_at=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            previous_state=None,
            config=config,
            hr=hr,
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "manual_review")
        self.assertIn("org2", evaluation.reason)

    def test_hr_snapshot_unions_all_sources_and_requires_each_fresh(self):
        self.db.add_all(
            [
                HRSourceRecord(
                    worker_key="worker-1",
                    source_id="org1",
                    fio="Работник Первый",
                    corporate_email="one@domain.com",
                    is_present=True,
                ),
                HRSourceRecord(
                    worker_key="worker-2",
                    source_id="org2",
                    fio="Работник Второй",
                    corporate_email="two@another-domain.ru",
                    is_present=True,
                ),
                OneCImportRun(
                    trigger="scheduled",
                    status="success",
                    source_id="org1",
                    workers_count=1,
                    started_at=FIXED_NOW - timedelta(hours=2),
                    completed_at=FIXED_NOW - timedelta(hours=2),
                ),
                OneCImportRun(
                    trigger="scheduled",
                    status="success",
                    source_id="org2",
                    workers_count=1,
                    started_at=FIXED_NOW - timedelta(hours=3),
                    completed_at=FIXED_NOW - timedelta(hours=3),
                ),
            ]
        )
        self.db.commit()
        with patch("app.services.zimbra_observer.utcnow", return_value=FIXED_NOW):
            snapshot = self.service()._hr_snapshot()
        self.assertTrue(snapshot.fresh)
        self.assertEqual(snapshot.source_count, 2)
        self.assertEqual(snapshot.emails, frozenset({"one@domain.com", "two@another-domain.ru"}))
        self.assertEqual(snapshot.age_minutes, 180)

        # Устаревание только второй организации делает весь объединенный список
        # небезопасным для новых блокировок/удалений.
        second_run = self.db.scalars(
            select(OneCImportRun).where(OneCImportRun.source_id == "org2")
        ).one()
        second_run.started_at = FIXED_NOW - timedelta(days=3)
        second_run.completed_at = FIXED_NOW - timedelta(days=3)
        self.db.commit()
        with patch("app.services.zimbra_observer.utcnow", return_value=FIXED_NOW):
            snapshot = self.service()._hr_snapshot()
        self.assertFalse(snapshot.fresh)
        self.assertEqual(snapshot.stale_sources, ("org2",))

    def test_future_legacy_note_date_suspends_inactivity_check(self):
        config = self.service().get_settings_record()
        evaluation = self.service()._evaluate(
            self.account(
                last_logon_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                note="увольнение 30 09 2026",
            ),
            previous_state=None,
            config=config,
            hr=self.fresh_hr("other@domain.com"),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "none")
        self.assertIn("30.09.2026", evaluation.reason)
        self.assertIn("неактивности не применяется", evaluation.reason)

    def test_same_recommendation_does_not_create_duplicate_event(self):
        self.db.add(
            HRSourceRecord(
                worker_key="worker-1",
                source_id="org_com",
                fio="Работник Тестовый",
                corporate_email="active.worker@domain.com",
                is_present=True,
            )
        )
        self.db.add(
            OneCImportRun(
                trigger="scheduled",
                status="success",
                source_id="org_com",
                workers_count=1,
                started_at=FIXED_NOW - timedelta(hours=2),
                completed_at=FIXED_NOW - timedelta(hours=2),
            )
        )
        self.db.commit()

        observed = self.account(
            last_logon_at=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
        service = self.service()
        with patch(
            "app.services.zimbra_observer.utcnow",
            return_value=FIXED_NOW,
        ), patch.object(service, "_fetch_accounts", return_value=[observed]):
            first = service.run("manual")
            second = service.run("manual")

        self.assertEqual(first.event_count, 1)
        self.assertEqual(second.event_count, 0)
        events = self.db.scalars(select(ZimbraObservationEvent)).all()
        self.assertEqual(len(events), 1)
        state = self.db.scalars(select(ZimbraLifecycleState)).one()
        self.assertEqual(state.recommendation, "close")

    def test_current_states_lists_all_accounts_from_all_domains_without_old_limit(self):
        rows = []
        for index in range(350):
            domain = "domain-a.ru" if index % 2 == 0 else "domain-b.com"
            rows.append(
                ZimbraLifecycleState(
                    account_key=f"state-{index}",
                    zimbra_id=f"zimbra-{index}",
                    primary_email=f"user{index:03d}@{domain}",
                    account_status="active",
                    recommendation="none",
                    reason="Действий не требуется",
                )
            )
        rows.append(
            ZimbraLifecycleState(
                account_key="state-close",
                zimbra_id="zimbra-close",
                primary_email="old@third-domain.net",
                account_status="active",
                recommendation="close",
                reason="Превышен срок неактивности",
            )
        )
        self.db.add_all(rows)
        self.db.commit()

        states = self.service().current_states()
        emails = {row.primary_email for row in states}

        self.assertEqual(len(states), 351)
        self.assertIn("user000@domain-a.ru", emails)
        self.assertIn("user001@domain-b.com", emails)
        self.assertIn("old@third-domain.net", emails)
        self.assertTrue(any(row.recommendation == "none" for row in states))
        self.assertEqual(states[0].recommendation, "close")

    def test_gaa_verbose_parser_reads_addresses_status_and_dates(self):
        output = """# name user@domain.com
mail: user@domain.com
zimbraMailAlias: alias@domain.com
zimbraId: abc-123
zimbraAccountStatus: active
zimbraLastLogonTimestamp: 20250801093000Z
zimbraCreateTimestamp: 20200102030405Z
zimbraNotes: test note
"""
        rows = ZimbraObserverService._parse_gaa_verbose(output)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].zimbra_id, "abc-123")
        self.assertEqual(rows[0].account_status, "active")
        self.assertIn("alias@domain.com", rows[0].addresses)
        self.assertEqual(rows[0].last_logon_at.year, 2025)

    def test_full_zimbra_read_is_not_scoped_to_configured_domains(self):
        output = b"""# name one@domain.com
mail: one@domain.com
zimbraId: id-1
zimbraAccountStatus: active
zimbraCreateTimestamp: 20200102030405Z

# name two@other.ru
mail: two@other.ru
zimbraId: id-2
zimbraAccountStatus: active
zimbraCreateTimestamp: 20200102030405Z
"""

        class FakeChannel:
            def __init__(self):
                self._out = output

            def recv_ready(self):
                return bool(self._out)

            def recv(self, _size):
                data, self._out = self._out, b""
                return data

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, _size):
                return b""

            def exit_status_ready(self):
                return not self._out

            def recv_exit_status(self):
                return 0

            def close(self):
                pass

        channel = FakeChannel()

        class FakeStream:
            def __init__(self, channel):
                self.channel = channel

        class FakeStdin:
            class C:
                @staticmethod
                def shutdown_write():
                    pass
            channel = C()

        class FakeClient:
            def __init__(self):
                self.command = ""

            def exec_command(self, command, timeout=None):
                self.command = command
                return FakeStdin(), FakeStream(channel), FakeStream(channel)

            def close(self):
                pass

        client = FakeClient()

        class FakeZimbraService:
            _query_lock = threading.Lock()

            def __init__(self, _settings):
                pass

            @staticmethod
            def _zmprov_command():
                return "zmprov"

            @staticmethod
            def _client():
                return client

        settings = Settings(
            app_secret_key="test-secret-key-1234567890",
            zimbra_domains=[],
            zimbra_primary_domain="",
        )
        service = ZimbraObserverService(settings, self.db)
        with patch("app.services.zimbra_observer.ZimbraService", FakeZimbraService):
            rows = service._fetch_accounts()
        self.assertEqual({row.primary_email for row in rows}, {"one@domain.com", "two@other.ru"})
        self.assertEqual(client.command, "zmprov -l gaa -v")

    def test_daily_schedule_uses_application_timezone(self):
        now = datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc)
        due = scheduled_datetime("08:30", "Europe/Moscow", now=now)
        self.assertEqual(due.hour, 8)
        self.assertEqual(due.minute, 30)
        self.assertEqual(due.utcoffset().total_seconds(), 3 * 3600)

    def test_failed_scheduled_run_is_retried_after_backoff(self):
        scheduler = ZimbraObserverScheduler(self.settings, None)
        now_local = datetime(2026, 8, 9, 9, 0, tzinfo=timezone(timedelta(hours=3)))
        now_utc = now_local.astimezone(timezone.utc)
        failed = ZimbraObservationRun(
            trigger="scheduled",
            status="failed",
            started_at=now_utc - timedelta(minutes=12),
            completed_at=now_utc - timedelta(minutes=11),
        )
        self.db.add(failed)
        self.db.commit()
        self.assertFalse(scheduler._already_ran_today(self.db, now_local))

        failed.completed_at = now_utc - timedelta(minutes=5)
        self.db.commit()
        self.assertTrue(scheduler._already_ran_today(self.db, now_local))

    def test_timestamp_and_legacy_note_parsers(self):
        parsed = parse_zimbra_timestamp("20260809083045Z")
        self.assertEqual(parsed, datetime(2026, 8, 9, 8, 30, 45, tzinfo=timezone.utc))
        for value in (
            "Увольнение 01.10.2025",
            "01,10,2025",
            "01 10 2025",
            "01102025",
            "01/10/2025",
            "01-10-2025",
        ):
            self.assertEqual(parse_dismissal_note_date(value).isoformat(), "2025-10-01")
        # Пока поле даты увольнения отсутствует в 1С, legacy-поведение старого
        # скрипта трактует любую дату в Notes как управляющую дату.
        self.assertEqual(
            parse_dismissal_note_date("неактивна с 01.10.2025").isoformat(),
            "2025-10-01",
        )


if __name__ == "__main__":
    unittest.main()
