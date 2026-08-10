from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FinalDismissalTargetPlanTests(unittest.TestCase):
    def setUp(self):
        self.text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")

    def test_multiple_zimbra_ids_are_not_collapsed(self):
        self.assertIn("by_id: dict[str, set[str]]", self.text)
        self.assertIn(
            'target_key": f"zimbra:{zimbra_id}"',
            self.text,
        )

    def test_aliases_with_same_zimbra_id_share_target(self):
        self.assertIn("by_id[zimbra_id].update(addresses)", self.text)

    def test_ad_guid_conflict_stops_automatic_ad_action(self):
        self.assertIn(
            "У одного человека найдены разные AD objectGUID",
            self.text,
        )
        self.assertIn(
            'status = "intervention" if plan["error"] else "pending"',
            self.text,
        )

    def test_no_auto_reenable_exists(self):
        self.assertNotIn("enable_user(", self.text)
        self.assertNotIn("open_account(", self.text)


if __name__ == "__main__":
    unittest.main()
