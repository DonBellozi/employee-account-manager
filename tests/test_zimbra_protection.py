from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models_zimbra_protection import (
    ZimbraProtectedAccount,
    ZimbraProtectionEvent,
    ZimbraProtectionMigration,
)
from app.services.zimbra_protection import (
    ManagedObservedZimbraAccount,
    ManagedZimbraObserverService,
)


FIXED_NOW = datetime(2026, 8, 9, 5, 30, tzinfo=timezone.utc)


class ZimbraProtectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = Settings(
            app_secret_key="test-secret-key-1234567890",
            zimbra_domains=["domain.com"],
            zimbra_primary_domain="domain.com",
        )
        self.service = ManagedZimbraObserverService(self.settings, self.db)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def account(
        *,
        email: str = "service@domain.com",
        zimbra_id: str = "zimbra-1",
        display_name: str = "Служебный ящик",
        note: str = "never_disable",
    ) -> ManagedObservedZimbraAccount:
        return ManagedObservedZimbraAccount(
            zimbra_id=zimbra_id,
            primary_email=email,
            addresses=(email,),
            account_status="active",
            last_logon_at=FIXED_NOW - timedelta(days=500),
            created_at=FIXED_NOW - timedelta(days=1000),
            note=note,
            display_name=display_name,
        )

    def test_parser_reads_display_name(self):
        output = """# name service@domain.com
mail: service@domain.com
zimbraId: zimbra-1
zimbraAccountStatus: active
displayName: Служебный ящик
zimbraNotes: never_disable
"""
        rows = self.service._parse_gaa_verbose(output)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].display_name, "Служебный ящик")
        self.assertEqual(rows[0].primary_email, "service@domain.com")

    def test_initial_import_stores_display_name_email_and_stable_id(self):
        observed = self.account()
        with patch.object(self.service, "_fetch_accounts", return_value=[observed]), patch(
            "app.services.zimbra_protection.utcnow", return_value=FIXED_NOW
        ):
            result = self.service.import_legacy_never_disable("admin")

        self.assertEqual(result["imported"], 1)
        row = self.db.scalars(select(ZimbraProtectedAccount)).one()
        self.assertEqual(row.zimbra_id, "zimbra-1")
        self.assertEqual(row.display_name, "Служебный ящик")
        self.assertEqual(row.primary_email, "service@domain.com")
        self.assertTrue(row.is_active)
        self.assertEqual(row.source, "legacy_zimbra_notes")
        self.assertIsNotNone(self.db.get(ZimbraProtectionMigration, 1))

    def test_import_writes_audit_event(self):
        with patch.object(
            self.service, "_fetch_accounts", return_value=[self.account()]
        ):
            self.service.import_legacy_never_disable("admin")
        event = self.db.scalars(select(ZimbraProtectionEvent)).one()
        self.assertEqual(event.action, "imported")
        self.assertEqual(event.display_name, "Служебный ящик")
        self.assertEqual(event.primary_email, "service@domain.com")

    def test_removed_web_protection_is_not_reenabled_by_repeat_import(self):
        row = ZimbraProtectedAccount(
            zimbra_id="zimbra-1",
            primary_email="service@domain.com",
            display_name="Служебный ящик",
            source="legacy_zimbra_notes",
            reason="legacy",
            is_active=False,
            activated_by="admin",
            activated_at=FIXED_NOW - timedelta(days=1),
            deactivated_by="admin",
            deactivated_at=FIXED_NOW,
        )
        self.db.add(row)
        self.db.add(
            ZimbraProtectionMigration(
                id=1,
                completed_at=FIXED_NOW - timedelta(days=1),
                completed_by="admin",
                last_import_at=FIXED_NOW - timedelta(days=1),
                last_import_by="admin",
            )
        )
        self.db.commit()

        with patch.object(self.service, "_fetch_accounts", return_value=[self.account()]):
            result = self.service.import_legacy_never_disable("admin")

        self.assertEqual(result["inactive_skipped"], 1)
        self.db.refresh(row)
        self.assertFalse(row.is_active)

    def test_after_migration_legacy_note_alone_no_longer_protects(self):
        self.db.add(
            ZimbraProtectionMigration(
                id=1,
                completed_at=FIXED_NOW,
                completed_by="admin",
                last_import_at=FIXED_NOW,
                last_import_by="admin",
            )
        )
        self.db.commit()
        self.service._load_protection_cache()
        config = self.service.get_settings_record()
        evaluation = self.service._evaluate(
            self.account(note="never_disable"),
            previous_state=None,
            config=config,
            hr=type("HR", (), {
                "emails": frozenset(),
                "snapshot_at": FIXED_NOW,
                "age_minutes": 0,
                "fresh": True,
                "records_count": 1,
            })(),
            dismissal_map={},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "close")

    def test_active_web_protection_has_priority(self):
        self.db.add(
            ZimbraProtectedAccount(
                zimbra_id="zimbra-1",
                primary_email="service@domain.com",
                display_name="Служебный ящик",
                source="manual",
                reason="Служебный ящик",
                is_active=True,
                activated_by="admin",
                activated_at=FIXED_NOW,
            )
        )
        self.db.add(
            ZimbraProtectionMigration(
                id=1,
                completed_at=FIXED_NOW,
                completed_by="admin",
                last_import_at=FIXED_NOW,
                last_import_by="admin",
            )
        )
        self.db.commit()
        self.service._load_protection_cache()
        config = self.service.get_settings_record()
        evaluation = self.service._evaluate(
            self.account(note=""),
            previous_state=None,
            config=config,
            hr=type("HR", (), {
                "emails": frozenset(),
                "snapshot_at": FIXED_NOW,
                "age_minutes": 0,
                "fresh": True,
                "records_count": 1,
            })(),
            dismissal_map={"service@domain.com": FIXED_NOW.date()},
            now=FIXED_NOW,
            local_today=FIXED_NOW.date(),
        )
        self.assertEqual(evaluation.recommendation, "protected_note")
        self.assertIn("защищена в Web", evaluation.reason)

    def test_metadata_refresh_follows_same_zimbra_id_after_email_rename(self):
        row = ZimbraProtectedAccount(
            zimbra_id="zimbra-1",
            primary_email="old@domain.com",
            display_name="Старое имя",
            source="manual",
            reason="service",
            is_active=True,
            activated_by="admin",
            activated_at=FIXED_NOW,
        )
        self.db.add(row)
        self.db.commit()
        changed = self.account(email="new@domain.ru", display_name="Новое имя")
        with patch("app.services.zimbra_protection.utcnow", return_value=FIXED_NOW):
            self.service._refresh_protection_metadata([changed])
        self.db.refresh(row)
        self.assertEqual(row.primary_email, "new@domain.ru")
        self.assertEqual(row.display_name, "Новое имя")
        self.assertEqual(row.zimbra_id, "zimbra-1")


if __name__ == "__main__":
    unittest.main()
