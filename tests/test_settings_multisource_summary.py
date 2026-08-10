from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SettingsMultiSourceSummaryTests(unittest.TestCase):
    def test_summary_endpoint_uses_multisource_service(self):
        text = (
            ROOT / "app/routers/onec_sources.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "MultiSourceHRRegistryViewService",
            text,
        )
        self.assertIn(
            '@router.get("/settings/onec-sources/summary")',
            text,
        )
        self.assertIn(
            "service.summary(source_id=source_id)",
            text,
        )

    def test_settings_replaces_legacy_single_source_summary(self):
        text = (
            ROOT / "app/static/settings_extensions.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Сверка 1С / AD / Zimbra – все организации",
            text,
        )
        self.assertIn(
            "hideLegacySummaries",
            text,
        )
        self.assertIn(
            "/settings/onec-sources/summary",
            text,
        )

    def test_summary_refreshes_after_report_changes(self):
        text = (
            ROOT / "app/static/settings_extensions.js"
        ).read_text(encoding="utf-8")
        self.assertIn("MutationObserver", text)
        self.assertIn("refreshMultiSourceSummary()", text)


if __name__ == "__main__":
    unittest.main()
