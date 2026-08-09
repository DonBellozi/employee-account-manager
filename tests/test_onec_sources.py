from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import HRPerson, HRSourceRecord
from app.models_onec_sources import HREmploymentState, OneCAdditionalSource
from app.services.onec_additional_import import OneCAdditionalImportService
from app.services.onec_sources import OneCSourceRegistryService
from app.services.onec_xlsx import OneCPlacement, OneCWorkbook, OneCWorker


class FakeSettings:
    onec_source_domain = "company1.ru"
    onec_imap_folder = "HR"
    onec_imap_from_contains = "robot1@"
    onec_attachment_filename = "main.xlsx"
    onec_data_dir = "/tmp/onec-tests"
    onec_worker_hash_secret = "worker-secret"
    app_secret_key = "app-secret-key-123456"
    onec_header_search_rows = 20
    app_timezone = "Europe/Moscow"


class OneCSourcesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = FakeSettings()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def registry(self):
        return OneCSourceRegistryService(self.settings, self.db)

    def source(self, **overrides):
        values = dict(
            source_id=None,
            name="Организация 2",
            mail_domain="company2.ru",
            imap_folder="INBOX",
            sender_filter="robot2@",
            attachment_filename="second.xlsx",
            enabled=True,
            operator="admin",
        )
        values.update(overrides)
        return self.registry().save(**values)

    @staticmethod
    def worker(key="w1", fio="Иванов Иван", email=None):
        return OneCWorker(
            worker_key=key,
            fio=fio,
            email=email,
            login=email.split("@", 1)[0] if email else None,
            placements=(OneCPlacement("Отдел", "Инженер"),),
            personal_email=None,
        )

    def workbook(self, *workers):
        return OneCWorkbook(
            workers=tuple(workers),
            headers=("СНИЛС", "ФИО"),
            header_row=2,
            detected_columns={"snils": "СНИЛС", "fio": "ФИО"},
            potential_dismissal_columns=(),
        )

    def test_primary_is_seeded_from_existing_settings(self):
        primary = self.registry().ensure_primary()
        self.assertTrue(primary.is_primary)
        self.assertEqual(primary.mail_domain, "company1.ru")
        self.assertEqual(primary.imap_folder, "HR")
        self.assertEqual(primary.sender_filter, "robot1@")
        self.assertEqual(primary.attachment_filename, "main.xlsx")

    def test_primary_web_save_updates_runtime_settings(self):
        registry = self.registry()
        primary = registry.ensure_primary()
        registry.save(
            source_id=primary.id,
            name="Компания 1",
            mail_domain="company1.ru",
            imap_folder="Staff",
            sender_filter="newrobot@",
            attachment_filename="staff.xlsx",
            enabled=True,
            operator="admin",
        )
        self.assertEqual(self.settings.onec_imap_folder, "Staff")
        self.assertEqual(self.settings.onec_imap_from_contains, "newrobot@")
        self.assertEqual(self.settings.onec_source_domain, "company1.ru")
        self.assertEqual(self.settings.onec_attachment_filename, "staff.xlsx")

    def test_additional_source_has_same_four_source_settings(self):
        row = self.source(imap_folder="Reports")
        self.assertFalse(row.is_primary)
        self.assertEqual(row.imap_folder, "Reports")
        self.assertEqual(row.sender_filter, "robot2@")
        self.assertEqual(row.mail_domain, "company2.ru")
        self.assertEqual(row.attachment_filename, "second.xlsx")

    def test_sender_can_be_empty_when_filename_identifies_source(self):
        row = self.source(sender_filter="")
        self.assertEqual(row.sender_filter, "")

    def test_source_specific_folder_is_passed_to_imap(self):
        source = self.source(imap_folder="Company2")
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        with patch(
            "app.services.onec_additional_import.OneCImapService"
        ) as imap_cls:
            service.find_latest()
            imap_cls.return_value.find_latest_attachment.assert_called_once_with(
                folder="Company2",
                sender_filter="robot2@",
                attachment_filename="second.xlsx",
            )

    def test_missing_corporate_email_uses_standard_issue_state(self):
        source = self.source()
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        service._sync_registry(
            workbook=self.workbook(self.worker()),
            dismissal_dates={},
        )
        record = self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == "company2.ru"
            )
        )
        self.assertEqual(record.zimbra_status, "no_email")
        self.assertEqual(record.ad_status, "no_login")
        self.assertEqual(record.reconciliation_status, "issue")

    def test_email_appearance_clears_temporary_no_email_state(self):
        source = self.source()
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        service._sync_registry(
            workbook=self.workbook(self.worker()),
            dismissal_dates={},
        )
        service._sync_registry(
            workbook=self.workbook(
                self.worker(email="ivanov@company2.ru")
            ),
            dismissal_dates={},
        )
        record = self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == "company2.ru"
            )
        )
        self.assertEqual(record.zimbra_status, "not_checked")
        self.assertEqual(record.ad_status, "not_checked")
        self.assertEqual(record.reconciliation_status, "not_checked")

    def test_active_worker_creates_shared_person_and_source_record(self):
        source = self.source()
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        summary = service._sync_registry(
            workbook=self.workbook(self.worker()),
            dismissal_dates={},
        )
        self.assertEqual(summary["active"], 1)
        self.assertEqual(
            self.db.scalar(select(HRPerson)).worker_key,
            "w1",
        )

    def test_future_dismissal_is_scheduled(self):
        source = self.source()
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        future = date.today() + timedelta(days=5)
        service._sync_registry(
            workbook=self.workbook(self.worker()),
            dismissal_dates={"w1": future},
        )
        state = self.db.scalar(select(HREmploymentState))
        self.assertEqual(state.status, "scheduled")
        self.assertEqual(state.dismissal_date, future)

    def test_past_dismissal_is_dismissed(self):
        source = self.source()
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        past = date.today() - timedelta(days=1)
        service._sync_registry(
            workbook=self.workbook(self.worker()),
            dismissal_dates={"w1": past},
        )
        state = self.db.scalar(select(HREmploymentState))
        self.assertEqual(state.status, "dismissed")

    def test_missing_from_next_export_means_dismissed_in_that_org(self):
        source = self.source()
        service = OneCAdditionalImportService(
            self.settings,
            self.db,
            source,
        )
        service._sync_registry(
            workbook=self.workbook(self.worker()),
            dismissal_dates={},
        )
        service._sync_registry(
            workbook=self.workbook(),
            dismissal_dates={},
        )
        record = self.db.scalar(select(HRSourceRecord))
        state = self.db.scalar(select(HREmploymentState))
        self.assertFalse(record.is_present)
        self.assertEqual(state.status, "dismissed")
        self.assertEqual(state.status_reason, "absent_from_export")


if __name__ == "__main__":
    unittest.main()
