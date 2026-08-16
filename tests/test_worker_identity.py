from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.services.worker_identity import WorkerIdentityResolver
from app.services.zimbra_observer import (
    HRProtectionSnapshot,
    ObservedZimbraAccount,
    ZimbraObserverService,
)


class WorkerIdentityResolverTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def resolver(self) -> WorkerIdentityResolver:
        return WorkerIdentityResolver(self.db)

    def add_worker(
        self,
        *,
        worker_key: str = "w-1",
        fio: str = "Иванов Иван Иванович",
        corporate_email: str = "ivanov@corp.test",
        personal_email: str = "",
        login: str = "ivanov.ii",
        status: str = "active",
        is_present: bool = True,
        dismissal_date: date | None = None,
        source_id: str = "main",
    ):
        self.db.add(
            HRSourceRecord(
                worker_key=worker_key,
                source_id=source_id,
                fio=fio,
                corporate_email=corporate_email,
                personal_email=personal_email,
                login=login,
                is_present=is_present,
            )
        )
        self.db.add(
            HREmploymentState(
                worker_key=worker_key,
                source_id=source_id,
                fio=fio,
                status=status,
                is_present=is_present,
                dismissal_date=dismissal_date,
            )
        )
        self.db.commit()

    # --- признаки сопоставления ------------------------------------------

    def test_corporate_email(self):
        self.add_worker()
        match = self.resolver().resolve(emails=["Ivanov@CORP.test"])
        self.assertTrue(match.matched)
        self.assertTrue(match.active)
        self.assertEqual(match.method, "email")
        self.assertEqual(match.worker_key, "w-1")

    def test_personal_email(self):
        self.add_worker(personal_email="ivan.personal@mail.test")
        match = self.resolver().resolve(emails=["ivan.personal@mail.test"])
        self.assertTrue(match.active)

    def test_alias_matches_through_email_login_mapping(self):
        # Сценарий инцидента в Zimbra: работник пользуется алиасом, а в 1С
        # записан другой адрес.
        self.add_worker(corporate_email="i.ivanov@corp.test")
        self.db.add(
            EmailLoginMapping(
                worker_key="w-1",
                source_domain="corp.test",
                source_email="i.ivanov@corp.test",
                ad_object_guid="guid-1",
                ad_login="ivanov.ii",
                zimbra_id="zid-1",
                zimbra_email="ivanov@corp.test",
            )
        )
        self.db.commit()
        match = self.resolver().resolve(emails=["ivanov@corp.test"])
        self.assertTrue(match.active)

    def test_login_matches_when_email_is_empty(self):
        self.add_worker()
        match = self.resolver().resolve(emails=[""], logins=["ivanov.ii"])
        self.assertTrue(match.active)
        self.assertEqual(match.method, "login")

    def test_email_local_part_is_used_as_a_login(self):
        # В 1С логин не заполнен, но локальная часть адреса совпадает.
        self.add_worker(login="")
        match = self.resolver().resolve(logins=["ivanov"])
        self.assertTrue(match.active)

    def test_fio_from_description(self):
        self.add_worker(login="", corporate_email="")
        match = self.resolver().resolve(fio="иванов  иван иванович")
        self.assertTrue(match.active)
        self.assertEqual(match.method, "fio")

    def test_fio_normalisation_handles_yo(self):
        self.add_worker(fio="Алёнов Пётр Петрович", login="", corporate_email="")
        self.assertTrue(self.resolver().resolve(fio="аленов петр петрович").active)

    def test_no_signal_means_no_match(self):
        self.add_worker()
        match = self.resolver().resolve(
            emails=["scanner@device.test"],
            logins=["scanner01"],
            fio="Сканер второго этажа",
        )
        self.assertFalse(match.matched)
        self.assertFalse(match.active)

    # --- признак занятости ------------------------------------------------

    def test_scheduled_dismissal_still_counts_as_working(self):
        self.add_worker(status="scheduled", dismissal_date=date(2030, 1, 1))
        self.assertTrue(self.resolver().resolve(emails=["ivanov@corp.test"]).active)

    def test_employment_in_any_organisation_protects(self):
        self.add_worker(source_id="org-a", status="dismissed", is_present=False)
        self.add_worker(source_id="org-b", status="active", is_present=True)
        match = self.resolver().resolve(emails=["ivanov@corp.test"])
        self.assertTrue(match.active)

    def test_fully_dismissed_worker_is_matched_but_not_active(self):
        self.add_worker(
            status="dismissed",
            is_present=False,
            dismissal_date=date(2026, 8, 1),
        )
        match = self.resolver().resolve(emails=["ivanov@corp.test"])
        self.assertTrue(match.matched)
        self.assertFalse(match.active)
        self.assertEqual(match.dismissal_date, date(2026, 8, 1))

    def test_ambiguous_match_is_reported(self):
        self.add_worker(worker_key="w-1", source_id="a")
        self.add_worker(
            worker_key="w-2",
            source_id="b",
            fio="Петров Петр Петрович",
            login="petrov.pp",
            corporate_email="ivanov@corp.test",
        )
        match = self.resolver().resolve(emails=["ivanov@corp.test"])
        self.assertTrue(match.ambiguous)
        self.assertEqual(match.worker_key, "")
        self.assertIn("ambiguous", match.method)


