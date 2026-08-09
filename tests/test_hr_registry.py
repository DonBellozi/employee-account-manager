import sys
import types
import unittest
from types import SimpleNamespace
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
from app.services.hr_registry import HRRegistryService
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


if __name__ == "__main__":
    unittest.main()
