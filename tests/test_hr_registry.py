import sys
import types
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

# Stubs prevent importing optional runtime connector libs in this isolated test.
ad_stub = types.ModuleType("app.services.ad")
class ActiveDirectoryService:
    pass
class ADDirectoryUser:
    pass
ad_stub.ActiveDirectoryService = ActiveDirectoryService
ad_stub.ADDirectoryUser = ADDirectoryUser
sys.modules["app.services.ad"] = ad_stub

z_stub = types.ModuleType("app.services.zimbra")
class ZimbraService:
    pass
class ZimbraAccountIdentity:
    pass
z_stub.ZimbraService = ZimbraService
z_stub.ZimbraAccountIdentity = ZimbraAccountIdentity
sys.modules["app.services.zimbra"] = z_stub

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AuditLog, DomainAccessUser
from app.services.hr_registry import (
    MANUAL_CHECK_ACTION,
    MANUAL_CHECK_INVALIDATED_ACTION,
    HRRegistryService,
)
from app.services.onec_xlsx import OneCPlacement, OneCWorkbook, OneCWorker


class HRRegistryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.settings = SimpleNamespace(
            onec_source_domain="",
            zimbra_domains=["example.com"],
            ad_check_enabled=True,
            zimbra_check_enabled=True,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.services.hr_registry.ZimbraService")
    @patch("app.services.hr_registry.ActiveDirectoryService")
    def test_sync(self, ad, zimbra):
        zimbra.return_value.accounts_by_addresses.return_value = {
            "ivanov.ii@example.com": SimpleNamespace(
                login="ivanov.ii",
                primary_email="ivanov.ii@example.com",
                addresses=("ivanov.ii@example.com",),
            )
        }
        ad.return_value.users_by_logins.return_value = {
            "ivanov.ii": SimpleNamespace(is_enabled=True)
        }

        book = OneCWorkbook(
            workers=(
                OneCWorker(
                    "a" * 64,
                    "Иванов Иван Иванович",
                    "ivanov.ii@example.com",
                    "ivanov.ii",
                    (OneCPlacement("ИТ", "Специалист"),),
                    personal_email="ivan.personal@example.net",
                ),
                OneCWorker(
                    "b" * 64,
                    "Петров Петр Петрович",
                    "petrov.pp@example.com",
                    "petrov.pp",
                    (OneCPlacement("Проекты", "Эксперт"),),
                ),
            ),
            headers=("СНИЛС",),
            header_row=2,
            detected_columns={"snils": "СНИЛС"},
            potential_dismissal_columns=(),
        )

        service = HRRegistryService(self.settings, self.db)
        summary = service.sync_and_reconcile(book)["reconciliation"]

        self.assertEqual(
            (summary["total"], summary["ok"], summary["issues"]),
            (2, 1, 1),
        )
        self.assertEqual(len(service.list_rows(status="issues")), 1)
        all_rows = service.list_rows(query="ivan.personal@example.net")
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0]["personal_email"], "ivan.personal@example.net")

    @patch("app.services.hr_registry.ZimbraService")
    @patch("app.services.hr_registry.ActiveDirectoryService")
    def test_checked_worker_is_hidden_from_issues_and_returns_after_transfer(
        self,
        ad,
        zimbra,
    ):
        book = OneCWorkbook(
            workers=(
                OneCWorker(
                    "c" * 64,
                    "Сидоров Сергей Сергеевич",
                    "",
                    "",
                    (OneCPlacement("АХО", "Специалист"),),
                    personal_email="sidorov.personal@example.net",
                ),
            ),
            headers=("СНИЛС",),
            header_row=2,
            detected_columns={"snils": "СНИЛС"},
            potential_dismissal_columns=(),
        )
        service = HRRegistryService(self.settings, self.db)
        service.sync_and_reconcile(book)
        row = service.list_rows(status="issues")[0]

        self.db.add(
            DomainAccessUser(
                username="ivanov.ii",
                display_name="Иванов Иван Иванович",
                email="ivanov.ii@example.com",
                is_active=True,
            )
        )
        self.db.commit()

        service.mark_accounts_not_required(
            row["id"],
            "ivanov.ii",
            "ad",
        )
        # Repeating the same action is idempotent.
        service.mark_accounts_not_required(
            row["id"],
            "ivanov.ii",
            "ad",
        )

        summary = service.summary()
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["issues"], 0)
        self.assertEqual(service.list_rows(status="issues"), [])
        checked = service.list_rows(status="checked")[0]
        self.assertEqual(checked["reconciliation_label"], "Проверен")
        self.assertEqual(
            checked["manual_check_note"],
            "Иванов И.И. подтвердил, что учетные записи не требуются",
        )
        self.assertEqual(checked["manual_check_operator"], "ivanov.ii")
        confirmations = self.db.query(AuditLog).filter(
            AuditLog.action == MANUAL_CHECK_ACTION
        ).all()
        self.assertEqual(len(confirmations), 1)

        transferred = OneCWorkbook(
            workers=(
                OneCWorker(
                    "c" * 64,
                    "Сидоров Сергей Сергеевич",
                    "",
                    "",
                    (OneCPlacement("АХО", "Ведущий специалист"),),
                    personal_email="sidorov.personal@example.net",
                ),
            ),
            headers=("СНИЛС",),
            header_row=2,
            detected_columns={"snils": "СНИЛС"},
            potential_dismissal_columns=(),
        )
        service.sync_workbook(transferred)

        returned = service.list_rows(status="issues")[0]
        self.assertEqual(returned["reconciliation_label"], "Требует проверки")
        self.assertIn("Ранее Иванов И.И.", returned["manual_check_previous_note"])
        self.assertTrue(
            self.db.query(AuditLog).filter(
                AuditLog.action == MANUAL_CHECK_INVALIDATED_ACTION
            ).count()
        )

    @patch("app.services.hr_registry.ZimbraService")
    @patch("app.services.hr_registry.ActiveDirectoryService")
    def test_create_link_prefills_personal_email_without_new_workflow(
        self,
        ad,
        zimbra,
    ):
        book = OneCWorkbook(
            workers=(
                OneCWorker(
                    "d" * 64,
                    "Смирнов Семен Семенович",
                    "",
                    "",
                    (OneCPlacement("АХО", "Рабочий"),),
                    personal_email="smirnov.personal@example.net",
                ),
            ),
            headers=("СНИЛС",),
            header_row=2,
            detected_columns={"snils": "СНИЛС"},
            potential_dismissal_columns=(),
        )
        service = HRRegistryService(self.settings, self.db)
        service.sync_and_reconcile(book)
        row = service.list_rows()[0]
        self.assertTrue(row["create_url"].startswith("/employees/new?"))
        query = parse_qs(urlparse(row["create_url"]).query)
        self.assertEqual(
            query["fio"][0],
            "Смирнов Семен Семенович\nsmirnov.personal@example.net",
        )


if __name__ == "__main__":
    unittest.main()
