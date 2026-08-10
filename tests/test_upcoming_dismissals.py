from __future__ import annotations

from datetime import timedelta
import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import AuditLog, HRSourceRecord
from app.models_dismissals import DismissalDeferral
from app.models_onec_sources import HREmploymentState
from app.services.upcoming_dismissals import UpcomingDismissalService


class FakeSettings:
    app_timezone = "UTC"
    onec_data_dir = "/tmp/does-not-exist"
    onec_worker_hash_secret = "secret"
    app_secret_key = "app-secret"
    onec_header_search_rows = 10


class UpcomingDismissalTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = FakeSettings()
        self.service = UpcomingDismissalService(self.settings, self.db)
        self.today = self.service.today

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _state(self, worker, source, status, days=None):
        dismissal = self.today + timedelta(days=days) if days is not None else None
        row = HREmploymentState(
            worker_key=worker,
            source_id=source,
            source_name=source,
            fio="Иванов Иван",
            status=status,
            is_present=True,
            dismissal_date=dismissal,
            status_reason="dismissal_date" if dismissal else "current_export",
        )
        self.db.add(row)
        return row

    def _record(self, worker, source):
        self.db.add(
            HRSourceRecord(
                worker_key=worker,
                source_id=source,
                source_name=source,
                fio="Иванов Иван",
                corporate_email=f"ivanov@{source}",
                personal_email="",
                login="ivanov",
                placements_json=json.dumps([
                    {"department": "Отдел", "position": "Инженер"}
                ]),
                is_present=True,
            )
        )

    def test_active_second_organization_excludes_worker(self):
        self._state("worker", "one.ru", "scheduled", 5)
        self._state("worker", "two.ru", "active")
        self._record("worker", "one.ru")
        self._record("worker", "two.ru")
        self.db.commit()
        self.assertEqual(self.service.list_upcoming(), [])

    def test_last_date_across_organizations_is_final_date(self):
        self._state("worker", "one.ru", "scheduled", 5)
        self._state("worker", "two.ru", "scheduled", 10)
        self._record("worker", "one.ru")
        self._record("worker", "two.ru")
        self.db.commit()
        rows = self.service.list_upcoming()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dismissal_date"], self.today + timedelta(days=10))

    def test_defer_adds_week_and_logs_operation(self):
        self._state("worker", "one.ru", "scheduled", 5)
        self._record("worker", "one.ru")
        self.db.commit()
        current = self.service.list_upcoming()[0]
        result = self.service.defer(
            worker_key="worker",
            expected_dismissal_date=current["dismissal_date"],
            operator_username="admin",
        )
        self.assertEqual(
            result["deferred_until"],
            current["dismissal_date"] + timedelta(days=7),
        )
        saved = self.db.scalar(select(DismissalDeferral))
        self.assertIsNotNone(saved)
        event = self.db.scalar(
            select(AuditLog).where(AuditLog.action == "final_dismissal_deferred")
        )
        self.assertIsNotNone(event)

    def test_second_defer_extends_another_week(self):
        self._state("worker", "one.ru", "scheduled", 5)
        self._record("worker", "one.ru")
        self.db.commit()
        current = self.service.list_upcoming()[0]
        first = self.service.defer(
            worker_key="worker",
            expected_dismissal_date=current["dismissal_date"],
            operator_username="admin",
        )
        second = self.service.defer(
            worker_key="worker",
            expected_dismissal_date=current["dismissal_date"],
            operator_username="admin",
        )
        self.assertEqual(
            second["deferred_until"],
            first["deferred_until"] + timedelta(days=7),
        )


if __name__ == "__main__":
    unittest.main()
