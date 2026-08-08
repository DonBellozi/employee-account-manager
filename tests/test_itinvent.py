from __future__ import annotations

import sys
import types
import unittest
from decimal import Decimal

from app.config import Settings
from app.services.itinvent import ITInventService


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []
        self.current = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.current = self.responses.pop(0)

    def fetchall(self):
        return list(self.current)

    def fetchone(self):
        return self.current[0] if self.current else None


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ITInventTests(unittest.TestCase):
    def settings(self):
        return Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef",
            itinvent_enabled=True,
            itinvent_db_host="sql.local",
            itinvent_db_name="ITInvent",
            itinvent_db_username="reader",
            itinvent_db_password="secret",
            itinvent_issued_location_no=24,
        )

    def test_inventory_number_is_identifier_not_float(self):
        self.assertEqual(ITInventService.identifier_text(12345.0), "12345")
        self.assertEqual(ITInventService.identifier_text(Decimal("12345.00")), "12345")
        self.assertEqual(ITInventService.identifier_text(None), "")

    def test_lookup_uses_login_and_location_24(self):
        cursor = FakeCursor(
            [
                [(17, "Ахметова Анна Сергеевна", "ahmetova.as")],
                [("Ноутбук", "SN-1", 12345.0, "BUH-42")],
            ]
        )
        connection = FakeConnection(cursor)
        fake = types.SimpleNamespace(connect=lambda **kwargs: connection)
        old = sys.modules.get("pymssql")
        sys.modules["pymssql"] = fake
        try:
            result = ITInventService(self.settings()).equipment_for_login(
                "Ahmetova.AS"
            )
        finally:
            if old is None:
                sys.modules.pop("pymssql", None)
            else:
                sys.modules["pymssql"] = old

        self.assertTrue(result.owner_found)
        self.assertEqual(result.owner_display_name, "Ахметова Анна Сергеевна")
        self.assertEqual(result.owner_login, "ahmetova.as")
        self.assertEqual(len(result.equipment), 1)
        self.assertEqual(result.equipment[0].inventory_number, "12345")
        self.assertEqual(cursor.executed[0][1], ("ahmetova.as",))
        self.assertEqual(cursor.executed[1][1], (17, 24))
        self.assertIn("i.LOC_NO = %s", cursor.executed[1][0])

    def test_owner_without_equipment_is_distinct_from_missing_owner(self):
        cursor = FakeCursor(
            [[(18, "Иванов Иван Иванович", "ivanov.ii")], []]
        )
        connection = FakeConnection(cursor)
        fake = types.SimpleNamespace(connect=lambda **kwargs: connection)
        old = sys.modules.get("pymssql")
        sys.modules["pymssql"] = fake
        try:
            result = ITInventService(self.settings()).equipment_for_login(
                "ivanov.ii"
            )
        finally:
            if old is None:
                sys.modules.pop("pymssql", None)
            else:
                sys.modules["pymssql"] = old
        self.assertTrue(result.owner_found)
        self.assertEqual(result.equipment, ())


if __name__ == "__main__":
    unittest.main()
