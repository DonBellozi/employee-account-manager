from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_dismissal_lifecycle import FinalDismissalBlockRun
from app.models_onec_sources import HREmploymentState
from app.services.final_dismissal_lifecycle import (
    FinalDismissalLifecycleService,
    POST_RECONCILE_ACTION,
    utcnow,
)


class FinalDismissalPostReconcileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.settings = Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef0123456789abcdef",
            app_timezone="Europe/Moscow",
            onec_source_domain="example.ru",
            zimbra_domains="example.ru",
            ad_check_enabled=True,
            zimbra_check_enabled=True,
            dry_run=False,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _seed(self, *, employment_status: str = "dismissed"):
        worker_key = "a" * 64
        record = HRSourceRecord(
            worker_key=worker_key,
            source_id="example.ru",
            source_name="Example",
            fio="Иванов Иван Иванович",
            corporate_email="ivanov@example.ru",
            personal_email="",
            login="ivanov",
            placements_json="[]",
            is_present=True,
            ad_status="enabled",
            zimbra_status="present",
            reconciliation_status="issue",
        )
        mapping = EmailLoginMapping(
            worker_key=worker_key,
            source_domain="example.ru",
            source_email="ivanov@example.ru",
            ad_object_guid="11111111-1111-1111-1111-111111111111",
            ad_login="ivanov",
            zimbra_id="22222222-2222-2222-2222-222222222222",
            zimbra_email="ivanov@example.ru",
            created_by="test",
        )
        state = HREmploymentState(
            worker_key=worker_key,
            source_id="example.ru",
            source_name="Example",
            fio="Иванов Иван Иванович",
            status=employment_status,
            is_present=True,
            dismissal_date=date(2026, 8, 10),
        )
        run = FinalDismissalBlockRun(
            worker_key=worker_key,
            dismissal_date=date(2026, 8, 10),
            effective_block_date=date(2026, 8, 10),
            fio="Иванов Иван Иванович",
            status="success",
        )
        self.db.add_all([record, mapping, state, run])
        self.db.commit()
        return record, mapping, run

    @staticmethod
    def _ad_user(*, enabled: bool):
        return SimpleNamespace(
            username="ivanov",
            object_guid="11111111-1111-1111-1111-111111111111",
            is_enabled=enabled,
        )

    @staticmethod
    def _zimbra_identity(*, status: str):
        return SimpleNamespace(
            zimbra_id="22222222-2222-2222-2222-222222222222",
            primary_email="ivanov@example.ru",
            login="ivanov",
            addresses=("ivanov@example.ru",),
            account_status=status,
        )

    @patch("app.services.final_dismissal_lifecycle.ZimbraService")
    @patch("app.services.final_dismissal_lifecycle.ActiveDirectoryService")
    def test_successful_block_is_immediately_reconciled(
        self,
        ad_cls,
        zimbra_cls,
    ):
        record, mapping, run = self._seed()
        ad_cls.return_value.get_user_by_object_guid.return_value = self._ad_user(
            enabled=False
        )
        identity = self._zimbra_identity(status="closed")
        zimbra_cls.return_value.accounts_by_ids.return_value = {
            identity.zimbra_id: identity
        }

        ok = FinalDismissalLifecycleService(
            self.settings,
            self.db,
        )._post_reconcile_worker(run)

        self.assertTrue(ok)
        self.db.refresh(record)
        self.assertEqual(record.ad_status, "disabled")
        self.assertEqual(record.zimbra_status, "closed")
        self.assertEqual(record.reconciliation_status, "ok")
        self.assertIsNotNone(record.reconciled_at)

        event = self.db.scalar(
            select(AuditLog).where(
                AuditLog.action == POST_RECONCILE_ACTION
            )
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.result, "success")

    @patch("app.services.final_dismissal_lifecycle.ZimbraService")
    @patch("app.services.final_dismissal_lifecycle.ActiveDirectoryService")
    def test_active_employment_keeps_disabled_accounts_as_issue(
        self,
        ad_cls,
        zimbra_cls,
    ):
        record, _, run = self._seed(employment_status="active")
        ad_cls.return_value.get_user_by_object_guid.return_value = self._ad_user(
            enabled=False
        )
        identity = self._zimbra_identity(status="closed")
        zimbra_cls.return_value.accounts_by_ids.return_value = {
            identity.zimbra_id: identity
        }

        FinalDismissalLifecycleService(
            self.settings,
            self.db,
        )._post_reconcile_worker(run)

        self.db.refresh(record)
        self.assertEqual(record.ad_status, "disabled")
        self.assertEqual(record.zimbra_status, "closed")
        self.assertEqual(record.reconciliation_status, "issue")

    def test_failed_post_reconcile_is_throttled_and_success_is_final(self):
        _, _, run = self._seed()
        service = FinalDismissalLifecycleService(self.settings, self.db)
        target = service._post_reconcile_target(run)

        failed = AuditLog(
            actor="system",
            action=POST_RECONCILE_ACTION,
            target=target,
            result="error",
            details="temporary",
            created_at=utcnow(),
        )
        self.db.add(failed)
        self.db.commit()
        self.assertFalse(service._post_reconcile_due(run))

        failed.created_at = utcnow() - timedelta(minutes=6)
        self.db.commit()
        self.assertTrue(service._post_reconcile_due(run))

        success = AuditLog(
            actor="system",
            action=POST_RECONCILE_ACTION,
            target=target,
            result="success",
            details="ok",
            created_at=utcnow(),
        )
        self.db.add(success)
        self.db.commit()
        self.assertFalse(service._post_reconcile_due(run))


if __name__ == "__main__":
    unittest.main()