class ZimbraInactivityRegressionTests(unittest.TestCase):
    """Действующий работник не теряет ящик по неактивности."""

    NOW = datetime(2026, 8, 16, 6, 0, tzinfo=timezone.utc)

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
            zimbra_domains=["corp.test"],
            zimbra_primary_domain="corp.test",
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_active_worker(self, **kwargs):
        defaults = dict(
            worker_key="w-1",
            source_id="main",
            fio="Иванов Иван Иванович",
            corporate_email="i.ivanov@corp.test",
            personal_email="",
            login="ivanov.ii",
            is_present=True,
        )
        defaults.update(kwargs)
        self.db.add(HRSourceRecord(**defaults))
        self.db.add(
            HREmploymentState(
                worker_key=defaults["worker_key"],
                source_id=defaults["source_id"],
                fio=defaults["fio"],
                status="active",
                is_present=True,
            )
        )
        self.db.commit()

    def snapshot(self) -> HRProtectionSnapshot:
        rows = list(self.db.scalars(select(HRSourceRecord)).all())
        return HRProtectionSnapshot(
            emails=frozenset(
                row.corporate_email.strip().lower()
                for row in rows
                if row.corporate_email and "@" in row.corporate_email
            ),
            snapshot_at=self.NOW - timedelta(hours=2),
            age_minutes=120,
            fresh=True,
            records_count=len(rows),
            source_count=1,
            identity=WorkerIdentityResolver(self.db),
        )

    def evaluate(self, *addresses: str):
        service = ZimbraObserverService(self.settings, self.db)
        config = service.get_settings_record()
        account = ObservedZimbraAccount(
            zimbra_id="zid-1",
            primary_email=addresses[0],
            addresses=addresses,
            account_status="active",
            # Полтора года без входа: порог неактивности заведомо превышен.
            last_logon_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            note="",
        )
        return service._evaluate(
            account,
            previous_state=None,
            config=config,
            hr=self.snapshot(),
            dismissal_map={},
            now=self.NOW,
            local_today=self.NOW.date(),
        )

    def test_exact_corporate_email_is_protected(self):
        self.add_active_worker()
        self.assertEqual(
            self.evaluate("i.ivanov@corp.test").recommendation,
            "protected_hr",
        )

    def test_alias_is_protected(self):
        # Ящик известен под другим адресом, а в 1С записан основной.
        self.add_active_worker()
        evaluation = self.evaluate("ivanov.ii@corp.test", "i.ivanov@corp.test")
        self.assertEqual(evaluation.recommendation, "protected_hr")

    def test_worker_without_corporate_email_in_export_is_protected(self):
        # Ровно инцидент: поле в выгрузке пустое, ящик существует.
        self.add_active_worker(corporate_email="")
        evaluation = self.evaluate("ivanov.ii@corp.test")
        self.assertEqual(evaluation.recommendation, "protected_hr")
        self.assertNotEqual(evaluation.recommendation, "close")

    def test_worker_matched_only_by_login_is_protected(self):
        self.add_active_worker(corporate_email="typo@corp.test")
        evaluation = self.evaluate("ivanov.ii@corp.test")
        self.assertEqual(evaluation.recommendation, "protected_hr")

    def test_mailbox_of_nobody_is_still_a_close_candidate(self):
        # Защита не должна превратиться в «никогда ничего не закрывать».
        self.add_active_worker()
        evaluation = self.evaluate("old.project@corp.test")
        self.assertEqual(evaluation.recommendation, "close")
        self.assertIn("Неактивность", evaluation.reason)


class SharedRuleWiringTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def test_both_contours_use_the_shared_resolver(self):
        for path in (
            "app/services/zimbra_observer.py",
            "app/services/synology_lifecycle.py",
        ):
            self.assertIn(
                "from app.services.worker_identity import WorkerIdentityResolver",
                self.read(path),
            )

    def test_zimbra_protection_is_no_longer_a_single_field(self):
        text = self.read("app/services/zimbra_observer.py")
        block = text[text.index("def _evaluate("):]
        block = block[: block.index("if status == \"active\":")]
        # Совпадение по corporate_email осталось как быстрый путь, но оно
        # больше не единственное основание для защиты работника.
        self.assertIn("hr.identity", block)
        self.assertIn("match.active", block)

    def test_synology_no_longer_has_its_own_matching(self):
        text = self.read("app/services/synology_lifecycle.py")
        self.assertNotIn("_match_worker_keys", text)
        self.assertIn("self._identity.resolve(", text)


if __name__ == "__main__":
    unittest.main()
