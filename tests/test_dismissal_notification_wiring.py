from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DismissalNotificationWiringTests(unittest.TestCase):
    def test_notice_is_unique_per_worker_and_dismissal_date(self):
        text = (
            ROOT / "app/models_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"worker_key",\n            "dismissal_date"', text)
        self.assertIn("uq_dismissal_notice_worker_date", text)

    def test_worker_waits_for_import_and_does_not_use_itinvent(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _import_running", text)
        self.assertIn('OneCImportRun.status == "running"', text)
        self.assertNotIn("ITInvent", text)
        self.assertNotIn("ActiveDirectoryService", text)
        self.assertNotIn("ZimbraService", text)

    def test_sent_notice_is_not_sent_again(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn('notice.status == "sent"', text)
        self.assertIn("continue", text)

    def test_only_final_current_or_future_candidates_are_mailed(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn("UpcomingDismissalService", text)
        self.assertIn('item["dismissal_date"] >= self.today', text)

    def test_aliases_can_be_deduplicated_by_zimbra_id_without_zimbra_query(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn("email_to_zimbra_id", text)
        self.assertIn('key = f"zimbra:{stable}"', text)
        self.assertNotIn("accounts_by_addresses", text)

    def test_corporate_sender_follows_recipient_domain(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "sender_domain = domain if domain in configured_domains else default_domain",
            text,
        )

    def test_all_enabled_sources_must_share_fresh_import_cycle(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _sources_synchronized", text)
        self.assertIn("OneCAdditionalSource.enabled.is_(True)", text)
        self.assertIn("timedelta(minutes=30)", text)
        self.assertIn('status": "sources_not_synchronized"', text)

    def test_new_dismissal_template_must_be_saved_before_auto_send(self):
        text = (
            ROOT / "app/services/dismissal_notifications.py"
        ).read_text(encoding="utf-8")
        self.assertIn("dismissal_templates[domain].updated_by", text)
        page = (
            ROOT / "app/templates/admin_mail_templates.html"
        ).read_text(encoding="utf-8")
        self.assertIn("начнется только после первого сохранения", page)

    def test_main_starts_and_stops_background_worker(self):
        text = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("DismissalNotificationWorker", text)
        self.assertIn("dismissal_notification_worker.start()", text)
        self.assertIn("dismissal_notification_worker.stop()", text)

    def test_journal_includes_notice(self):
        text = (
            ROOT / "app/routers/dashboard_dismissals.py"
        ).read_text(encoding="utf-8")
        self.assertIn("DismissalEquipmentNotice", text)
        self.assertIn("Уведомление о возврате оборудования", text)


if __name__ == "__main__":
    unittest.main()
