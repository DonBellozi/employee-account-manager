from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.services.itinvent import ITInventEquipmentType, ITInventLocation
from app.services.itinvent_control import ITInventControlService


class ITInventControlTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.settings = Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef",
            itinvent_enabled=True,
            itinvent_db_host="sql.local",
            itinvent_db_name="ITInvent",
            itinvent_db_username="reader",
            itinvent_db_password="secret",
            itinvent_issued_location_no=24,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_fallback_preserves_location_24_until_web_save(self):
        selection = ITInventControlService(self.settings, self.db).load()
        self.assertFalse(selection.persisted)
        self.assertEqual(selection.location_nos, ("24",))
        self.assertEqual(selection.locations[0].description, "Выданы в пользование")
        self.assertEqual(selection.equipment_type_keys, ())

    @patch("app.services.itinvent_control.ITInventService")
    def test_web_save_persists_locations_and_always_controlled_types(self, service_cls):
        service_cls.return_value.list_locations.return_value = (
            ITInventLocation("24", "Выданы в пользование"),
            ITInventLocation("7", "Склад"),
        )
        service_cls.return_value.list_equipment_types.return_value = (
            ITInventEquipmentType("2", "1", "Ноутбук"),
            ITInventEquipmentType("3", "1", "Монитор"),
        )

        service = ITInventControlService(self.settings, self.db)
        saved = service.save_from_keys(
            location_keys=["24"],
            type_keys=["2|1"],
            operator="admin",
        )
        loaded = service.load()

        self.assertTrue(saved.persisted)
        self.assertEqual(saved.location_nos, ("24",))
        self.assertEqual(saved.equipment_type_keys, (("2", "1"),))
        self.assertTrue(loaded.persisted)
        self.assertEqual(loaded.locations[0].description, "Выданы в пользование")
        self.assertEqual(loaded.equipment_types[0].type_name, "Ноутбук")

    @patch("app.services.itinvent_control.ITInventService")
    def test_catalog_payload_returns_compact_keys_and_current_selection(self, service_cls):
        service_cls.return_value.list_locations.return_value = (
            ITInventLocation("24", "Выданы в пользование"),
        )
        service_cls.return_value.list_equipment_types.return_value = (
            ITInventEquipmentType("2", "1", "Ноутбук"),
        )
        payload = ITInventControlService(self.settings, self.db).catalog_payload()
        self.assertEqual(payload["locations"][0]["key"], "24")
        self.assertEqual(payload["types"][0]["key"], "2|1")
        self.assertEqual(payload["selected_locations"], ["24"])
        self.assertEqual(payload["selected_types"], [])


if __name__ == "__main__":
    unittest.main()
