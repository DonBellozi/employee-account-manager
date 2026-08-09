from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RegistryMappingPersistenceTests(unittest.TestCase):
    def test_mapping_is_available_for_every_active_row(self):
        text = (
            ROOT / "app/services/hr_registry_multisource.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'f"/employees/registry/{row[\'id\']}/map"',
            text,
        )
        self.assertNotIn(
            'effective_status\n                    in {"issue", "error", "not_checked"}',
            text,
        )

    def test_incomplete_hr_data_protects_explicit_mapping(self):
        text = (
            ROOT / "app/services/hr_registry_multisource.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _mapping_safety_snapshot", text)
        self.assertIn("record.login.strip()", text)
        self.assertIn("record.corporate_email.strip()", text)
        self.assertIn("def _restore_mapping_safety_snapshot", text)

    def test_reconcile_restores_protected_mapping_before_shared_ad(self):
        text = (
            ROOT / "app/services/hr_registry_multisource.py"
        ).read_text(encoding="utf-8")
        snapshot = text.index("protected_mappings = self._mapping_safety_snapshot")
        reconcile = text.index("service.reconcile_current()", snapshot)
        restore = text.index("self._restore_mapping_safety_snapshot", reconcile)
        shared = text.index("self._resolve_shared_ad_for_source", restore)
        self.assertLess(snapshot, reconcile)
        self.assertLess(reconcile, restore)
        self.assertLess(restore, shared)


if __name__ == "__main__":
    unittest.main()
