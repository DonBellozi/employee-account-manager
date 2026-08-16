from __future__ import annotations

import unittest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.services.synology import SynologyLocalUser
from app.services.synology_lifecycle import SynologyLifecycleService
from app.services.synology_policy import (
    ACTION_CLASSIFY,
    ACTION_DISABLE,
    CLASS_INTERNAL_ACTIVE,
    CLASS_INTERNAL_DISMISSED,
    CLASS_PROTECTED,
    CLASS_UNKNOWN,
    classify_account,
    desired_action,
)


DOMAINS = {"corp.test"}


def classify(**kwargs) -> str:
    base = dict(
        email="",
        managed_domains=DOMAINS,
        protected=False,
        exception=False,
        active_employee=False,
        matched_employee=False,
    )
    base.update(kwargs)
    return classify_account(**base)


class PresenceIsTheFirstFilterTests(unittest.TestCase):
    """Регресс: действующих работников блокировать нельзя."""

    def test_active_employee_without_email_is_not_unknown(self):
        # Именно этот случай вызвал инцидент: у работника в DSM пустой e-mail.
        self.assertEqual(
            classify(email="", active_employee=True, matched_employee=True),
            CLASS_INTERNAL_ACTIVE,
        )

    def test_active_employee_with_personal_email_is_protected(self):
        self.assertEqual(
            classify(
                email="ivanov@gmail.test",
                active_employee=True,
                matched_employee=True,
            ),
            CLASS_INTERNAL_ACTIVE,
        )

    def test_unmatched_account_in_our_domain_is_not_treated_as_dismissed(self):
        # Учетка нашего домена, которую не удалось связать с человеком, —
        # это пробел в данных, а не увольнение.
        self.assertEqual(
            classify(email="someone@corp.test", matched_employee=False),
            CLASS_UNKNOWN,
        )

    def test_matched_but_no_longer_working_is_dismissed(self):
        self.assertEqual(
            classify(
                email="former@corp.test",
                matched_employee=True,
                active_employee=False,
            ),
            CLASS_INTERNAL_DISMISSED,
        )

    def test_system_account_wins_over_everything(self):
        self.assertEqual(
            classify(protected=True, active_employee=True, matched_employee=True),
            CLASS_PROTECTED,
        )

    def test_unknown_is_never_disabled_automatically(self):
        decision = desired_action(
            classification=CLASS_UNKNOWN,
            is_active=True,
            observed_expires_at=None,
            today=date(2026, 8, 16),
            delete_after=None,
            enrolled=False,
            policy_expires_at=None,
            previous_active=None,
            internal_months=3,
            external_months=6,
        )
        self.assertEqual(decision.action, ACTION_CLASSIFY)
        self.assertNotEqual(decision.action, ACTION_DISABLE)


class WorkerFixture:
    """Общая кадровая фикстура; сам по себе не является набором тестов."""

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
            zimbra_domains=["corp.test"],
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self) -> SynologyLifecycleService:
        return SynologyLifecycleService(self.settings, self.db)

    def add_worker(
        self,
        *,
        worker_key: str = "w-1",
        fio: str = "Иванов Иван Иванович",
        corporate_email: str = "ivanov@corp.test",
        personal_email: str = "",
        login: str = "ivanov.ii",
        status: str = "active",
    ):
        self.db.add(
            HRSourceRecord(
                worker_key=worker_key,
                source_id="main",
                fio=fio,
                corporate_email=corporate_email,
                personal_email=personal_email,
                login=login,
                is_present=True,
            )
        )
        self.db.add(
            HREmploymentState(
                worker_key=worker_key,
                source_id="main",
                fio=fio,
                status=status,
                is_present=True,
            )
        )
        self.db.commit()

    def account(self, **kwargs) -> SynologyLocalUser:
        base = dict(
            login="ivanov.ii",
            stable_id="uid:1500",
            uid="1500",
            email="",
            description="",
            status="active",
            is_active=True,
        )
        base.update(kwargs)
        return SynologyLocalUser(**base)

    def snapshot(self, account: SynologyLocalUser) -> dict:
        return self.service()._hr_snapshot(account)


