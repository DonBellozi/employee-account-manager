from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace

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
            sender_filter="robot2@",
            attachment_filename="second.xlsx",
            has_corporate_email=False,
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

    def test_source_without_corporate_email_is_allowed(self):
        row = self.source()
        self.assertFalse(row.has_corporate_email)
        self.assertEqual(row.source_id, "company2.ru")

    def test_primary_domain_cannot_be_added_again(self):
        with self.assertRaisesRegex(ValueError, "основным источником"):
            self.source(mail_domain="company1.ru")

    def test_sender_filename_pair_is_unique(self):
        self.source()
        with self.assertRaisesRegex(ValueError, "отправителем"):
            self.source(
                mail_domain="company3.ru",
                name="Организация 3",
            )

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
        record = self.db.scalar(select(HRSourceRecord))
        self.assertEqual(record.source_id, "company2.ru")
        self.assertEqual(record.corporate_email, "")

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
