from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.services.hr_registry_manual_mapping import (
    HRRegistryManualMappingService,
)


class FakeSettings:
    zimbra_domains = ["one.ru", "two.ru"]
    zimbra_check_enabled = True
    zimbra_backend = "ssh"


class FakeAD:
    def __init__(self, settings):
        pass

    def get_user(self, login):
        if login != "ivanov":
            return None
        return SimpleNamespace(
            username="ivanov",
            email="ivanov@one.ru",
            object_guid="11111111-1111-1111-1111-111111111111",
            is_enabled=True,
        )

    def users_by_email(self, email, limit=10):
        if email in {"ivanov@one.ru", "ivanov@two.ru"}:
            return [self.get_user("ivanov")]
        return []


class FakeZimbra:
    def __init__(self, settings):
        pass

    @staticmethod
    def identity():
        return SimpleNamespace(
            zimbra_id="zimbra-1",
            primary_email="ivanov@one.ru",
            login="ivanov",
            addresses=("ivanov@one.ru", "ivanov@two.ru"),
        )

    def account_by_address(self, email):
        if email in self.identity().addresses:
            return self.identity()
        return None

    def accounts_by_addresses(self, emails):
        result = {}
        for email in emails:
            if email in self.identity().addresses:
                result[email] = self.identity()
        return result


class ManualMappingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.record = HRSourceRecord(
            worker_key="worker",
            source_id="two.ru",
            source_name="Компания 2",
            fio="Иванов Иван",
            corporate_email="",
            personal_email="",
            login="",
            placements_json="[]",
            is_present=True,
        )
        self.db.add(self.record)
        self.db.commit()
        self.db.refresh(self.record)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self):
        return HRRegistryManualMappingService(
            FakeSettings(),
            self.db,
        )

    @patch("app.services.hr_registry_manual_mapping.ZimbraService", FakeZimbra)
    @patch("app.services.hr_registry_manual_mapping.ActiveDirectoryService", FakeAD)
    def test_login_only_creates_full_mapping(self):
        result = self.service().save_identifier(
            record_id=self.record.id,
            identifier="ivanov",
            actor="admin",
        )
        self.assertTrue(result["has_ad"])
        self.assertTrue(result["has_zimbra"])
        mapping = self.db.scalar(select(EmailLoginMapping))
        self.assertEqual(mapping.ad_login, "ivanov")
        self.assertEqual(mapping.zimbra_id, "zimbra-1")

    @patch("app.services.hr_registry_manual_mapping.ZimbraService", FakeZimbra)
    @patch("app.services.hr_registry_manual_mapping.ActiveDirectoryService", FakeAD)
    def test_email_only_creates_full_mapping(self):
        result = self.service().save_identifier(
            record_id=self.record.id,
            identifier="ivanov@two.ru",
            actor="admin",
        )
        self.assertTrue(result["has_ad"])
        self.assertTrue(result["has_zimbra"])
        mapping = self.db.scalar(select(EmailLoginMapping))
        self.assertEqual(mapping.source_email, "ivanov@two.ru")
        self.assertEqual(mapping.ad_login, "ivanov")

    @patch("app.services.hr_registry_manual_mapping.ZimbraService", FakeZimbra)
    @patch("app.services.hr_registry_manual_mapping.ActiveDirectoryService", FakeAD)
    def test_mapping_is_bound_to_exact_hr_record_source(self):
        self.service().save_identifier(
            record_id=self.record.id,
            identifier="ivanov",
            actor="admin",
        )
        mapping = self.db.scalar(select(EmailLoginMapping))
        self.assertEqual(mapping.worker_key, "worker")
        self.assertEqual(mapping.source_domain, "two.ru")


if __name__ == "__main__":
    unittest.main()
