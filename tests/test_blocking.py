from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import BlockingOperation, EmailLoginMapping, HRSourceRecord
from app.services.ad import ADDirectoryUser
from app.services.blocking import BlockingCard, BlockingService
from app.services.itinvent import ITInventEmployeeAssets, ITInventEquipment
from app.services.zimbra import ZimbraAccountIdentity


class BlockingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.settings = Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef",
            onec_source_domain="org.ru",
            ad_check_enabled=True,
            zimbra_check_enabled=True,
            itinvent_enabled=True,
            itinvent_db_host="sql.local",
            itinvent_db_name="ITInvent",
            itinvent_db_username="reader",
            itinvent_db_password="secret",
        )
        self.record = HRSourceRecord(
            worker_key="worker-1",
            source_id="org.ru",
            source_name="org.ru",
            fio="Миронова Анна Сергеевна",
            corporate_email="mironova.as@org.ru",
            login="mironova.as",
            placements_json="[]",
            is_present=True,
        )
        self.db.add(self.record)
        self.db.add(
            EmailLoginMapping(
                worker_key="worker-1",
                source_domain="org.ru",
                source_email="mironova.as@org.ru",
                ad_object_guid="11111111-1111-1111-1111-111111111111",
                ad_login="ahmetova.as",
                zimbra_id="zimbra-1",
                zimbra_email="ahmetova.as@org.ru",
                created_by="admin",
            )
        )
        self.db.commit()
        self.db.refresh(self.record)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_worker_lock_is_shared_between_instances(self):
        first = BlockingService.__new__(BlockingService)
        second = BlockingService.__new__(BlockingService)
        lock1 = first._lock_for("worker-1")
        lock2 = second._lock_for("WORKER-1")
        self.assertIs(lock1, lock2)

    @patch("app.services.blocking.ITInventService")
    @patch("app.services.blocking.ZimbraService")
    @patch("app.services.blocking.ActiveDirectoryService")
    def test_changed_surname_still_uses_confirmed_ad_login_for_itinvent(
        self,
        ad_cls,
        zimbra_cls,
        itinvent_cls,
    ):
        ad_cls.return_value.get_user_by_object_guid.return_value = ADDirectoryUser(
            username="ahmetova.as",
            display_name="Ахметова Анна Сергеевна",
            email="ahmetova.as@org.ru",
            distinguished_name="CN=Ахметова,DC=local,DC=dmn",
            is_enabled=True,
            object_guid="11111111-1111-1111-1111-111111111111",
        )
        zimbra_cls.return_value.accounts_by_ids.return_value = {
            "zimbra-1": ZimbraAccountIdentity(
                zimbra_id="zimbra-1",
                primary_email="ahmetova.as@org.ru",
                login="ahmetova.as",
                addresses=(
                    "ahmetova.as@org.ru",
                    "mironova.as@org.ru",
                ),
                account_status="active",
            )
        }
        assets = ITInventEmployeeAssets(
            owner_found=True,
            owner_display_name="Ахметова Анна Сергеевна",
            owner_login="ahmetova.as",
            equipment=(
                ITInventEquipment(
                    equipment_type="Ноутбук",
                    equipment_name="Lenovo ThinkPad T14",
                    serial_number="SN-1",
                    inventory_number="12345",
                    accounting_inventory_number="BUH-42",
                ),
            ),
        )
        itinvent_cls.return_value.configured = True
        itinvent_cls.return_value.equipment_for_login.return_value = assets

        card = BlockingService(self.settings, self.db).card(self.record.id)

        self.assertEqual(card.fio, "Миронова Анна Сергеевна")
        self.assertEqual(card.ad_user.display_name, "Ахметова Анна Сергеевна")
        self.assertEqual(card.effective_login, "ahmetova.as")
        itinvent_cls.return_value.equipment_for_login.assert_called_once_with(
            "ahmetova.as",
            location_nos=("24",),
            equipment_types=(),
        )
        self.assertEqual(card.itinvent.owner_display_name, "Ахметова Анна Сергеевна")
        self.assertEqual(card.itinvent.equipment[0].equipment_type, "Ноутбук")


    @patch("app.services.blocking.ITInventService")
    @patch("app.services.blocking.ZimbraService")
    @patch("app.services.blocking.ActiveDirectoryService")
    def test_itinvent_refresh_uses_mapping_without_rereading_ad(
        self,
        ad_cls,
        zimbra_cls,
        itinvent_cls,
    ):
        assets = ITInventEmployeeAssets(
            owner_found=True,
            owner_display_name="Ахметова Анна Сергеевна",
            owner_login="ahmetova.as",
            equipment=(),
        )
        itinvent_cls.return_value.configured = True
        itinvent_cls.return_value.equipment_for_login.return_value = assets

        result = BlockingService(self.settings, self.db).refresh_itinvent(
            self.record.id
        )

        self.assertEqual(result.state, "found")
        self.assertEqual(result.effective_login, "ahmetova.as")
        itinvent_cls.return_value.equipment_for_login.assert_called_once_with(
            "ahmetova.as",
            location_nos=("24",),
            equipment_types=(),
        )
        ad_cls.assert_not_called()
        zimbra_cls.assert_not_called()


    @patch.object(BlockingService, "card")
    @patch("app.services.blocking_queue.ZimbraService")
    @patch("app.services.blocking_queue.ActiveDirectoryService")
    def test_block_disables_ad_and_closes_zimbra(
        self,
        ad_cls,
        zimbra_cls,
        card_mock,
    ):
        settings = self.settings.model_copy(update={"dry_run": False})
        ad_user = ADDirectoryUser(
            username="ahmetova.as",
            display_name="Ахметова Анна Сергеевна",
            email="ahmetova.as@org.ru",
            distinguished_name="CN=Ахметова,DC=local,DC=dmn",
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
        assets = ITInventEmployeeAssets(
            owner_found=True,
            owner_display_name="Ахметова Анна Сергеевна",
            owner_login="ahmetova.as",
            equipment=(),
        )
        card_mock.return_value = BlockingCard(
            record_id=self.record.id,
            worker_key="worker-1",
            source_id="org.ru",
            fio="Миронова Анна Сергеевна",
            corporate_email="mironova.as@org.ru",
            placements=(),
            effective_login="ahmetova.as",
            ad_target_guid="11111111-1111-1111-1111-111111111111",
            zimbra_target_id="zimbra-1",
            zimbra_target_email="ahmetova.as@org.ru",
            ad_user=ad_user,
            ad_error="",
            zimbra=zimbra,
            zimbra_error="",
            zimbra_status_label="Активна",
            itinvent=assets,
            itinvent_state="found",
            itinvent_error="",
            itinvent_checked_at="",
        )

        ad_cls.return_value.get_user_by_object_guid.return_value = ad_user
        zimbra_cls.return_value.accounts_by_ids.return_value = {"zimbra-1": zimbra}

        result = BlockingService(settings, self.db).block(
            self.record.id,
            "operator",
        )

        ad_cls.return_value.disable_user.assert_called_once_with("ahmetova.as")
        zimbra_cls.return_value.close_account.assert_called_once_with(
            "ahmetova.as@org.ru"
        )
        self.assertEqual(result.status, "success")
        operation = self.db.query(BlockingOperation).one()
        self.assertTrue(operation.ad_disabled)
        self.assertTrue(operation.zimbra_locked)
        self.assertEqual(operation.login, "ahmetova.as")


if __name__ == "__main__":
    unittest.main()
