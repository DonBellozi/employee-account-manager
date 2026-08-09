from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models_zimbra_lifecycle import (
    ZimbraLifecycleAction,
    ZimbraLifecycleSettings,
)
from app.models_zimbra_observer import ZimbraLifecycleState
from app.models_zimbra_protection import ZimbraProtectedAccount
from app.services.zimbra_lifecycle import BackupResult, ZimbraLifecycleService


class ZimbraLifecycleTests(unittest.TestCase):
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
            dry_run=False,
            zimbra_check_enabled=True,
            zimbra_domains=["domain.com"],
            zimbra_primary_domain="domain.com",
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self, settings=None):
        return ZimbraLifecycleService(settings or self.settings, self.db)

    def add_state(self, email, recommendation, status):
        row = ZimbraLifecycleState(
            account_key=f"id-{email}",
            zimbra_id=f"zid-{email}",
            primary_email=email,
            account_status=status,
            recommendation=recommendation,
            reason="test",
        )
        self.db.add(row)
        self.db.commit()
        return row

    def fake_observer_run(self):
        return SimpleNamespace(id=77, status="success", error_message="")

    def test_defaults_are_safe(self):
        row = self.service().get_settings_record()
        self.assertFalse(row.allow_close)
        self.assertFalse(row.allow_backup)
        self.assertFalse(row.allow_delete)
        self.assertEqual(row.backup_dir, "/opt/tmp")

    def test_delete_requires_backup(self):
        with self.assertRaisesRegex(ValueError, "без резервного копирования"):
            self.service().save_settings(
                allow_close=False,
                allow_backup=False,
                allow_delete=True,
                backup_dir="/opt/tmp",
                operator="admin",
            )

    def test_plan_uses_only_actionable_observer_states(self):
        self.add_state("close@domain.com", "close", "active")
        self.add_state("old@domain.com", "archive_delete", "closed")
        self.add_state("protected@domain.com", "protected_hr", "active")
        service = self.service()
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ):
            run = service.build_plan("admin")

        self.assertEqual(run.status, "success")
        self.assertEqual(run.planned_close, 1)
        self.assertEqual(run.planned_archive, 1)
        actions = service.run_actions(run.id)
        self.assertEqual(len(actions), 2)
        self.assertEqual({a.action for a in actions}, {"close", "backup_delete"})

    def test_disabled_close_permission_never_calls_zimbra(self):
        self.add_state("close@domain.com", "close", "active")
        service = self.service()
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ), patch.object(service, "_current_status") as status, patch(
            "app.services.zimbra_lifecycle.ZimbraService.close_account"
        ) as close:
            run = service.execute("admin")

        status.assert_not_called()
        close.assert_not_called()
        self.assertEqual(run.skipped_count, 1)

    def test_allowed_close_is_verified(self):
        self.add_state("close@domain.com", "close", "active")
        service = self.service()
        service.save_settings(
            allow_close=True,
            allow_backup=False,
            allow_delete=False,
            backup_dir="/opt/tmp",
            operator="admin",
        )
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ), patch.object(
            service, "_current_status", side_effect=["active", "closed"]
        ), patch(
            "app.services.zimbra_lifecycle.ZimbraService.close_account"
        ) as close:
            run = service.execute("admin")

        close.assert_called_once_with("close@domain.com")
        self.assertEqual(run.closed_success, 1)
        action = service.run_actions(run.id)[0]
        self.assertEqual(action.status, "success")

    def test_archive_delete_requires_successful_backup(self):
        self.add_state("old@domain.com", "archive_delete", "closed")
        service = self.service()
        service.save_settings(
            allow_close=False,
            allow_backup=True,
            allow_delete=True,
            backup_dir="/opt/tmp",
            operator="admin",
        )
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ), patch.object(
            service, "_current_status", return_value="closed"
        ), patch.object(
            service, "_backup_account", side_effect=RuntimeError("backup failed")
        ), patch(
            "app.services.zimbra_lifecycle.ZimbraService.delete_account"
        ) as delete:
            run = service.execute("admin")

        delete.assert_not_called()
        self.assertEqual(run.backup_success, 0)
        self.assertEqual(run.delete_success, 0)
        self.assertEqual(run.failed_count, 1)

    def test_successful_backup_then_delete_is_verified(self):
        self.add_state("old@domain.com", "archive_delete", "closed")
        service = self.service()
        service.save_settings(
            allow_close=False,
            allow_backup=True,
            allow_delete=True,
            backup_dir="/opt/tmp",
            operator="admin",
        )
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ), patch.object(
            service, "_current_status", side_effect=["closed", "closed", None]
        ), patch.object(
            service,
            "_backup_account",
            return_value=BackupResult("/opt/tmp/old.tgz", 12345),
        ), patch(
            "app.services.zimbra_lifecycle.ZimbraService.delete_account"
        ) as delete:
            run = service.execute("admin")

        delete.assert_called_once_with("old@domain.com")
        self.assertEqual(run.backup_success, 1)
        self.assertEqual(run.delete_success, 1)
        actions = service.run_actions(run.id)
        self.assertEqual([a.action for a in actions], ["backup", "delete"])
        self.assertTrue(all(a.status == "success" for a in actions))

    def test_web_protection_recheck_blocks_mutation(self):
        state = self.add_state("close@domain.com", "close", "active")
        self.db.add(
            ZimbraProtectedAccount(
                zimbra_id=state.zimbra_id,
                primary_email=state.primary_email,
                display_name="Protected",
                source="manual",
                reason="test",
                is_active=True,
                activated_by="admin",
            )
        )
        self.db.commit()
        service = self.service()
        service.save_settings(
            allow_close=True,
            allow_backup=False,
            allow_delete=False,
            backup_dir="/opt/tmp",
            operator="admin",
        )
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ), patch.object(service, "_current_status") as status, patch(
            "app.services.zimbra_lifecycle.ZimbraService.close_account"
        ) as close:
            run = service.execute("admin")
        status.assert_not_called()
        close.assert_not_called()
        self.assertEqual(run.skipped_count, 1)

    def test_global_dry_run_never_mutates(self):
        self.add_state("close@domain.com", "close", "active")
        dry = self.settings.model_copy(update={"dry_run": True})
        service = self.service(dry)
        service.save_settings(
            allow_close=True,
            allow_backup=True,
            allow_delete=True,
            backup_dir="/opt/tmp",
            operator="admin",
        )
        with patch(
            "app.services.zimbra_lifecycle.ManagedZimbraObserverService.run",
            return_value=self.fake_observer_run(),
        ), patch.object(service, "_current_status") as status, patch(
            "app.services.zimbra_lifecycle.ZimbraService.close_account"
        ) as close:
            run = service.execute("admin")
        status.assert_not_called()
        close.assert_not_called()
        self.assertEqual(run.closed_success, 0)
        self.assertEqual(run.skipped_count, 1)

    def test_old_app_volume_path_is_migrated_to_zimbra_tmp(self):
        row = ZimbraLifecycleSettings(
            id=1,
            allow_close=False,
            allow_backup=False,
            allow_delete=False,
            backup_dir="/app/data/zimbra-backups",
        )
        self.db.add(row)
        self.db.commit()
        loaded = self.service().get_settings_record()
        self.assertEqual(loaded.backup_dir, "/opt/tmp")

    def test_backup_directory_is_remote_absolute_path(self):
        self.assertEqual(
            self.service()._normalize_backup_dir("/opt/tmp"),
            "/opt/tmp",
        )
        with self.assertRaisesRegex(ValueError, "абсолютным"):
            self.service()._normalize_backup_dir("opt/tmp")
        with self.assertRaisesRegex(ValueError, "Корневой"):
            self.service()._normalize_backup_dir("/")


if __name__ == "__main__":
    unittest.main()
