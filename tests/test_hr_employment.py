from __future__ import annotations

from datetime import timedelta
import unittest
from zoneinfo import ZoneInfo
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.services.hr_employment import sync_workbook_employment
from app.services.onec_xlsx import OneCPlacement, OneCWorkbook, OneCWorker


class HREmploymentSyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.today = datetime.now(ZoneInfo("UTC")).date()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _book(self, dismissal_date):
        return OneCWorkbook(
            workers=(
                OneCWorker(
                    worker_key="worker",
                    fio="Иванов Иван",
                    email="ivanov@example.ru",
                    login="ivanov",
                    placements=(OneCPlacement("Отдел", "Инженер"),),
                    dismissal_date=dismissal_date,
                ),
            ),
            headers=("СНИЛС", "Дата увольнения"),
            header_row=1,
            detected_columns={"snils": "СНИЛС"},
            potential_dismissal_columns=("Дата увольнения",),
            dismissal_column="Дата увольнения",
        )

    def test_future_final_date_creates_scheduled_state(self):
        dismissal = self.today + timedelta(days=4)
        sync_workbook_employment(
            self.db,
            workbook=self._book(dismissal),
            source_id="one.ru",
            source_name="Компания 1",
            timezone_name="UTC",
        )
        self.db.commit()
        state = self.db.scalar(select(HREmploymentState))
        self.assertEqual(state.status, "scheduled")
        self.assertEqual(state.dismissal_date, dismissal)

    def test_no_final_date_keeps_active_state(self):
        sync_workbook_employment(
            self.db,
            workbook=self._book(None),
            source_id="one.ru",
            source_name="Компания 1",
            timezone_name="UTC",
        )
        self.db.commit()
        state = self.db.scalar(select(HREmploymentState))
        self.assertEqual(state.status, "active")
        self.assertIsNone(state.dismissal_date)


if __name__ == "__main__":
    unittest.main()
