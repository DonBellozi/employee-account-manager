from __future__ import annotations

import unittest

from sqlalchemy import Boolean, Integer, String, create_engine
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import HRSourceRecord
from app.models_onec_sources import OneCAdditionalSource
from app.services.hr_registry_multisource import (
    MultiSourceHRRegistryViewService,
)


class FakeSettings:
    onec_source_domain = "one.ru"
    zimbra_domains = ["one.ru", "two.ru"]

    def model_copy(self, deep=False):
        copied = FakeSettings()
        copied.onec_source_domain = self.onec_source_domain
        copied.zimbra_domains = list(self.zimbra_domains)
        return copied


class MultiSourceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = FakeSettings()

        self.db.add_all([
            OneCAdditionalSource(
                name="Компания 1",
                mail_domain="one.ru",
                imap_folder="INBOX",
                sender_filter="one@",
                attachment_filename="one.xlsx",
                is_primary=True,
                enabled=True,
            ),
            OneCAdditionalSource(
                name="Компания 2",
                mail_domain="two.ru",
                imap_folder="INBOX",
                sender_filter="two@",
                attachment_filename="two.xlsx",
                is_primary=False,
                enabled=True,
            ),
            HRSourceRecord(
                worker_key="w1",
                source_id="one.ru",
                source_name="one.ru",
                fio="Иванов Иван",
                corporate_email="ivanov@one.ru",
                personal_email="",
                login="ivanov",
                placements_json="[]",
                is_present=True,
            ),
            HRSourceRecord(
                worker_key="w2",
                source_id="two.ru",
                source_name="Компания 2",
                fio="Петров Петр",
                corporate_email="",
                personal_email="",
                login="",
                placements_json="[]",
                is_present=True,
            ),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_all_sources_are_visible(self):
        service = MultiSourceHRRegistryViewService(
            self.settings,
            self.db,
        )
        rows = service.list_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["source_id"] for row in rows},
            {"one.ru", "two.ru"},
        )

    def test_source_filter(self):
        service = MultiSourceHRRegistryViewService(
            self.settings,
            self.db,
        )
        rows = service.list_rows(source_id="two.ru")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fio"], "Петров Петр")
        self.assertEqual(rows[0]["source_name"], "Компания 2")

    def test_summary_is_aggregated(self):
        service = MultiSourceHRRegistryViewService(
            self.settings,
            self.db,
        )
        summary = service.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["organizations"], 2)
        self.assertEqual(summary["source_name"], "Все организации")


if __name__ == "__main__":
    unittest.main()
