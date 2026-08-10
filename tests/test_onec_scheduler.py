from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.onec_scheduler import next_scheduled_run, schedule_info


class OneCSchedulerTests(unittest.TestCase):
    def settings(self):
        return Settings(
            _env_file=None,
            app_secret_key="0123456789abcdef",
            app_timezone="Europe/Moscow",
            onec_auto_import_enabled=True,
            onec_auto_import_time="09:00",  # legacy, polling его не использует
            onec_auto_import_startup_catchup=True,
        )

    def test_next_run_is_five_minutes_later(self):
        settings = self.settings()
        now = datetime(
            2026, 8, 8, 8, 30,
            tzinfo=ZoneInfo("Europe/Moscow"),
        )
        next_run = next_scheduled_run(settings, now=now)
        self.assertEqual(next_run.strftime("%Y-%m-%d %H:%M"), "2026-08-08 08:35")

    def test_next_run_crosses_midnight(self):
        settings = self.settings()
        now = datetime(
            2026, 8, 8, 23, 58,
            tzinfo=ZoneInfo("Europe/Moscow"),
        )
        next_run = next_scheduled_run(settings, now=now)
        self.assertEqual(next_run.strftime("%Y-%m-%d %H:%M"), "2026-08-09 00:03")

    def test_schedule_info_exposes_polling_and_control_export(self):
        info = schedule_info(self.settings())
        self.assertTrue(info["enabled"])
        self.assertEqual(info["time"], "каждые 5 минут")
        self.assertEqual(info["control_export_time"], "19:00")
        self.assertEqual(info["startup_catchup_label"], "Да")


if __name__ == "__main__":
    unittest.main()
