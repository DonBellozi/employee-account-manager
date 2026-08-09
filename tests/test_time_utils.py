import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.time_utils import format_app_datetime, to_app_timezone


class MoscowTimeTests(unittest.TestCase):
    @patch("app.time_utils.get_settings")
    def test_naive_sqlite_utc_is_shown_in_moscow_time(self, get_settings):
        get_settings.return_value.app_timezone = "Europe/Moscow"
        value = datetime(2026, 8, 9, 8, 0, 0)
        self.assertEqual(format_app_datetime(value), "09.08.2026 11:00:00")

    @patch("app.time_utils.get_settings")
    def test_aware_utc_is_shown_in_moscow_time(self, get_settings):
        get_settings.return_value.app_timezone = "Europe/Moscow"
        value = datetime(2026, 8, 9, 8, 30, 0, tzinfo=timezone.utc)
        local = to_app_timezone(value)
        self.assertIsNotNone(local)
        self.assertEqual(local.utcoffset().total_seconds(), 3 * 3600)
        self.assertEqual(local.hour, 11)

    @patch("app.time_utils.get_settings")
    def test_invalid_zone_falls_back_to_moscow(self, get_settings):
        get_settings.return_value.app_timezone = "Invalid/Zone"
        value = datetime(2026, 8, 9, 8, 0, 0)
        self.assertEqual(format_app_datetime(value, "%H:%M"), "11:00")


if __name__ == "__main__":
    unittest.main()
