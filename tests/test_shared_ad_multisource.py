from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import OneCAdditionalSource
from app.services.hr_registry_multisource import (
    MultiSourceHRRegistryViewService,
)


class FakeSettings:
    onec_source_domain = "one.ru"
    zimbra_domains = ["one.ru", "two.ru"]
    ad_check_enabled = True

    def model_copy(self, deep=False):
        copied = FakeSettings()
        copied.onec_source_domain = self.onec_source_domain
        copied.zimbra_domains = list(self.zimbra_domains)
        copied.ad_check_enabled = self.ad_check_enabled
        return copied


class FakeAD:
    def __init__(self, settings):
        self.settings = settings

    def users_by_logins(self, logins):
        return {
            login: SimpleNamespace(
                username=login,
                is_enabled=True,
                object_guid=f"guid-{login}",
            )
            for login in logins
            if login == "ivanov"
        }

    def users_by_object_guids(self, guids):
        return {}


class SharedADTests(unittest.TestCase):
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
                sender_filter="",
                attachment_filename="one.xlsx",
                is_primary=True,
                enabled=True,
            ),
            OneCAdditionalSource(
                name="Компания 2",
                mail_domain="two.ru",
                imap_folder="INBOX",
                sender_filter="",
                attachment_filename="two.xlsx",
                is_primary=False,
                enabled=True,
            ),
            HRSourceRecord(
                worker_key="same-worker",
                source_id="one.ru",
                source_name="Компания 1",
                fio="Иванов Иван",
                corporate_email="ivanov@one.ru",
                personal_email="",
                login="ivanov",
                placements_json="[]",
                is_present=True,
                ad_status="enabled",
                zimbra_status="present",
                reconciliation_status="ok",
            ),
            HRSourceRecord(
                worker_key="same-worker",
                source_id="two.ru",
                source_name="Компания 2",
                fio="Иванов Иван",
                corporate_email="",
                personal_email="",
                login="",
                placements_json="[]",
                is_present=True,
                ad_status="no_login",
                zimbra_status="no_email",
                reconciliation_status="issue",
            ),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_worker_key_resolves_shared_ad_login(self):
        service = MultiSourceHRRegistryViewService(
            self.settings,
            self.db,
        )
        with patch(
            "app.services.hr_registry_multisource.ActiveDirectoryService",
            FakeAD,
        ):
            resolved = service._resolve_shared_ad_for_source("two.ru")

        self.assertEqual(resolved, 1)
        second = self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == "two.ru"
            )
        )
        self.assertEqual(second.login, "ivanov")
        self.assertEqual(second.ad_status, "enabled")
        self.assertEqual(second.zimbra_status, "no_email")
        self.assertEqual(second.reconciliation_status, "issue")

    def test_conflicting_sibling_logins_are_not_guessed(self):
        self.db.add(
            HRSourceRecord(
                worker_key="same-worker",
                source_id="three.ru",
                source_name="Компания 3",
                fio="Иванов Иван",
                corporate_email="other@three.ru",
                personal_email="",
                login="other",
                placements_json="[]",
                is_present=True,
                ad_status="enabled",
                zimbra_status="present",
                reconciliation_status="ok",
            )
        )
        self.db.commit()

        service = MultiSourceHRRegistryViewService(
            self.settings,
            self.db,
        )
        hints = service._shared_ad_hints(
            "two.ru",
            ["same-worker"],
        )
        self.assertNotIn("same-worker", hints)


if __name__ == "__main__":
    unittest.main()
