from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MailTemplatesWorkspaceTests(unittest.TestCase):
    def test_page_is_two_pane_workspace(self):
        text = (
            ROOT / "app/templates/admin_mail_templates.html"
        ).read_text(encoding="utf-8")
        self.assertIn("mail-template-sidebar", text)
        self.assertIn("mail-template-editor-shell", text)
        self.assertIn("data-template-target", text)
        self.assertIn("data-template-panel", text)

    def test_dismissal_template_is_in_left_list(self):
        text = (
            ROOT / "app/templates/admin_mail_templates.html"
        ).read_text(encoding="utf-8")
        self.assertIn("'dismissal'", text)
        self.assertIn("Отсрочка блокировки повторное письмо не отправляет", text)

    def test_only_selected_panel_is_shown_by_js(self):
        text = (
            ROOT / "app/static/mail_templates.js"
        ).read_text(encoding="utf-8")
        self.assertIn("panel.classList.toggle('active'", text)
        self.assertIn("button.classList.toggle('active'", text)

    def test_responsive_layout_exists(self):
        text = (
            ROOT / "app/static/mail_templates.css"
        ).read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: minmax(240px, 290px)", text)
        self.assertIn("@media (max-width: 900px)", text)


if __name__ == "__main__":
    unittest.main()
