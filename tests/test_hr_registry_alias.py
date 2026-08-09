from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import HRSourceRecord
from app.services.hr_registry_alias import HRRegistryAliasService


class FakeSettings:
    zimbra_domains = ["one.ru", "two.ru"]
    zimbra_check_enabled = True
    zimbra_backend = "ssh"
    dry_run = False


class FakeZimbra:
    neighbor = SimpleNamespace(
        zimbra_id="mailbox-1",
        primary_email="ivanov@one.ru",
        login="ivanov",
        addresses=("ivanov@one.ru",),
    )
    conflicting = SimpleNamespace(
        zimbra_id="mailbox-2",
        primary_email="ivanov@two.ru",
        login="ivanov",
        addresses=("ivanov@two.ru",),
    )
    alias_mode = "free"

    def __init__(self, settings):
        self.settings = settings

    def accounts_by_addresses(self, emails):
        result = {}
        for email in emails:
            if email == "ivanov@one.ru":
                result[email] = self.neighbor
            elif email == "ivanov@two.ru":
                if self.alias_mode == "same":
                    result[email] = SimpleNamespace(
                        zimbra_id="mailbox-1",
                        primary_email="ivanov@one.ru",
                        login="ivanov",
                        addresses=("ivanov@one.ru", "ivanov@two.ru"),
                    )
                elif self.alias_mode == "conflict":
                    result[email] = self.conflicting
        return result


class AliasSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = FakeSettings()

        self.first = HRSourceRecord(
            worker_key="same-worker",
            source_id="one.ru",
            source_name="Компания 1",
            fio="Иванов Иван",
            corporate_email="ivanov@one.ru",
            personal_email="",
            login="ivanov",
            placements_json="[]",
            is_present=True,
        )
        self.second = HRSourceRecord(
            worker_key="same-worker",
            source_id="two.ru",
            source_name="Компания 2",
            fio="Иванов Иван",
            corporate_email="",
            personal_email="",
            login="",
            placements_json="[]",
            is_present=True,
        )
        self.db.add_all([self.first, self.second])
        self.db.commit()
        self.db.refresh(self.second)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.services.hr_registry_alias.ZimbraService", FakeZimbra)
    def test_free_alias_is_proposed_from_neighbor_mailbox(self):
        FakeZimbra.alias_mode = "free"
        items = HRRegistryAliasService(
            self.settings,
            self.db,
        ).suggestions()
        item = items[self.second.id]
        self.assertEqual(item["sibling_email"], "ivanov@one.ru")
        self.assertEqual(item["mailbox_zimbra_id"], "mailbox-1")
        self.assertEqual(item["proposed_alias"], "ivanov@two.ru")
        self.assertTrue(item["can_create"])

    @patch("app.services.hr_registry_alias.ZimbraService", FakeZimbra)
    def test_existing_alias_on_same_mailbox_can_be_bound(self):
        FakeZimbra.alias_mode = "same"
        item = HRRegistryAliasService(
            self.settings,
            self.db,
        ).suggestions()[self.second.id]
        self.assertTrue(item["alias_exists"])
        self.assertTrue(item["can_bind"])
        self.assertFalse(item["can_create"])

    @patch("app.services.hr_registry_alias.ZimbraService", FakeZimbra)
    def test_alias_owned_by_other_mailbox_is_blocked(self):
        FakeZimbra.alias_mode = "conflict"
        item = HRRegistryAliasService(
            self.settings,
            self.db,
        ).suggestions()[self.second.id]
        self.assertTrue(item["alias_conflict"])
        self.assertFalse(item["can_create"])
        self.assertFalse(item["can_bind"])

    @patch("app.services.hr_registry_alias.ZimbraService", FakeZimbra)
    def test_employee_with_own_hr_email_gets_no_alias_offer(self):
        FakeZimbra.alias_mode = "free"
        self.second.corporate_email = "ivanov@two.ru"
        self.db.commit()
        items = HRRegistryAliasService(
            self.settings,
            self.db,
        ).suggestions()
        self.assertNotIn(self.second.id, items)


if __name__ == "__main__":
    unittest.main()
