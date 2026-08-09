from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import BlockingOperation, BlockingQueueItem, OperationStatus
from app.services.ad import ADDirectoryUser
from app.services.blocking_queue import BlockingQueueService
from app.services.zimbra import ZimbraAccountIdentity


class BlockingQueueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.settings = Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef",
            ad_check_enabled=True,
            zimbra_check_enabled=True,
            dry_run=False,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _operation(self) -> BlockingOperation:
        operation = BlockingOperation(
            worker_key="worker-1",
            source_id="org.ru",
            source_record_id=1,
            operator_username="operator",
            full_name="Миронова Анна Сергеевна",
            login="ahmetova.as",
            corporate_email="mironova.as@org.ru",
            status=OperationStatus.RUNNING,
            dry_run=False,
        )
        self.db.add(operation)
        self.db.flush()
        self.db.add_all(
            [
                BlockingQueueItem(
                    operation_id=operation.id,
                    system="ad",
                    target_identifier="ahmetova.as",
                    stable_id="11111111-1111-1111-1111-111111111111",
                    desired_state="disabled",
                    status="pending",
                    next_attempt_at=datetime.now(timezone.utc),
                ),
                BlockingQueueItem(
                    operation_id=operation.id,
                    system="zimbra",
                    target_identifier="ahmetova.as@org.ru",
                    stable_id="zimbra-1",
                    desired_state="closed",
                    status="pending",
                    next_attempt_at=datetime.now(timezone.utc),
                ),
            ]
        )
        self.db.commit()
        return operation

    @patch("app.services.blocking_queue.ZimbraService")
    @patch("app.services.blocking_queue.ActiveDirectoryService")
    def test_active_accounts_are_disabled_and_closed(self, ad_cls, zimbra_cls):
        operation = self._operation()
        ad_user = ADDirectoryUser(
            username="ahmetova.as",
            display_name="Ахметова Анна Сергеевна",
            email="ahmetova.as@org.ru",
            distinguished_name="CN=A,DC=local,DC=dmn",
            is_enabled=True,
            object_guid="11111111-1111-1111-1111-111111111111",
        )
        zimbra = ZimbraAccountIdentity(
            zimbra_id="zimbra-1",
            primary_email="ahmetova.as@org.ru",
            login="ahmetova.as",
            addresses=("ahmetova.as@org.ru",),
            account_status="active",
        )
        ad_cls.return_value.get_user_by_object_guid.return_value = ad_user
        zimbra_cls.return_value.accounts_by_ids.return_value = {"zimbra-1": zimbra}

        view = BlockingQueueService(self.settings, self.db).process_operation(
            operation.id,
            force=True,
        )

        ad_cls.return_value.disable_user.assert_called_once_with("ahmetova.as")
        zimbra_cls.return_value.close_account.assert_called_once_with(
            "ahmetova.as@org.ru"
        )
        self.assertEqual(view.status, "success")
        self.assertEqual(view.ad.status, "completed")
        self.assertEqual(view.zimbra.status, "completed")
        self.db.refresh(operation)
        self.assertTrue(operation.ad_disabled)
        self.assertTrue(operation.zimbra_locked)

    @patch("app.services.blocking_queue.ZimbraService")
    @patch("app.services.blocking_queue.ActiveDirectoryService")
    def test_manual_server_blocking_is_accepted_without_duplicate_command(
        self,
        ad_cls,
        zimbra_cls,
    ):
        operation = self._operation()
        ad_cls.return_value.get_user_by_object_guid.return_value = ADDirectoryUser(
            username="ahmetova.as",
            display_name="Ахметова Анна Сергеевна",
            email="ahmetova.as@org.ru",
            distinguished_name="CN=A,DC=local,DC=dmn",
            is_enabled=False,
            object_guid="11111111-1111-1111-1111-111111111111",
        )
        zimbra_cls.return_value.accounts_by_ids.return_value = {
            "zimbra-1": ZimbraAccountIdentity(
                zimbra_id="zimbra-1",
                primary_email="ahmetova.as@org.ru",
                login="ahmetova.as",
                addresses=("ahmetova.as@org.ru",),
                account_status="closed",
            )
        }

        view = BlockingQueueService(self.settings, self.db).process_operation(
            operation.id,
            force=True,
        )

        ad_cls.return_value.disable_user.assert_not_called()
        zimbra_cls.return_value.close_account.assert_not_called()
        self.assertEqual(view.status, "success")
        self.assertEqual(view.ad.status, "already_completed")
        self.assertEqual(view.zimbra.status, "already_completed")
        self.assertIn("уже была заблокирована", view.ad.status_label)
        self.assertIn("уже была закрыта", view.zimbra.status_label)

    @patch("app.services.blocking_queue.ZimbraService")
    @patch("app.services.blocking_queue.ActiveDirectoryService")
    def test_network_failure_stays_pending_for_automatic_retry(
        self,
        ad_cls,
        zimbra_cls,
    ):
        operation = self._operation()
        ad_cls.return_value.get_user_by_object_guid.side_effect = RuntimeError(
            "connection refused"
        )
        zimbra_cls.return_value.accounts_by_ids.side_effect = RuntimeError(
            "timed out"
        )

        view = BlockingQueueService(self.settings, self.db).process_operation(
            operation.id,
            force=True,
        )

        self.assertEqual(view.status, "running")
        self.assertEqual(view.ad.status, "pending")
        self.assertEqual(view.zimbra.status, "pending")
        self.assertIsNotNone(view.ad.next_attempt_at)
        self.assertIsNotNone(view.zimbra.next_attempt_at)

    @patch("app.services.blocking_queue.ZimbraService")
    @patch("app.services.blocking_queue.ActiveDirectoryService")
    def test_not_found_requires_intervention(self, ad_cls, zimbra_cls):
        operation = self._operation()
        ad_cls.return_value.get_user_by_object_guid.return_value = None
        ad_cls.return_value.get_user.return_value = None
        zimbra_cls.return_value.accounts_by_ids.return_value = {}
        zimbra_cls.return_value.account_by_address.return_value = None

        view = BlockingQueueService(self.settings, self.db).process_operation(
            operation.id,
            force=True,
        )

        self.assertEqual(view.status, "failed")
        self.assertEqual(view.ad.status, "intervention")
        self.assertEqual(view.zimbra.status, "intervention")
        self.assertIsNone(view.ad.next_attempt_at)
        self.assertIsNone(view.zimbra.next_attempt_at)

    @patch("app.services.blocking_queue.ZimbraService")
    @patch("app.services.blocking_queue.ActiveDirectoryService")
    def test_manual_retry_can_finish_previous_temporary_failure(
        self,
        ad_cls,
        zimbra_cls,
    ):
        operation = self._operation()
        ad_user = ADDirectoryUser(
            username="ahmetova.as",
            display_name="Ахметова Анна Сергеевна",
            email="ahmetova.as@org.ru",
            distinguished_name="CN=A,DC=local,DC=dmn",
            is_enabled=True,
            object_guid="11111111-1111-1111-1111-111111111111",
        )
        zimbra = ZimbraAccountIdentity(
            zimbra_id="zimbra-1",
            primary_email="ahmetova.as@org.ru",
            login="ahmetova.as",
            addresses=("ahmetova.as@org.ru",),
            account_status="active",
        )
        ad_cls.return_value.get_user_by_object_guid.side_effect = [
            RuntimeError("connection refused"),
            ad_user,
        ]
        zimbra_cls.return_value.accounts_by_ids.side_effect = [
            RuntimeError("timed out"),
            {"zimbra-1": zimbra},
        ]

        service = BlockingQueueService(self.settings, self.db)
        first = service.process_operation(operation.id, force=True)
        self.assertEqual(first.status, "running")

        second = service.process_operation(operation.id, force=True)
        self.assertEqual(second.status, "success")
        self.assertEqual(second.ad.status, "completed")
        self.assertEqual(second.zimbra.status, "completed")


if __name__ == "__main__":
    unittest.main()
