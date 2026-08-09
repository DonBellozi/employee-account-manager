from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceModalTests(unittest.TestCase):
    def test_base_has_native_dialog_and_creation_trigger(self):
        text = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        self.assertIn('<dialog class="workspace-modal"', text)
        self.assertIn('href="/employees/new" data-workspace-modal', text)
        self.assertIn('/static/workspace_modal.js', text)
        self.assertIn('/static/workspace_modal.css', text)

    def test_registry_actions_use_modal(self):
        text = (ROOT / "app/templates/hr_registry.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("data-workspace-modal"), 3)
        self.assertIn('data-workspace-title="Сопоставление"', text)
        self.assertIn('data-workspace-title="Создать AD"', text)
        self.assertIn('data-workspace-title="Создание учетных записей"', text)

    def test_mapping_embedded_success_notifies_parent(self):
        text = (ROOT / "app/templates/hr_registry_mapping.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("workspace-modal-saved", text)
        self.assertIn('name="modal"', text)


if __name__ == "__main__":
    unittest.main()
