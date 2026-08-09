from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceModalNavigationTests(unittest.TestCase):
    def setUp(self):
        self.script = (
            ROOT / "app/static/workspace_modal.js"
        ).read_text(encoding="utf-8")

    def test_embedded_links_close_instead_of_navigate(self):
        self.assertIn(
            "const link = event.target.closest('a[href]')",
            self.script,
        )
        self.assertIn("event.preventDefault()", self.script)
        self.assertIn("closeWorkspace()", self.script)

    def test_success_message_closes_and_refreshes_parent(self):
        self.assertIn(
            "event.data?.type !== 'workspace-modal-saved'",
            self.script,
        )
        self.assertIn("changed = true", self.script)
        self.assertIn("window.location.reload()", self.script)

    def test_message_must_come_from_the_modal_iframe(self):
        self.assertIn(
            "event.source !== frame.contentWindow",
            self.script,
        )

    def test_hash_links_are_not_treated_as_navigation(self):
        self.assertIn("href.startsWith('#')", self.script)


if __name__ == "__main__":
    unittest.main()