class WorkerMatchingTests(WorkerFixture, unittest.TestCase):
    """Учетка DSM связывается с человеком по нескольким признакам."""

    def test_matched_by_corporate_email(self):
        self.add_worker()
        result = self.snapshot(self.account(email="Ivanov@CORP.test"))
        self.assertTrue(result["matched"])
        self.assertTrue(result["active"])
        self.assertEqual(result["match_method"], "email")

    def test_matched_by_personal_email(self):
        self.add_worker(personal_email="ivanov.personal@mail.test")
        result = self.snapshot(self.account(email="ivanov.personal@mail.test"))
        self.assertTrue(result["matched"])
        self.assertTrue(result["active"])

    def test_matched_by_login_when_dsm_has_no_email(self):
        # Ключевой сценарий инцидента.
        self.add_worker()
        result = self.snapshot(self.account(email="", login="ivanov.ii"))
        self.assertTrue(result["matched"])
        self.assertTrue(result["active"])
        self.assertEqual(result["match_method"], "login")

    def test_matched_by_ad_login_mapping(self):
        self.add_worker(login="")
        self.db.add(
            EmailLoginMapping(
                worker_key="w-1",
                source_domain="corp.test",
                source_email="ivanov@corp.test",
                ad_object_guid="guid-1",
                ad_login="ivanov.ii",
            )
        )
        self.db.commit()
        result = self.snapshot(self.account(email="", login="ivanov.ii"))
        self.assertTrue(result["matched"])
        self.assertTrue(result["active"])
        self.assertEqual(result["match_method"], "login")

    def test_matched_by_fio_from_description(self):
        self.add_worker(login="other.login")
        result = self.snapshot(
            self.account(
                email="",
                login="ivanov1",
                description="Иванов Иван Иванович",
            )
        )
        self.assertTrue(result["matched"])
        self.assertTrue(result["active"])
        self.assertEqual(result["match_method"], "fio")

    def test_fio_matching_ignores_case_spacing_and_yo(self):
        self.add_worker(fio="Алёнов  Пётр Петрович", login="other.login")
        result = self.snapshot(
            self.account(
                email="",
                login="unrelated",
                description="аленов петр петрович",
            )
        )
        self.assertTrue(result["matched"])

    def test_truly_unknown_account_stays_unmatched(self):
        self.add_worker()
        result = self.snapshot(
            self.account(
                email="",
                login="scanner01",
                description="Сканер второго этажа",
            )
        )
        self.assertFalse(result["matched"])
        self.assertFalse(result["active"])

    def test_scheduled_dismissal_still_counts_as_working(self):
        self.add_worker(status="scheduled")
        result = self.snapshot(self.account(email="", login="ivanov.ii"))
        self.assertTrue(result["active"])


class EndToEndClassificationTests(WorkerFixture, unittest.TestCase):
    """Полный путь: снимок DSM -> сопоставление -> класс -> действие."""

    def decide(self, account: SynologyLocalUser) -> tuple[str, str]:
        service = self.service()
        hr = service._hr_snapshot(account)
        classification = classify_account(
            email=account.email,
            managed_domains=service.managed_domains(),
            protected=account.protected,
            exception=False,
            active_employee=bool(hr["active"]),
            matched_employee=bool(hr["matched"]),
        )
        decision = desired_action(
            classification=classification,
            is_active=account.is_active,
            observed_expires_at=None,
            today=service.today,
            delete_after=None,
            enrolled=False,
            policy_expires_at=None,
            previous_active=None,
            internal_months=3,
            external_months=6,
        )
        return classification, decision.action

    def test_working_employee_without_email_is_never_disabled(self):
        self.add_worker()
        classification, action = self.decide(
            self.account(email="", login="ivanov.ii")
        )
        self.assertEqual(classification, CLASS_INTERNAL_ACTIVE)
        self.assertNotEqual(action, ACTION_DISABLE)

    def test_working_employee_with_corporate_email_is_never_disabled(self):
        self.add_worker()
        classification, action = self.decide(
            self.account(email="ivanov@corp.test", login="ivanov.ii")
        )
        self.assertEqual(classification, CLASS_INTERNAL_ACTIVE)
        self.assertNotEqual(action, ACTION_DISABLE)

    def test_orphan_account_is_not_disabled_without_evidence(self):
        self.add_worker()
        classification, action = self.decide(
            self.account(
                email="",
                login="scanner01",
                stable_id="uid:1900",
                description="Сканер второго этажа",
            )
        )
        self.assertEqual(classification, CLASS_UNKNOWN)
        self.assertNotEqual(action, ACTION_DISABLE)

    def test_dismissed_employee_is_recognised_but_handed_over(self):
        # Класс определяется правильно, однако блокировкой занимается общий
        # контур увольнения вместе с AD и Zimbra, а не этот модуль.
        self.add_worker(status="dismissed")
        self.db.query(HRSourceRecord).update({HRSourceRecord.is_present: False})
        self.db.commit()
        classification, action = self.decide(
            self.account(email="ivanov@corp.test", login="ivanov.ii")
        )
        self.assertEqual(classification, CLASS_INTERNAL_DISMISSED)
        self.assertNotEqual(action, ACTION_DISABLE)


if __name__ == "__main__":
    unittest.main()
