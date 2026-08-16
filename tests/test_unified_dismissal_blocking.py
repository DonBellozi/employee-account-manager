from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models_dismissal_lifecycle import FinalDismissalBlockTarget
from app.models_synology import SynologyAccountState
from app.services.final_dismissal_lifecycle import (
    SYSTEM_LABELS,
    FinalDismissalLifecycleService,
)
from app.services.synology_policy import (
    ACTION_DISABLE,
    ACTION_NONE,
    CLASS_EXCEPTION,
    CLASS_INTERNAL_ACTIVE,
    CLASS_INTERNAL_DISMISSED,
    CLASS_PROTECTED,
    desired_action,
)


class SynologyIsPartOfTheSharedRunTests(unittest.TestCase):
    """Учетка DSM блокируется тем же решением, что AD и Zimbra."""

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

    def service(self) -> FinalDismissalLifecycleService:
        return FinalDismissalLifecycleService(self.settings, self.db)

    def add_state(
        self,
        *,
        login: str = "ivanov.ii",
        stable_id: str = "uid:1500",
        worker_key: str = "w-1",
        classification: str = CLASS_INTERNAL_ACTIVE,
        is_active: bool = True,
        is_present: bool = True,
    ) -> SynologyAccountState:
        row = SynologyAccountState(
            stable_id=stable_id,
            login=login,
            uid="1500",
            email="ivanov@corp.test",
            description="Иванов Иван Иванович",
            status="active" if is_active else "expired",
            is_active=is_active,
            is_present=is_present,
            classification=classification,
            worker_key=worker_key,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_matched_account_becomes_a_target(self):
        self.add_state()
        plans = self.service()._synology_plan("w-1")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["system"], "synology")
        self.assertEqual(plans[0]["identifier"], "ivanov.ii")
        self.assertEqual(plans[0]["stable_id"], "uid:1500")

    def test_unmatched_accounts_never_become_targets(self):
        # Именно это защищает от инцидента: учетка без связи с человеком
        # не может попасть в блокировку по увольнению.
        self.add_state(worker_key="")
        self.assertEqual(self.service()._synology_plan("w-1"), [])
        self.assertEqual(self.service()._synology_plan(""), [])

    def test_exceptions_and_system_accounts_are_skipped(self):
        self.add_state(stable_id="uid:1", login="svc", classification=CLASS_EXCEPTION)
        self.add_state(stable_id="uid:2", login="root", classification=CLASS_PROTECTED)
        self.assertEqual(self.service()._synology_plan("w-1"), [])

    def test_already_disabled_account_is_not_planned_again(self):
        self.add_state(is_active=False)
        self.assertEqual(self.service()._synology_plan("w-1"), [])

    def test_absent_account_is_not_planned(self):
        self.add_state(is_present=False)
        self.assertEqual(self.service()._synology_plan("w-1"), [])

    def test_disabled_integration_produces_no_targets(self):
        self.add_state()
        settings = Settings(
            app_secret_key="test-secret-key-1234567890",
            synology_enabled=False,
        )
        service = FinalDismissalLifecycleService(settings, self.db)
        self.assertEqual(service._synology_plan("w-1"), [])

    def test_target_model_accepts_the_third_system(self):
        self.db.add(
            FinalDismissalBlockTarget(
                run_id=1,
                system="synology",
                target_key="synology:uid:1500",
                target_identifier="ivanov.ii",
                stable_id="uid:1500",
            )
        )
        self.db.commit()
        stored = self.db.scalar(
            select(FinalDismissalBlockTarget).where(
                FinalDismissalBlockTarget.system == "synology"
            )
        )
        self.assertIsNotNone(stored)
        self.assertEqual(SYSTEM_LABELS["synology"], "Synology")


class SynologyNoLongerDecidesDismissalTests(unittest.TestCase):
    def decide(self, classification: str, *, is_active: bool = True) -> str:
        return desired_action(
            classification=classification,
            is_active=is_active,
            observed_expires_at=None,
            today=date(2026, 8, 16),
            delete_after=None,
            enrolled=False,
            policy_expires_at=None,
            previous_active=None,
            internal_months=3,
            external_months=6,
        ).action

    def test_dismissed_class_no_longer_triggers_its_own_block(self):
        # Увольнение обрабатывается общим контуром; дублирующее решение здесь
        # приводило к отключению действующих работников.
        self.assertEqual(self.decide(CLASS_INTERNAL_DISMISSED), ACTION_NONE)
        self.assertNotEqual(self.decide(CLASS_INTERNAL_DISMISSED), ACTION_DISABLE)

    def test_migration_cycles_are_kept(self):
        # Собственная задача контура — увод с локальных учеток — остается.
        overdue = desired_action(
            classification=CLASS_INTERNAL_ACTIVE,
            is_active=True,
            observed_expires_at=None,
            today=date(2026, 8, 16),
            delete_after=None,
            enrolled=True,
            policy_expires_at=date(2026, 8, 1),
            previous_active=True,
            internal_months=3,
            external_months=6,
        ).action
        self.assertEqual(overdue, ACTION_DISABLE)


class SharedContourWiringTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def test_all_three_systems_are_dispatched_in_one_place(self):
        text = self.read("app/services/final_dismissal_lifecycle.py")
        block = text[text.index("def _process_target("):]
        for system in ("ad", "zimbra", "synology"):
            self.assertIn(f'target.system == "{system}"', block)

    def test_synology_plan_joins_the_same_run(self):
        text = self.read("app/services/final_dismissal_lifecycle.py")
        block = text[text.index("def _ensure_run("):]
        block = block[: block.index("def _targets(")]
        self.assertIn("self._synology_plan(", block)
        self.assertIn("self._zimbra_plan(", block)
        self.assertIn("self._ad_plan(", block)

    def test_synology_uses_the_same_interlock_and_window(self):
        text = self.read("app/services/final_dismissal_lifecycle.py")
        block = text[text.index("def _process_target("):]
        block = block[: block.index("def process(")]
        # Кадровая перепроверка выполняется до диспетчеризации в любую систему.
        self.assertLess(
            block.index("self._still_due("),
            block.index('target.system == "ad"'),
        )

    def test_synology_policy_delegates_dismissal(self):
        policy = self.read("app/services/synology_policy.py")
        block = policy[policy.index("if classification == CLASS_INTERNAL_DISMISSED:"):]
        block = block[: block.index("if classification == CLASS_EXTERNAL:")]
        self.assertIn("ACTION_NONE", block)
        self.assertNotIn("ACTION_DISABLE", block)


if __name__ == "__main__":
    unittest.main()
