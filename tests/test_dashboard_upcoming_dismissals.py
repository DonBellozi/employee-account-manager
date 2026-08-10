from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardUpcomingDismissalsTests(unittest.TestCase):
    def test_old_reconciliation_module_is_removed(self):
        text = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("Сверка 1С / AD / Zimbra", text)
        self.assertIn("Ближайшие увольнения", text)
        self.assertIn("Отложить", text)

    def test_new_dashboard_router_wins_before_legacy_dashboard(self):
        text = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertLess(
            text.index("app.include_router(dashboard_dismissals.router)"),
            text.index("app.include_router(employees.router)"),
        )

    def test_primary_registry_sync_updates_employment_state(self):
        text = (ROOT / "app/services/hr_registry.py").read_text(encoding="utf-8")
        self.assertIn("sync_workbook_employment", text)
        self.assertIn("workbook=workbook", text)

    def test_additional_import_uses_position_aware_dismissal_date(self):
        text = (
            ROOT / "app/services/onec_additional_import.py"
        ).read_text(encoding="utf-8")
        self.assertIn("worker.dismissal_date", text)
        self.assertIn("workbook.dismissal_column", text)


if __name__ == "__main__":
    unittest.main()
