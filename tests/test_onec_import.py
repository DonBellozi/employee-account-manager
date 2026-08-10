from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import OneCImportRun
from app.services.onec_imap import OneCAttachment
from app.services.onec_import import OneCImportService


def make_xlsx() -> bytes:
    temp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    path = Path(temp.name)
    temp.close()
    try:
        wb = Workbook()
        ws = wb.active
        ws.append(["Отчет"])
        ws.append(
            [
                "СНИЛС",
                "Сотрудник.Физическое лицо.ФИО",
                "Физическое лицо.Адрес электронной почты",
                "Должность",
                "Дата увольнения",
            ]
        )
        ws.append(["Отдел ИТ"])
        ws.append(
            [
                "123-456-789 01",
                "Иванов Иван Иванович",
                "ivanov.ii@example.ru",
                "Специалист",
                "",
            ]
        )
        wb.save(path)
        wb.close()
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


class OneCImportHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef0123456789abcdef",
            onec_data_dir=self.tempdir.name,
            onec_source_domain="example.ru",
            zimbra_domains="example.ru",
        )
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.payload = make_xlsx()
        self.attachment = OneCAttachment(
            uid="101",
            message_date="Fri, 08 Aug 2026 01:30:00 +0300",
            sender="1c-robot@example.ru",
            subject="Кадровая выгрузка",
            filename=self.settings.onec_attachment_filename,
            file_hash=hashlib.sha256(self.payload).hexdigest(),
            payload=self.payload,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def registry_mock(self):
        registry = SimpleNamespace()
        registry.source_id = "example.ru"
        registry.sync_workbook = lambda workbook: {
            "created_people": 1,
            "created_source_records": 1,
            "marked_missing": 0,
        }
        registry.reconcile_current = lambda: {
            "total": 1,
            "ok": 1,
            "issues": 0,
            "errors": 0,
            "not_checked": 0,
        }
        return registry

    @patch("app.services.onec_import.HRRegistryService")
    @patch("app.services.onec_import.OneCImapService")
    def test_duplicate_sha_is_not_processed_twice(self, imap_cls, registry_cls):
        imap_cls.return_value.find_latest_attachment.return_value = self.attachment
        registry = self.registry_mock()
        registry_cls.return_value = registry

        service = OneCImportService(self.settings, self.db)
        first = service.analyze_latest(trigger="manual")
        self.assertEqual(first["import_status"], "success")
        current_before = service.current_file.read_bytes()
        snapshot_before = service.snapshot_file.read_bytes()

        second = service.analyze_latest(trigger="scheduled")
        self.assertEqual(second["import_status"], "duplicate")
        self.assertEqual(service.current_file.read_bytes(), current_before)
        self.assertEqual(service.snapshot_file.read_bytes(), snapshot_before)

        runs = self.db.query(OneCImportRun).order_by(OneCImportRun.id).all()
        self.assertEqual([run.status for run in runs], ["success", "duplicate"])
        self.assertEqual(registry_cls.call_count, 1)

    @patch("app.services.onec_import.OneCImapService")
    def test_bad_xlsx_does_not_replace_last_successful_snapshot(self, imap_cls):
        service = OneCImportService(self.settings, self.db)
        service.data_dir.mkdir(parents=True, exist_ok=True)
        service.current_file.write_bytes(b"previous-good-xlsx")
        service.snapshot_file.write_text(
            '{"old": {"fio": "Предыдущий"}}',
            encoding="utf-8",
        )
        service.report_file.write_text(
            '{"analyzed_at": "old-report"}',
            encoding="utf-8",
        )

        bad = b"not-an-xlsx"
        imap_cls.return_value.find_latest_attachment.return_value = OneCAttachment(
            uid="102",
            message_date="",
            sender="",
            subject="",
            filename=self.settings.onec_attachment_filename,
            file_hash=hashlib.sha256(bad).hexdigest(),
            payload=bad,
        )

        with self.assertRaises(Exception):
            service.analyze_latest(trigger="scheduled")

        self.assertEqual(service.current_file.read_bytes(), b"previous-good-xlsx")
        self.assertEqual(
            service.snapshot_file.read_text(encoding="utf-8"),
            '{"old": {"fio": "Предыдущий"}}',
        )
        self.assertEqual(
            service.report_file.read_text(encoding="utf-8"),
            '{"analyzed_at": "old-report"}',
        )

        run = self.db.query(OneCImportRun).one()
        self.assertEqual(run.status, "failed")
        self.assertIn("Предыдущий успешный снимок", run.message)

    @patch("app.services.onec_import.HRRegistryService")
    @patch("app.services.onec_import.OneCImapService")
    def test_reconciliation_error_keeps_valid_hr_import(self, imap_cls, registry_cls):
        imap_cls.return_value.find_latest_attachment.return_value = self.attachment

        registry = self.registry_mock()

        def fail_reconciliation():
            raise RuntimeError("AD временно недоступен")

        registry.reconcile_current = fail_reconciliation
        registry_cls.return_value = registry

        service = OneCImportService(self.settings, self.db)
        report = service.analyze_latest(trigger="scheduled")

        self.assertEqual(report["import_status"], "partial")
        self.assertTrue(service.current_file.is_file())
        self.assertTrue(service.snapshot_file.is_file())
        run = self.db.query(OneCImportRun).one()
        self.assertEqual(run.status, "partial")
        self.assertIn("AD временно недоступен", run.error_message)


if __name__ == "__main__":
    unittest.main()
