from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.hr_registry import (
    ZIMBRA_LABELS,
    reconciliation_status_for,
    worker_requires_active_accounts,
    zimbra_registry_status,
)


class RegistryZimbraAccountStatusTests(unittest.TestCase):
    def record(self, ad_status: str, zimbra_status: str):
        return SimpleNamespace(
            ad_status=ad_status,
            zimbra_status=zimbra_status,
        )

    def test_zimbra_real_states_are_preserved(self):
        for status in (
            "active",
            "closed",
            "locked",
            "lockout",
            "maintenance",
            "pending",
        ):
            identity = SimpleNamespace(account_status=status)
            self.assertEqual(zimbra_registry_status(identity), status)

    def test_unknown_zimbra_state_keeps_safe_legacy_present(self):
        identity = SimpleNamespace(account_status="unexpected")
        self.assertEqual(zimbra_registry_status(identity), "present")

    def test_labels_distinguish_active_and_closed(self):
        self.assertEqual(ZIMBRA_LABELS["active"], "Есть, активна")
        self.assertEqual(ZIMBRA_LABELS["closed"], "Есть, закрыта")

    def test_active_employee_requires_enabled_ad_and_active_zimbra(self):
        self.assertEqual(
            reconciliation_status_for(
                self.record("enabled", "active"),
                requires_active_accounts=True,
            ),
            "ok",
        )
        self.assertEqual(
            reconciliation_status_for(
                self.record("disabled", "active"),
                requires_active_accounts=True,
            ),
            "issue",
        )
        self.assertEqual(
            reconciliation_status_for(
                self.record("enabled", "closed"),
                requires_active_accounts=True,
            ),
            "issue",
        )

    def test_final_dismissal_accepts_disabled_ad_and_closed_zimbra(self):
        self.assertEqual(
            reconciliation_status_for(
                self.record("disabled", "closed"),
                requires_active_accounts=False,
            ),
            "ok",
        )

    def test_final_dismissal_accepts_accounts_already_absent(self):
        self.assertEqual(
            reconciliation_status_for(
                self.record("missing", "missing"),
                requires_active_accounts=False,
            ),
            "ok",
        )
        self.assertEqual(
            reconciliation_status_for(
                self.record("no_login", "no_email"),
                requires_active_accounts=False,
            ),
            "ok",
        )

    def test_final_dismissal_rejects_still_active_account(self):
        self.assertEqual(
            reconciliation_status_for(
                self.record("enabled", "closed"),
                requires_active_accounts=False,
            ),
            "issue",
        )
        self.assertEqual(
            reconciliation_status_for(
                self.record("disabled", "active"),
                requires_active_accounts=False,
            ),
            "issue",
        )

    def test_any_active_or_scheduled_employment_protects_accounts(self):
        states = [
            SimpleNamespace(status="dismissed"),
            SimpleNamespace(status="active"),
        ]
        self.assertIs(worker_requires_active_accounts(states), True)
        states = [
            SimpleNamespace(status="dismissed"),
            SimpleNamespace(status="scheduled"),
        ]
        self.assertIs(worker_requires_active_accounts(states), True)

    def test_all_dismissed_means_final_dismissal(self):
        states = [
            SimpleNamespace(status="dismissed"),
            SimpleNamespace(status="dismissed"),
        ]
        self.assertIs(worker_requires_active_accounts(states), False)


if __name__ == "__main__":
    unittest.main()
