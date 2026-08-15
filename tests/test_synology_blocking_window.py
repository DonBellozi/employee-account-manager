from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.services.blocking_window import (
    BLOCK_MAX_ATTEMPTS,
    BLOCK_RETRY_MINUTES,
    BLOCK_TIME,
    is_block_window_open,
)
from app.services.synology import SynologyService
from app.services.synology_lifecycle import SynologyLifecycleService


class BlockingWindowTests(unittest.TestCase):
    def test_window_opens_at_the_shared_project_time(self):
        self.assertEqual(BLOCK_TIME, time(19, 10))
        day = date(2026, 8, 15)
        self.assertFalse(
            is_block_window_open(datetime.combine(day, time(19, 9, 59)))
        )
        self.assertTrue(
            is_block_window_open(datetime.combine(day, time(19, 10)))
        )
        self.assertTrue(
            is_block_window_open(datetime.combine(day, time(23, 59)))
        )

    def test_retry_policy_is_bounded(self):
        self.assertEqual(BLOCK_RETRY_MINUTES, 10)
        self.assertGreaterEqual(BLOCK_MAX_ATTEMPTS, 2)


class SynologyWindowStateTests(unittest.TestCase):
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
            synology_enabled=True,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self) -> SynologyLifecycleService:
        return SynologyLifecycleService(self.settings, self.db)

    def control(self):
        row = self.service().control_settings()
        row.write_enabled = True
        self.db.commit()
        return row

    def test_window_is_closed_before_the_shared_time(self):
        control = self.control()
        with patch(
            "app.services.synology_lifecycle.is_block_window_open",
            return_value=False,
        ):
            allowed, reason = self.service().block_window_state(control)
        self.assertFalse(allowed)
        self.assertIn("19:10", reason)

    def test_first_attempt_inside_window_is_allowed(self):
        control = self.control()
        with patch(
            "app.services.synology_lifecycle.is_block_window_open",
            return_value=True,
        ):
            allowed, _reason = self.service().block_window_state(control)
        self.assertTrue(allowed)

    def test_retry_waits_for_the_shared_interval(self):
        control = self.control()
        service = self.service()
        with patch(
            "app.services.synology_lifecycle.is_block_window_open",
            return_value=True,
        ):
            service._register_block_attempt(control)
            self.db.commit()

            allowed, reason = service.block_window_state(control)
            self.assertFalse(allowed)
            self.assertIn(str(BLOCK_RETRY_MINUTES), reason)

            # Интервал истек — попытка снова разрешена.
            control.last_block_attempt_at = datetime.now(
                timezone.utc
            ) - timedelta(minutes=BLOCK_RETRY_MINUTES + 1)
            self.db.commit()
            allowed, _reason = service.block_window_state(control)
            self.assertTrue(allowed)

    def test_attempts_are_capped_per_day(self):
        control = self.control()
        service = self.service()
        control.block_window_date = service.today
        control.block_attempts = BLOCK_MAX_ATTEMPTS
        control.last_block_attempt_at = datetime.now(timezone.utc) - timedelta(
            hours=1
        )
        self.db.commit()

        with patch(
            "app.services.synology_lifecycle.is_block_window_open",
            return_value=True,
        ):
            allowed, reason = service.block_window_state(control)
        self.assertFalse(allowed)
        self.assertIn("лимит", reason.lower())

    def test_counter_resets_on_a_new_day(self):
        control = self.control()
        service = self.service()
        control.block_window_date = service.today - timedelta(days=1)
        control.block_attempts = BLOCK_MAX_ATTEMPTS
        control.last_block_attempt_at = datetime.now(timezone.utc) - timedelta(
            days=1
        )
        self.db.commit()

        with patch(
            "app.services.synology_lifecycle.is_block_window_open",
            return_value=True,
        ):
            allowed, _reason = service.block_window_state(control)
        self.assertTrue(allowed)

        service._register_block_attempt(control)
        self.assertEqual(control.block_attempts, 1)
        self.assertEqual(control.block_window_date, service.today)


class SynologyProtectionScopeTests(unittest.TestCase):
    """Защищены только реально системные записи DSM."""

    def parse(self, output: str, login: str = "someone"):
        return SynologyService._parse_detail(login, output)

    def test_system_logins_and_low_uid_stay_protected(self):
        card = "User Name: [admin]\nUser uid: [1024]\nUser Mail: [a@corp.test]"
        self.assertTrue(self.parse(card, "admin").protected)

        card = "User Name: [svc]\nUser uid: [101]\nUser Mail: [s@corp.test]"
        self.assertTrue(self.parse(card, "svc").protected)

    def test_description_no_longer_makes_an_employee_protected(self):
        # Раньше слово administrator в любом месте карточки выводило учетку
        # из-под автоматики, и обычный сотрудник молча не блокировался.
        card = (
            "User Name: [ivanov.ii]\n"
            "User uid: [1035]\n"
            "Description: [System Administrator]\n"
            "Primary group: [users]\n"
            "User Mail: [ivanov@corp.test]"
        )
        parsed = self.parse(card, "ivanov.ii")
        self.assertFalse(parsed.protected)
        self.assertEqual(parsed.email, "ivanov@corp.test")

    def test_account_without_email_is_parsed_and_not_protected(self):
        card = "User Name: [legacy]\nUser uid: [1042]\nUser Mail: []"
        parsed = self.parse(card, "legacy")
        self.assertEqual(parsed.email, "")
        self.assertFalse(parsed.protected)


class SharedWindowWiringTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def test_both_contours_use_the_same_window_module(self):
        for path in (
            "app/services/final_dismissal_lifecycle.py",
            "app/services/synology_lifecycle.py",
        ):
            text = self.read(path)
            self.assertIn("from app.services.blocking_window import", text)
        # Локальных дублей времени блокировки остаться не должно.
        self.assertNotIn(
            "time(19, 10)",
            self.read("app/services/final_dismissal_lifecycle.py"),
        )

    def test_scheduler_tightens_interval_inside_the_window(self):
        text = self.read("app/services/synology_scheduler.py")
        self.assertIn("BLOCK_RETRY_MINUTES", text)
        self.assertIn("_effective_interval", text)

    def test_expire_account_no_longer_requires_email(self):
        text = self.read("app/services/synology.py")
        self.assertNotIn("автоматическая блокировка запрещена", text)


if __name__ == "__main__":
    unittest.main()
