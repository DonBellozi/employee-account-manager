import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.templating import Jinja2Templates

from app.time_utils import (
    format_app_datetime,
    register_datetime_filters,
    server_clock,
    to_app_timezone,
)


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


class ServerClockTests(unittest.TestCase):
    @patch("app.time_utils.get_settings")
    def test_clock_reports_configured_zone_and_offset(self, get_settings):
        get_settings.return_value.app_timezone = "Europe/Moscow"
        clock = server_clock()
        self.assertEqual(clock["zone"], "Europe/Moscow")
        self.assertEqual(clock["offset_minutes"], 180)
        self.assertEqual(clock["offset_label"], "UTC+03:00")
        self.assertEqual(clock["text"], f"{clock['date']} {clock['time']}")

    @patch("app.time_utils.get_settings")
    def test_epoch_matches_rendered_wall_time(self, get_settings):
        get_settings.return_value.app_timezone = "Europe/Moscow"
        clock = server_clock()
        # Браузер восстанавливает настенное время сервера из epoch и смещения,
        # поэтому эти два поля обязаны описывать один и тот же момент.
        restored = datetime.fromtimestamp(
            clock["epoch_ms"] / 1000,
            tz=timezone.utc,
        )
        offset_hours = clock["offset_minutes"] / 60
        restored_hour = (restored.hour + offset_hours) % 24
        self.assertEqual(int(restored_hour), int(clock["time"][:2]))

    @patch("app.time_utils.get_settings")
    def test_clock_is_registered_as_template_global(self, get_settings):
        get_settings.return_value.app_timezone = "Europe/Moscow"
        templates = register_datetime_filters(
            Jinja2Templates(directory="app/templates")
        )
        self.assertIn("server_clock", templates.env.globals)
        rendered = templates.env.from_string(
            "{{ server_clock().zone }}"
        ).render()
        self.assertEqual(rendered, "Europe/Moscow")


class ServerClockWiringTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def test_every_template_router_registers_the_global(self):
        # Шапка общая, поэтому глобал должен быть у каждого набора шаблонов,
        # иначе часы молча исчезнут на отдельных страницах.
        for path in sorted(Path("app/routers").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "Jinja2Templates(directory=" not in text:
                continue
            self.assertIn(
                "register_datetime_filters",
                text,
                f"{path.name}: шаблоны без register_datetime_filters",
            )

    def test_header_renders_server_clock(self):
        page = self.read("app/templates/base.html")
        self.assertIn("server-clock", page)
        self.assertIn("data-epoch-ms", page)
        self.assertIn("data-offset-minutes", page)
        self.assertIn("/static/server_clock.js", page)

    def test_clock_script_does_not_use_browser_local_time(self):
        script = self.read("app/static/server_clock.js")
        # Настенное время сервера собирается только из UTC-геттеров.
        self.assertIn("getUTCHours", script)
        for forbidden in ("getHours()", "getMinutes()", "toLocaleTimeString"):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
