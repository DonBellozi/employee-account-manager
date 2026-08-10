from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MailTemplateTestSendTests(unittest.TestCase):
    def setUp(self):
        self.router = (
            ROOT / "app/routers/mail_templates.py"
        ).read_text(encoding="utf-8")
        self.template = (
            ROOT / "app/templates/admin_mail_templates.html"
        ).read_text(encoding="utf-8")
        self.script = (
            ROOT / "app/static/mail_templates.js"
        ).read_text(encoding="utf-8")

    def test_test_endpoint_does_not_save_template(self):
        endpoint = self.router.split(
            '@router.post("/admin/mail-templates/{profile_id}/test")',
            1,
        )[1]
        self.assertIn("CredentialMailer(settings)._send", endpoint)
        self.assertNotIn("profile.personal_subject =", endpoint)
        self.assertNotIn("profile.corporate_subject =", endpoint)
        self.assertNotIn("dismissal_template.subject =", endpoint)

    def test_test_send_uses_safe_synthetic_values(self):
        self.assertIn('"TEST-Mail-Password-123!"', self.router)
        self.assertIn('"TEST-AD-Password-123!"', self.router)
        self.assertIn('"Иванов Иван Иванович"', self.router)
        self.assertIn('f"[ТЕСТ] {rendered_subject}"', self.router)

    def test_test_send_respects_dry_run(self):
        endpoint = self.router.split(
            '@router.post("/admin/mail-templates/{profile_id}/test")',
            1,
        )[1]
        self.assertIn("if settings.dry_run:", endpoint)
        self.assertIn('"sent": False', endpoint)

    def test_current_unsaved_form_is_sent_by_ajax(self):
        self.assertIn("body: new FormData(form)", self.script)
        self.assertIn("data-mail-template-test", self.template)
        self.assertIn("data-test-url", self.template)

    def test_test_recipient_is_remembered_locally(self):
        self.assertIn("mail-template-test-recipient", self.script)
        self.assertIn("localStorage.setItem", self.script)


if __name__ == "__main__":
    unittest.main()
