from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AbsenceMeansDismissalTests(unittest.TestCase):
    def test_primary_employment_sync_dates_absence_on_first_detection(self):
        text = (
            ROOT / "app/services/hr_employment.py"
        ).read_text(encoding="utf-8")

        self.assertIn('employment.status = "dismissed"', text)
        self.assertIn('employment.is_present = False', text)
        self.assertIn('employment.status_reason = "absent_from_export"', text)
        self.assertIn('employment.dismissal_date is None', text)
        self.assertIn('employment.dismissal_date > today', text)
        self.assertIn('employment.dismissal_date = today', text)

    def test_additional_source_uses_same_disappearance_rule(self):
        text = (
            ROOT / "app/services/onec_additional_import.py"
        ).read_text(encoding="utf-8")

        marker = 'employment.status_reason = "absent_from_export"'
        self.assertIn(marker, text)
        block_start = text.rfind(
            'employment.status = "dismissed"',
            0,
            text.index(marker) + len(marker),
        )
        block = text[block_start:text.index(marker) + len(marker)]
        self.assertIn('employment.dismissal_date is None', block)
        self.assertIn('employment.dismissal_date > today', block)
        self.assertIn('employment.dismissal_date = today', block)

    def test_rule_preserves_known_past_date(self):
        text = (
            ROOT / "app/services/hr_employment.py"
        ).read_text(encoding="utf-8")
        # Assignment is conditional rather than unconditional, so a known
        # date <= today remains untouched on subsequent imports.
        self.assertNotIn(
            'employment.dismissal_date = today\n        employment.status_reason',
            text,
        )


if __name__ == "__main__":
    unittest.main()
